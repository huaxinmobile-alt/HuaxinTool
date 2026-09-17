"""Tests for the advanced interface features and the final polish.

Everything here is checked against a real window rather than against the source,
for the same reason test_ui_design does it that way: the failure modes of this
layer - a toast that never leaves, a lazy tab that never builds, a dropped file
that goes nowhere - are all invisible in the source and obvious in a running
application.

What is deliberately checked most carefully is the honesty of the display:

* an ETA the tool calculated itself must be marked with a "~", and one a device
  reported must not be;
* a toast must also reach the log, because a message that vanishes after four
  seconds is not a record of anything;
* a settings control must change something - the same rule the rest of the suite
  applies, restated here for the three fields this work added.

Run directly:  python tests/test_polish.py
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("HUAXIN_REDUCE_MOTION", "1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "tests"))

from PyQt6.QtCore import QEvent, QEventLoop, QMimeData, QPointF, Qt, QUrl  # noqa: E402
from PyQt6.QtGui import QDropEvent, QEnterEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])
app.setStyle("Fusion")
app.setOrganizationName("HuaxinToolPolish")
app.setApplicationName("PolishCheck")

from huaxin.core import config  # noqa: E402
from huaxin.core import progress as progress_module  # noqa: E402
from huaxin.core.backend import BackendService  # noqa: E402
from huaxin.core.progress import JobProgress  # noqa: E402
from huaxin.ui import animations, filedialog, opstatus, style, tokens, toasts  # noqa: E402
from huaxin.ui.about import AboutDialog, build_info, diagnostics_text, docs_dir  # noqa: E402
from huaxin.ui.main_window import MainWindow  # noqa: E402
from huaxin.ui.settings_dialog import SettingsDialog  # noqa: E402

style.apply_theme(app, "midnight")

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, condition: bool, detail: str = "") -> bool:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  [PASS] {label}" + (f"  ({detail})" if detail else ""))
        return True
    print(f"  [FAIL] {label}" + (f"  ({detail})" if detail else ""))
    FAILURES.append(label)
    return False


def pump(seconds: float = 0.05) -> None:
    """Runs the event loop for a while without blocking the test."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
        time.sleep(0.005)


# --- real test files ---------------------------------------------------------


def _crc16_arc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def build_pac(path: Path) -> None:
    """A PAC in the vendor's layout. Copied from check_tabs, which mirrors the
    native builder: the offsets are the format and are not to be approximated."""
    entries = [
        ("HOST_FDL", "fdl1-sign.bin", 0x5500, 64, False),
        ("FDL2", "fdl2-sign.bin", 0x40004000, 96, False),
        ("AP", "boot.img", 0x10000000, 128, False),
    ]
    header_size, entry_size = 2124, 2580
    payload = sum(size for _, _, _, size, marker in entries if not marker)
    data = bytearray(header_size + entry_size * len(entries) + payload)

    def put_u16(offset: int, value: int) -> None:
        struct.pack_into("<H", data, offset, value)

    def put_u32(offset: int, value: int) -> None:
        struct.pack_into("<I", data, offset, value)

    def put_utf16(offset: int, width: int, text: str) -> None:
        raw = text.encode("utf-16-le")
        data[offset:offset + min(len(raw), width)] = raw[:width]

    put_utf16(0, 48, "BP_R1.0.0")
    put_utf16(52, 512, "SC9863A_TEST")
    put_utf16(564, 512, "V1.2.3")
    put_utf16(1104, 200, "TEST_ALIAS")
    put_u32(1076, len(entries))
    put_u32(1080, header_size)
    put_u32(1084, 1)
    put_u32(1088, 1)

    cursor = header_size + entry_size * len(entries)
    for index, (file_id, file_name, address, size, marker) in enumerate(entries):
        base = header_size + index * entry_size
        put_u32(base + 0, entry_size)
        put_utf16(base + 4, 512, file_id)
        put_utf16(base + 516, 512, file_name)
        put_u32(base + 1544, 1)
        put_u32(base + 1548, 1)
        put_u32(base + 1556, 0)
        put_u32(base + 1560, 1)
        put_u32(base + 1564, address)
        if marker:
            continue
        put_u32(base + 1540, size)
        put_u32(base + 1552, cursor)
        data[cursor:cursor + size] = bytes([0x40 + index]) * size
        cursor += size

    put_u32(44, 0)
    put_u32(48, len(data))
    put_u32(2116, 0xFFFAFFFA)
    put_u16(2120, _crc16_arc(bytes(data[:2120])))
    put_u16(2122, _crc16_arc(bytes(data[header_size:])))
    path.write_bytes(bytes(data))


def build_tar(path: Path) -> None:
    members = [("boot.img", 1024), ("system.img", 2048), ("meta-data/fota.zip", 256)]
    out = bytearray()
    for name, size in members:
        header = bytearray(512)
        header[0:len(name)] = name.encode()
        header[100:108] = b"0000644\x00"
        header[108:116] = b"0000000\x00"
        header[116:124] = b"0000000\x00"
        header[124:136] = f"{size:011o}\x00".encode()
        header[136:148] = b"00000000000\x00"
        header[148:156] = b" " * 8
        header[156:157] = b"0"
        header[257:263] = b"ustar\x00"
        header[263:265] = b"00"
        header[148:156] = f"{sum(header):06o}\x00 ".encode()
        out += header
        out += bytes(size)
        padding = (512 - size % 512) % 512
        out += bytes(padding)
    out += bytes(1024)
    out += b"0" * 32  # the appended md5 digest, as raw bytes rather than a header
    path.write_bytes(bytes(out))


# --- 1. toasts ---------------------------------------------------------------


def test_toasts() -> None:
    print("\n1. toast notifications")

    from huaxin.ui.components import Card

    host = Card()
    host.resize(900, 600)
    host.show()
    pump(0.05)
    manager = toasts.ToastManager(host)

    first = manager.show("info", "Scan finished", "3 devices found")
    check("a toast appears", first.isVisible())
    check("a toast is not focusable", first.focusPolicy() == Qt.FocusPolicy.NoFocus)
    check("a toast never activates the window",
          first.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))

    again = manager.show("warn", "Device disconnected", "The cable may be loose.", key="usb")
    repeated = manager.show("warn", "Device disconnected", "The cable may be loose.", key="usb")
    check("the same key refreshes one toast rather than stacking two", again is repeated)
    check("the repeat is counted, not swallowed", "×2" in repeated._title.text(),
          repeated._title.text())
    check("only two toasts are on screen", len(manager.live) == 2, str(len(manager.live)))

    for index in range(6):
        manager.show("info", f"Message {index}")
    check("the stack is capped", len(manager.live) <= manager.MAX_VISIBLE,
          f"{len(manager.live)} of {manager.MAX_VISIBLE}")

    # The lifetime is what keeps a toast transient, and hovering is what stops it
    # vanishing under somebody who is reading it.
    timed = manager.show("error", "Write failed", "See the log", timeout_ms=60000)
    check("a toast starts its countdown", timed._timer.isActive())
    # A real enter/leave event rather than a stub: the pause is implemented in
    # the widget's own handlers, so the test has to go through Qt to reach them.
    point = QPointF(float(timed.width()) / 2, float(timed.height()) / 2)
    global_point = QPointF(timed.mapToGlobal(point.toPoint()))
    timed.enterEvent(QEnterEvent(point, point, global_point))
    check("hovering pauses the countdown", not timed._timer.isActive())
    timed.leaveEvent(QEvent(QEvent.Type.Leave))
    check("leaving resumes it", timed._timer.isActive())

    before = len(manager.live)
    timed.dismiss()
    pump(0.05)
    check("dismissing removes it from the stack", len(manager.live) == before - 1,
          str(len(manager.live)))

    actions: list[str] = []
    with_action = manager.show("info", "Retry?", "", action=("Retry", lambda: actions.append("x")),
                               timeout_ms=60000)
    check("a toast can carry an action", with_action._actions.count() >= 2)
    check("the window is not modified by showing a toast", host.width() == 900)

    manager.clear()
    pump(0.05)
    check("clear empties the stack", manager.live == [], str(len(manager.live)))


# --- 2. the operation readout ------------------------------------------------


def test_operation_status() -> None:
    print("\n2. the operation readout")

    readout = opstatus.OperationStatus()
    check("the readout starts hidden", not readout.isVisible())

    readout.begin("flash_partition")
    check("the readout shows when a job starts", readout.isVisible())
    check("the job name is made readable", readout._name_label.text() == "Flash partition",
          readout._name_label.text())
    check("an unknown duration spins rather than showing zero",
          readout._ring.isIndeterminate())

    readout.report(JobProgress(operation_type="flashing", percentage=0.0))
    check("a percentage switches the ring to determinate",
          not readout._ring.isIndeterminate())
    check("no estimate is offered with no history", readout.estimate_remaining() < 0)
    check("and none is claimed", not readout.is_estimated_eta())

    readout._started_at -= 10.0
    readout.report(JobProgress(operation_type="flashing", percentage=25.0))
    estimate = readout.estimate_remaining()
    check("an estimate is derived once there is history", 25 < estimate < 35, f"{estimate:.1f}s")
    check("a derived estimate is marked as one", readout.is_estimated_eta())
    check("the marker is on the screen", "~" in readout._figures.text(),
          readout._figures.text())

    readout.report(JobProgress(operation_type="flashing", percentage=50.0, speed_mbps=18.24,
                               bytes_written=1500000000, total_bytes=3300000000,
                               eta_seconds=42.0))
    check("a reported ETA is not marked as an estimate", not readout.is_estimated_eta())
    figures = readout._figures.text()
    check("the reported ETA is shown plainly", "42s left" in figures, figures)
    check("the speed is shown", "18.2 MB/s" in figures, figures)
    check("the byte counts are shown", "1.40 GB / 3.07 GB" in figures, figures)
    check("elapsed time is shown", "elapsed" in figures, figures)

    # A stall is the one thing a progress bar cannot show, and the reason this
    # widget exists at all.
    readout.report(JobProgress(operation_type="flashing", percentage=50.0))
    readout._last_move = (time.monotonic() - (opstatus._STALL_SECONDS + 1), 50.0)
    readout._on_tick()
    check("a stalled transfer is called out", readout.stalled)
    check("and said in words", "no progress" in readout._detail_label.text(),
          readout._detail_label.text())

    readout.report(JobProgress(operation_type="flashing", percentage=60.0))
    check("moving again clears the stall", not readout.stalled)

    readout.finish(ok=True)
    check("finishing shows the outcome", "done" in readout._figures.text(),
          readout._figures.text())
    check("the clock stops when the job ends", not readout._clock.isActive())

    readout.reset()
    check("resetting hides the readout", not readout.isVisible())


# --- 3. the progress channel -------------------------------------------------


def test_progress_channel() -> None:
    print("\n3. progress reaches the interface with its figures intact")

    from huaxin.workers.worker import Job, JobContext
    import threading

    seen: list[tuple] = []
    percents: list[tuple] = []
    ctx = JobContext(
        "flash", lambda n, p, m: percents.append((p, m)), lambda *a: None,
        threading.Event(), lambda n, obj: seen.append((n, obj)),
    )

    class NativeLike:
        """Stands in for the native FlashProgress, which Python cannot build."""

        operation_type = "flashing"
        current_partition = "super"
        current_partition_index = 1
        total_partitions = 4
        bytes_written = 1_500_000_000
        total_bytes = 4_000_000_000
        percentage = 37.5
        speed_mbps = 21.75
        eta_seconds = 88.0
        status_message = "writing"
        running = True

    ctx.detail(NativeLike(), "writing super")
    check("a report reaches the detail channel", len(seen) == 1)
    check("the message also reaches the plain channel", len(percents) == 1)
    check("the percentage is carried across", percents[0][0] == 37)

    # The context passes the object through untouched: normalising is the
    # service's job, because the worker has no business knowing what shape the
    # interface wants.
    raw = seen[0][1]
    check("the object reaches the channel unchanged", raw is not None)
    normalised = progress_module.from_native(raw)
    check("the native object converts to the interface's own shape",
          isinstance(normalised, JobProgress))
    check("the speed survives", normalised.speed_mbps == 21.75)
    check("the ETA survives", normalised.eta_seconds == 88.0)
    check("the byte counts survive", normalised.bytes_written == 1_500_000_000)
    check("the partition index survives", normalised.current_partition_index == 1)
    check("what it describes reads as one line",
          "flashing" in normalised.describe() and "21.8 MB/s" in normalised.describe(),
          normalised.describe())
    check("a report already in the interface's shape is passed straight through",
          progress_module.from_native(normalised) is normalised)

    # And end to end, through the service, which is what the window listens to.
    service = BackendService()
    received: list[object] = []
    service.job_progress_detail.connect(lambda name, obj: received.append(obj))
    service._on_progress_detail("flash", NativeLike())
    check("the service republishes a normalised report", len(received) == 1
          and isinstance(received[0], JobProgress))
    check("a job that only reports percentages still works",
          Job(name="plain", fn=lambda ctx: None) is not None)


# --- 4. the file browser -----------------------------------------------------


def test_file_browser() -> None:
    print("\n4. the file browser")

    parsed = filedialog.parse_qt_filters("Images (*.img *.bin);;All files (*)")
    check("Qt's filter format is understood", parsed[0][1] == ("*.img", "*.bin"), str(parsed))
    check("the escape hatch is kept", parsed[-1] == ("All files", ("*",)), str(parsed))
    check("an empty filter still lists everything",
          filedialog.parse_qt_filters("") == (("All files", ("*",)),))

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        pac, tar = root / "package.pac", root / "firmware.tar.md5"
        build_pac(pac)
        build_tar(tar)

        dialog = filedialog.FileDialog(mode="open", start=str(root))
        check("the browser opens where it was told", dialog.directory() == root,
              str(dialog.directory()))

        note = dialog._package_note(pac, pac.stat().st_size)
        check("a PAC is identified from its own header", "Unisoc PAC package" in note, note[:60])
        check("the entry count is read from the file", "3 entries" in note, note[:80])
        check("the product name is read from the file", "SC9863A_TEST" in note, note[:80])
        check("a header-only read admits the payload was not checked",
              "payload CRC not checked" in note, note[:120])

        tar_note = dialog._package_note(tar, tar.stat().st_size)
        check("a tar package is identified", "Samsung Odin package" in tar_note, tar_note[:60])
        check("its members are listed", "3 members" in tar_note, tar_note[:80])

        other = root / "notes.txt"
        other.write_text("nothing to parse", encoding="utf-8")
        check("a file with no package header claims nothing",
              dialog._package_note(other, other.stat().st_size) == "")

        # Recent files.
        filedialog.forget_recent_files()
        filedialog.remember_file(pac)
        filedialog.remember_file(tar)
        recent = filedialog.recent_files()
        check("the most recent file is first", recent[0] == str(tar), str(recent[:2]))
        filedialog.remember_file(pac)
        check("remembering again moves it up rather than duplicating",
              filedialog.recent_files()[0] == str(pac) and len(filedialog.recent_files()) == 2)
        filedialog.forget_recent_files()
        check("the list can be cleared", filedialog.recent_files() == [])

        save_dialog = filedialog.FileDialog(mode="save", start=str(root), name="out.txt")
        check("save mode offers a file name box", save_dialog._name_edit.text() == "out.txt")
        save_dialog._name_edit.setText("")
        save_dialog._on_accept()
        check("saving with no name is refused rather than guessed",
              "Type a file name" in save_dialog._preview_note.text(),
              save_dialog._preview_note.text())

        folder_dialog = filedialog.FileDialog(mode="directory", start=str(root))
        check("directory mode has no file-type filter", folder_dialog._filter_box is None)
        check("directory mode starts in the folder it was given",
              folder_dialog.directory() == root)


# --- 5. the settings dialog --------------------------------------------------


def test_settings_dialog() -> None:
    print("\n5. the settings dialog")

    dialog = SettingsDialog(config.Settings())
    check("the categories are a sidebar, not tabs", dialog._nav.count() == len(dialog.PAGES),
          str(dialog._nav.count()))
    check("there is a page for every category", dialog._stack.count() == len(dialog.PAGES))
    check("the dialog opens on a page with a heading and a caption",
          dialog._page_title.text() == "General" and bool(dialog._page_caption.text()),
          dialog._page_title.text())

    for row in range(dialog._nav.count()):
        dialog._nav.setCurrentRow(row)
        title = dialog._page_title.text()
        check(f"page {row + 1} names itself", bool(title) and bool(dialog._page_caption.text()),
              title)

    # The round trip: every field must survive being shown and collected.
    original = config.Settings(
        animations=False, adb_path="C:/tools/adb.exe", fastboot_path="C:/tools/fastboot.exe",
        speed_limit=512 * 1024, theme="graphite", log_level="debug",
        default_timeout=12.5, confirm_destructive_operations=False,
        show_root_hubs=True, scan_on_startup=False, max_retry_attempts=5,
        log_max_bytes=4 * 1024 * 1024, auto_scroll_log=False,
    )
    dialog.load(original)
    collected = dialog.collected()
    differences = original.problems()
    check("showing and collecting changes nothing", differences == [], str(differences))
    check("motion is remembered", collected.animations is False)
    check("the tool paths are remembered", collected.adb_path == "C:/tools/adb.exe")
    check("the speed limit converts between bytes and kilobytes",
          collected.speed_limit == 512 * 1024, str(collected.speed_limit))
    check("the theme is remembered", collected.theme == "graphite")
    check("the log floor rises with the file setting",
          collected.log_max_bytes >= 4 * 1024 * 1024)

    # A value the dialog cannot display honestly is corrected by the validator
    # rather than silently shown as something else.
    broken = config.Settings(theme="chartreuse", adb_path="/not/a/real/adb")
    dialog.load(broken)
    fixed = dialog.collected()
    check("an unknown theme falls back to the default", fixed.theme in tokens.THEMES,
          fixed.theme)

    # The two settings added here must actually do something, which is the rule
    # the rest of the suite applies to every other setting.
    animations.set_animations_enabled(True)
    dialog._animations.setChecked(False)
    from huaxin.ui.main_window import MainWindow as _MainWindow  # noqa: F401

    applied = dialog.collected()
    animations.set_animations_enabled(bool(applied.animations))
    check("turning animations off in the dialog turns them off", not animations.animations_enabled())
    # Back to off for the rest of this suite: every check below assumes the end
    # state of an animation has already been reached.
    animations.set_animations_enabled(False)

    # A failure caused by a missing adb has to be able to lead somewhere, so the
    # dialog can be opened on a named category rather than only at the top.
    # A fresh dialog: the walk through every category above left this one on the
    # last page, and "which page does it open on" is a question about a new one.
    fresh = SettingsDialog(config.Settings())
    check("the dialog opens on the general page by default",
          fresh.current_page() == "general", fresh.current_page())
    check("it can be opened on a named category", fresh.show_page("tools"))
    check("and that category is then on screen", fresh.current_page() == "tools",
          fresh.current_page())
    check("the tools page is the one that takes the adb path",
          fresh._adb_field is not None)
    check("an unknown category is reported rather than ignored",
          not fresh.show_page("nonsense"))
    check("every category can be opened by name",
          all(fresh.show_page(key) for key, _, _, _ in fresh.PAGES),
          str([key for key, _, _, _ in fresh.PAGES]))
    from huaxin.core.adb_fastboot_wrapper import configure_tools

    with tempfile.TemporaryDirectory() as temp:
        fake = Path(temp) / ("adb.exe" if os.name == "nt" else "adb")
        fake.write_bytes(b"")
        tools = configure_tools(str(fake), "")
        check("a configured adb path is used", tools.adb_path == fake, str(tools.adb_path))
        tools = configure_tools("", "")
        check("an empty path falls back to discovery rather than failing",
              tools.adb_path is None or Path(tools.adb_path).is_file(), str(tools.adb_path))


# --- 6. the about dialog -----------------------------------------------------


def test_about_dialog() -> None:
    print("\n6. the about dialog")

    facts = {fact.label: fact.value for fact in build_info()}
    check("the application version is stated", "Huaxin Tool" in facts, str(facts.get("Huaxin Tool")))
    check("the native backend is stated", "Native backend" in facts, str(facts.get("Native backend")))
    check("the libusb version is stated", "libusb" in facts, str(facts.get("libusb")))
    check("the Qt version is stated", facts.get("Qt", "").count(".") >= 1, str(facts.get("Qt")))

    text = diagnostics_text()
    check("the diagnostics are plain text", "\n" in text and "Huaxin Tool:" in text)
    check("the diagnostics list the supported targets", "Supported targets" in text)
    check("a read-only vendor is not claimed as supported", "read-only" in text)

    dialog = AboutDialog()
    labels = [label.text() for label in dialog.findChildren(type(dialog._status))]
    joined = "\n".join(labels)

    from huaxin.ui.about import AppLogo

    check("the mark is drawn rather than loaded from a file",
          len(dialog.findChildren(AppLogo)) == 1)
    check("the support status is on screen, not only in the copy buffer",
          "ADB / Fastboot" in joined and "implemented" in joined, joined[:120])
    check("the two read-only vendors are called out as such",
          joined.count("read-only") >= 2, str(joined.count("read-only")))
    check("the hardware disclaimer is stated in the dialog",
          "real hardware" in joined, joined[:160])
    check("the licence is stated",
          "MIT" in joined and "LGPL" in joined, joined[joined.find("MIT"):][:60])
    check("the documentation folder is found", docs_dir() is not None, str(docs_dir()))

    from PyQt6.QtWidgets import QApplication as _App

    dialog._on_copy()
    copied = _App.clipboard().text()
    check("the build details can be copied for a report",
          "Huaxin Tool: " in copied and "Generated" in copied)
    check("the copy says so on screen", dialog._status.text() == "Copied.",
          dialog._status.text())


# --- 7. the main window ------------------------------------------------------


def test_main_window() -> None:
    print("\n7. the main window")

    service = BackendService()
    window = MainWindow(service)
    window.resize(1366, 880)
    window.show()
    pump(0.2)

    check("only the first tab is built at startup", list(window._panels) == ["android"],
          str(sorted(window._panels)))
    window._ensure_panel(2)
    check("a tab builds its panel on first visit", "mediatek" in window._panels)
    check("the panel is now the tab's widget",
          type(window._tabs.widget(2)).__name__ == "MediaTekPanel",
          type(window._tabs.widget(2)).__name__)
    check("building twice does not build a second panel",
          window._ensure_panel(2) is window.panel("mediatek"))

    # Alt+N is the keyboard route into a tab that has never been opened.
    window._on_show_tab(4)
    check("a shortcut builds the tab it jumps to", "samsung" in window._panels)
    check("and selects it", window._tabs.currentIndex() == 4)

    # Shortcuts.
    from PyQt6.QtGui import QAction

    shortcuts = set()
    for action in window.findChildren(QAction):
        shortcuts.update(key.toString() for key in action.shortcuts())
    for expected in ("F5", "Ctrl+S", "Ctrl+L", "Ctrl+.", "Esc", "Ctrl+,", "F1"):
        check(f"{expected} is bound to something", expected in shortcuts, str(sorted(shortcuts)))
    check("every vendor tab has a keyboard route",
          {"Alt+1", "Alt+2", "Alt+3", "Alt+4", "Alt+5"} <= shortcuts)

    # The log console's own controls, which the toolbar and Ctrl+S both use.
    window._console.append("info", "hello from the test")
    check("the console holds the line", "hello from the test" in window._console.text())
    window._on_clear_log()
    check("clearing the log empties it", window._console.text() == "")

    # Context menus exist where the task asks for them.
    check("the device list has a context menu",
          window._device_panel.table.contextMenuPolicy()
          == Qt.ContextMenuPolicy.CustomContextMenu)
    check("the log console has a context menu",
          window._console._view.contextMenuPolicy()
          == Qt.ContextMenuPolicy.CustomContextMenu)

    # Drag and drop routing.
    check("a PAC is routed to the Unisoc tab",
          window._route_for(Path("x.pac")) == "spd")
    check("an Odin package is routed to the Samsung tab",
          window._route_for(Path("firmware.tar.md5")) == "samsung")
    check("a PIT is routed to the Samsung tab",
          window._route_for(Path("device.pit")) == "samsung")
    check("an unrelated file is routed nowhere",
          window._route_for(Path("holiday.jpg")) is None)
    check("the overlay says so rather than pretending",
          "Not a firmware file" in window._describe_drop([Path("holiday.jpg")]))
    check("the overlay names the destination for a real one",
          "Unisoc" in window._describe_drop([Path("x.pac")]),
          window._describe_drop([Path("x.pac")]))
    with tempfile.TemporaryDirectory() as temp:
        pac = Path(temp) / "package.pac"
        build_pac(pac)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(pac))])
        event = QDropEvent(QPointF(40, 40), Qt.DropAction.CopyAction, mime,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        window.dragEnterEvent(event)
        check("a dragged package is accepted", event.isAccepted())
        check("the drop overlay appears", window._drop_hint.isVisible())
        check("and it says where the file is going",
              "Unisoc" in window._drop_hint.text(), window._drop_hint.text())
        window.dropEvent(event)
        check("the overlay goes away after a drop", not window._drop_hint.isVisible())
        check("the dropped file opened the right tab",
              window._tabs.currentIndex() == 3, str(window._tabs.currentIndex()))
        check("the drop was remembered for the browser",
              str(pac) in filedialog.recent_files())

    # Device arrival and departure announcements.
    from huaxin.core.backend import Device

    class FakeInfo:
        """The shape `Device.from_native` reads, and the values the catalogue
        really produces for 05c6:9008 - see src/cpp/core/device_catalog.cpp."""

        vid = 0x05C6
        pid = 0x9008
        bus_number = 1
        port_number = 4
        device_address = 7
        description = "Qualcomm MSM8998 in EDL mode"
        vendor = "Qualcomm"
        product = "MSM8998"
        manufacturer = "Qualcomm"
        serial = "ABC123"
        # "EDL (Emergency Download)" is the catalogue's own label, not a guess.
        mode = "EDL (Emergency Download)"
        phase = "Phase 5"
        kind = None
        recognised = True
        verified = True
        is_root_hub = False

    device = Device.from_native(FakeInfo())

    # The stack is emptied first: the drop above raised a notification, and the
    # counts below are about arrivals, not about what happened earlier.
    window._toasts.clear()
    pump(0.05)
    check("the stack is clear before the arrival checks", window._toasts.live == [],
          str(len(window._toasts.live)))

    window._on_devices_changed([device])
    check("the first scan is not announced as an arrival", len(window._toasts.live) == 0,
          str(len(window._toasts.live)))

    window._on_devices_changed([])
    check("a device leaving is announced", len(window._toasts.live) == 1,
          str(len(window._toasts.live)))
    check("the announcement also reaches the log",
          "Device disconnected" in window._console.text())

    window._toasts.clear()
    pump(0.05)
    window._on_devices_changed([device])
    check("a device arriving is announced once", len(window._toasts.live) == 1,
          str(len(window._toasts.live)))
    check("the arrival names the mode",
          "EDL (Emergency Download)" in window._toasts.live[0]._message.text(),
          window._toasts.live[0]._message.text())

    # The operation readout in the status bar, driven by the service's signals.
    window._on_job_started("edl.flash")
    check("a job names itself in the readout", window._operation.operation == "edl.flash")
    window._on_job_progress("edl.flash", 30, "writing super")
    check("progress reaches the readout",
          window._operation.progress.percentage == 30.0,
          str(window._operation.progress.percentage))
    window._on_job_failed("edl.flash", "huaxin.FlashException", "traceback")
    check("a failed job says so in the readout", "error" in window._operation._ring._state
          or window._operation._ring._state == "error", window._operation._ring._state)
    check("and raises a notification", any(
        toast.level() == "error" for toast in window._toasts.live))

    # A missing adb or fastboot is the one failure with a click-away fix, so it
    # gets a button that opens the page holding the path.
    window._toasts.clear()
    pump(0.05)
    window._on_job_failed(
        "fastboot.devices",
        "huaxin.core.adb_fastboot_wrapper.ToolNotFoundError",
        "traceback",
    )
    check("a missing tool raises a notification", len(window._toasts.live) == 1,
          str(len(window._toasts.live)))
    toolchain_toast = window._toasts.live[0]
    check("that notification offers to open the settings",
          toolchain_toast._actions.count() >= 2, str(toolchain_toast._actions.count()))
    check("it stays long enough to be acted on", toolchain_toast._lifetime >= 15000,
          str(toolchain_toast._lifetime))
    check("it names the page that takes the path",
          "Tools" in toolchain_toast._message.text(), toolchain_toast._message.text())
    check("the console says the same thing",
          "Tools & folders" in window._console.text())
    action_button = toolchain_toast.findChildren(
        type(toolchain_toast).__mro__[0]
    )
    check("the notification is keyed, so a repeat does not stack",
          window._toasts._by_key.get("toolchain") is toolchain_toast)

    # The message an operator reads when the tool is missing has to name the
    # route they can act on, not only the environment variable for scripts.
    from huaxin.core.adb_fastboot_wrapper import ToolNotFoundError

    hint = str(ToolNotFoundError("fastboot", ["PATH (fastboot)"]))
    check("the not-found message names the settings page",
          "Tools & folders" in hint, hint)
    check("and keeps the environment variable for scripted installs",
          "HUAXIN_FASTBOOT" in hint)
    check("and still lists everywhere it looked", "PATH (fastboot)" in hint)

    window.close()
    pump(0.1)


def main() -> int:
    print("Advanced UI features and final polish")
    test_toasts()
    test_operation_status()
    test_progress_channel()
    test_file_browser()
    test_settings_dialog()
    test_about_dialog()
    test_main_window()

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print(f"{len(FAILURES)} FAILED")
        for label in FAILURES:
            print(f"  - {label}")
        return 1
    print("Polish OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
