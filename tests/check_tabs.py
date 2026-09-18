"""Drives every vendor tab end to end against real files.

Not a mock-up: it builds a genuine PAC and a genuine tar archive with the formats'
own test builders, points the SPD and Samsung tabs at them, and checks what the
tables and status badges say.

Also renders each tab so the result can be looked at.
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

from PyQt6.QtCore import QEventLoop  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])
app.setStyle("Fusion")
app.setOrganizationName("HuaxinToolScreenshots")
app.setApplicationName("TabCheck")

from huaxin.core.backend import BackendService  # noqa: E402
from huaxin.ui import style, tokens  # noqa: E402
from huaxin.ui.main_window import MainWindow  # noqa: E402

# The application applies the theme in app.py; this script builds the window
# itself, so it has to apply it too. Without this the renders come out in Qt's
# default light grey and show nothing about how the tabs actually look - which
# is what the first run of this script did.
style.apply_theme(app)

CHECKS = 0
FAILURES = 0


def check(description: str, passed: bool, detail: str = "") -> None:
    global CHECKS, FAILURES
    CHECKS += 1
    if not passed:
        FAILURES += 1
    print(f"  [{'PASS' if passed else 'FAIL'}] {description}" + (f"  ({detail})" if detail else ""))


def pump(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        time.sleep(0.01)


# --- building real test files ------------------------------------------------


def build_pac(path: Path, version_two: bool = False) -> None:
    """A PAC in the vendor's layout. Mirrors the native test builder, because the
    offsets are the format and are not something to approximate."""
    entries = [
        ("HOST_FDL", "fdl1-sign.bin", 0x5500, 64, False),
        ("FDL2", "fdl2-sign.bin", 0x40004000, 96, False),
        ("AP", "boot.img", 0x10000000, 128, False),
        ("MODEM", "modem.bin", 0x20000000, 256, False),
        ("MARKER", "", 0, 0, True),
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

    put_utf16(0, 44 if version_two else 48, "BP_R1.0.0")
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
    # The two CRCs the format defines, computed the way the native code does.
    put_u16(2120, _crc16_arc(bytes(data[:2120])))
    put_u16(2122, _crc16_arc(bytes(data[header_size:])))
    path.write_bytes(bytes(data))


def _crc16_arc(data: bytes) -> int:
    """CRC-16/ARC, poly 0xA001 reflected, init 0 - the PAC's header CRC."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def build_tar(path: Path, with_digest: bool = True) -> None:
    """A tar archive in the Odin shape, with the digest appended as raw bytes."""
    members = [("boot.img", 4096), ("system.img", 8192), ("meta-data/fota.zip", 512)]
    out = bytearray()
    for name, size in members:
        header = bytearray(512)
        header[0:len(name)] = name.encode()
        header[100:108] = b"000644 \0"
        header[108:116] = b"000000 \0"
        header[116:124] = b"000000 \0"
        header[124:136] = ("%011o " % size).encode()
        header[136:148] = b"00000000000 "
        header[156:157] = b"0"
        header[257:263] = b"ustar\0"
        header[263:265] = b"00"
        checksum = sum(header[:148]) + sum(b" " * 8) + sum(header[156:])
        header[148:156] = ("%06o\0 " % checksum).encode()
        out += header
        out += bytes([0x5A]) * size
        out += bytes((512 - size % 512) % 512)
    out += bytes(1024)
    if with_digest:
        out += bytes(range(16))
    path.write_bytes(bytes(out))


def build_pit(path: Path) -> None:
    """A PIT with the magic at zero and the entry count at offset 4.

    The count sits immediately after the magic - a fact the round-trip test in
    the native suite exists because it was once got wrong.
    """
    partitions = [
        ("BOOT", 1, 2048, 512),
        ("SYSTEM", 2, 8192, 512),
        ("USERDATA", 3, 16384, 512),
        ("RECOVERY", 4, 2048, 512),
    ]
    data = bytearray(28 + 132 * len(partitions))
    struct.pack_into("<I", data, 0, 0x12349876)
    struct.pack_into("<I", data, 4, len(partitions))
    for index, (name, identifier, blocks, block_size) in enumerate(partitions):
        base = 28 + index * 132
        # The offsets the parser reads: block size at 20, block count at 24, and
        # the name fields running from 36 to the end of the entry.
        struct.pack_into("<I", data, base + 4, 2)          # device_type
        struct.pack_into("<I", data, base + 8, identifier)
        struct.pack_into("<I", data, base + 20, block_size)
        struct.pack_into("<I", data, base + 24, blocks)
        struct.pack_into("<I", data, base + 32, blocks * block_size)
        data[base + 36:base + 36 + len(name)] = name.encode()
        data[base + 68:base + 68 + len(name)] = (name.lower() + ".img").encode()
    path.write_bytes(bytes(data))


# --- the run -----------------------------------------------------------------


def _grab(window, size: tuple[int, int]):
    """The window's contents at the size asked for.

    Renders the central widget rather than the window: under the offscreen
    platform the window's own surface loses the custom title bar's controls, and
    the 800x800 virtual screen clamps the window itself, so a 1366x880 request
    would come out at 690x628.
    """
    from PyQt6.QtCore import QSize

    target = window.centralWidget() or window
    wanted = QSize(*size)
    if target.size() != wanted:
        target.resize(wanted)
    return target.grab()


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="huaxin-tabs-"))
    pac_path = scratch / "SC9863A_test.pac"
    tar_path = scratch / "AP_test.tar.md5"
    pit_path = scratch / "device.pit"
    build_pac(pac_path)
    build_tar(tar_path)
    build_pit(pit_path)
    print(f"built test files in {scratch}")

    service = BackendService()
    window = MainWindow(service)
    window.resize(1366, 880)
    window.show()
    pump(0.4)
    service.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and service.state not in ("ready", "unavailable"):
        pump(0.1)
    print(f"backend {service.state}")

    tabs = window._tabs
    # The panels are built on first visit - that is the point of the lazy tabs -
    # so every tab is opened here before anything is inspected. Going through the
    # tab widget rather than reaching into the window's dict keeps this checking
    # what a user would see.
    for index in range(tabs.count()):
        tabs.setCurrentIndex(index)
        pump(0.05)
    panels = list(window._panels.values())
    by_name = {type(panel).__name__: panel for panel in panels}
    print(f"tabs: {[tabs.tabText(i) for i in range(tabs.count())]}")
    check("every tab built its panel on first visit", len(panels) == tabs.count(),
          f"{len(panels)} of {tabs.count()}")

    # --- every tab has the common furniture ------------------------------
    print("\nevery tab has the same furniture")
    for panel in panels:
        name = type(panel).__name__
        check(f"{name}: has a header", panel.header is not None)
        check(f"{name}: has a status badge", panel.header.badge.text() != "")
        check(f"{name}: has a device box", panel.header.device_box is not None)
        check(f"{name}: has an action grid of two columns", panel.actions._columns == 2)
        check(f"{name}: has an info note", panel.info.text() != "")
        check(f"{name}: every button has a tooltip",
              all(button.toolTip() for button in panel.actions.buttons().values()))

    # --- the two-column layout matches the specification ------------------
    from huaxin.ui import tabkit

    print("\nthe columns are what the specification asks for")
    expected_columns = {
        "AndroidPanel": (
            ["Check Toolchain", "List Fastboot Devices", "Reboot to Bootloader",
             "Reboot to Download Mode", "Erase Partition (fastboot)"],
            ["List ADB Devices", "Read Device Info (adb)", "Reboot to EDL",
             "Flash Partition (fastboot)", "Wipe Data (fastboot -w)"],
        ),
        # The Unisoc tab gained a real device session: handshake, read-back, reset
        # and power-off replaced the two buttons that used to say the transport was
        # missing. Flashing a package is still absent, deliberately - see the tab.
        "SpdPanel": (
            ["Check Device", "Research Download Handshake", "Read Back Entry…",
             "Reset Device"],
            ["Load PAC File…", "Read Device Info", "Flash PAC Firmware", "Power Off"],
        ),
        "SamsungPanel": (
            ["Check Download Mode", "Read PIT From Device", "Flash (Odin)"],
            ["Load PIT File…", "Load Firmware Package…", "Repartition (PIT)"],
        ),
    }
    for panel in panels:
        name = type(panel).__name__
        if name not in expected_columns:
            continue
        left, right = expected_columns[name]
        check(f"{name}: left column matches", tabkit.left_column(panel._actions) == left,
              str(tabkit.left_column(panel._actions)))
        check(f"{name}: right column matches", tabkit.right_column(panel._actions) == right,
              str(tabkit.right_column(panel._actions)))

    # --- loading a real PAC into the SPD tab ------------------------------
    print("\nthe SPD tab reads a real PAC")
    spd = by_name["SpdPanel"]
    package = None
    try:
        from huaxin.core import parsers

        package = parsers.read_pac(pac_path)
        spd.set_package(package, pac_path)
        pump(0.2)
    except Exception as exc:  # noqa: BLE001
        check("a PAC can be read and shown", False, repr(exc))
    if package is not None:
        view = spd._pac_view
        check("the entry table is populated", view.table.rowCount() == 5,
              str(view.table.rowCount()))
        check("the header names the product", "SC9863A_TEST" in view._caption.text(),
              view._caption.text()[:70])
        check("the version is shown", "V1.2.3" in view._caption.text())
        check("the size is shown", "MiB" in view._caption.text() or "KiB" in view._caption.text())
        check("the payload CRC is reported as unchecked",
              "not checked" in view._caption.text(), view._caption.text()[-60:])
        check("the header CRC is reported as ok", "header CRC ok" in view._caption.text())
        check("the badge says partial, not ready", spd.header.badge.state() == "partial")
        check("the badge names the file", pac_path.name in spd.header.badge.text(),
              spd.header.badge.text()[:60])
        role_column = [view.table.item(r, 2).text() for r in range(view.table.rowCount())]
        check("the loaders are identified", "Fdl1" in role_column and "Fdl2" in role_column,
              str(sorted(set(role_column))))

        # The context menu is what makes a row actionable without a button.
        menu = view._menu_for_row(0)
        check("a PAC row has a context menu", len(menu) >= 2, str([label for label, _ in menu]))
        check("the menu can copy the file id", menu[0][0] == "Copy file ID")

    # --- loading a real PIT and package into the Samsung tab --------------
    print("\nthe Samsung tab reads a real PIT and package")
    samsung = by_name["SamsungPanel"]
    try:
        pit = parsers.read_pit(pit_path)
        samsung.set_pit(pit, pit_path)
        archive = parsers.read_package(tar_path)
        samsung.set_package(archive, tar_path)
        pump(0.2)
    except Exception as exc:  # noqa: BLE001
        check("a PIT and a package can be read and shown", False, repr(exc))

    pit_view = samsung._pit_view
    check("the PIT table is populated", pit_view.table.rowCount() == 4,
          str(pit_view.table.rowCount()))
    check("the PIT columns are the ones specified",
          pit_view.COLUMNS[0] == "Partition" and "Block count" in pit_view.COLUMNS,
          str(pit_view.COLUMNS))
    check("the partition names come through",
          set(pit_view.partition_names()) == {"BOOT", "SYSTEM", "USERDATA", "RECOVERY"},
          str(pit_view.partition_names()))

    package_view = samsung._package_view
    check("the package table is populated", package_view.table.rowCount() == 3,
          str(package_view.table.rowCount()))
    members = [package_view.table.item(r, 0).text() for r in range(package_view.table.rowCount())]
    check("the members are the ones in the archive", "boot.img" in members, str(members))
    mapping = [package_view.table.item(r, 2).text() for r in range(package_view.table.rowCount())]
    check("boot.img maps to the BOOT partition", "BOOT" in mapping, str(mapping))
    check("system.img maps to the SYSTEM partition", "SYSTEM" in mapping, str(mapping))
    check("a member with no matching partition says so", "—" in mapping, str(mapping))
    check("the badge counts the mapped members", "loaded" in samsung.header.badge.text(),
          samsung.header.badge.text()[:70])

    # --- the destructive buttons are marked -------------------------------
    print("\nthe destructive buttons are marked as such")
    for panel in panels:
        buttons = panel.actions.buttons()
        danger = [key for key, button in buttons.items() if button.property("role") == "danger"]
        specs = {spec.key: spec for spec in panel._actions}
        check(f"{type(panel).__name__}: every danger button is a destructive action",
              all(specs[key].danger for key in danger), str(danger))

    # --- busy state -------------------------------------------------------
    print("\nbuttons go dead while a job runs")
    panel = by_name["AndroidPanel"]
    panel.set_selected_device(None)
    before = {k: b.isEnabled() for k, b in panel.actions.buttons().items()}
    panel.actions.set_busy(True)
    panel.actions.set_enabled_states(has_device=True, busy=True)
    check("nothing is pressable while busy",
          not any(b.isEnabled() for b in panel.actions.buttons().values()))
    panel.actions.set_busy(False)
    panel.actions.set_enabled_states(has_device=False, busy=False)
    check("the previous states are restored afterwards",
          all(panel.actions.buttons()[k].isEnabled() == v for k, v in before.items()))

    # --- render -----------------------------------------------------------
    out = ROOT / "docs" / "screenshots"
    out.mkdir(parents=True, exist_ok=True)
    for index in range(tabs.count()):
        tabs.setCurrentIndex(index)
        pump(0.4)
        label = tabs.tabText(index).strip().replace(" ", "-").replace("/", "").lower()
        target = out / f"tab-{index + 1}-{label}.png"
        # The central widget at the size that was asked for, not the window: the
        # offscreen platform's window grab loses most of the custom chrome, and
        # its 800x800 screen clamps a window shown on it. See screenshot_ui.capture.
        _grab(window, (1366, 880)).save(str(target))
        print(f"rendered {target.name}")

    # The SPD and Samsung tabs are rendered with a file loaded, which is the
    # state worth looking at.
    for index, name in ((3, "spd-loaded"), (4, "samsung-loaded")):
        tabs.setCurrentIndex(index)
        pump(0.4)
        _grab(window, (1366, 880)).save(str(out / f"tab-{name}.png"))
        print(f"rendered tab-{name}.png")

    service.shutdown()
    window.close()
    pump(0.3)

    print(f"\n{CHECKS - FAILURES}/{CHECKS} checks passed")
    if FAILURES:
        print(f"{FAILURES} FAILED")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
