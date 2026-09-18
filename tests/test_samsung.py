"""Verify the Samsung integration from the Python side.

The PIT parser and Odin framing are verified natively
(tests/cpp/test_samsung.cpp). What is checked here is the binding surface, the
Python-level validation, the honest reporting of what is not implemented, and
that the tab is wired to real handlers.

    python tests/test_samsung.py
"""

from __future__ import annotations

import os
import struct
import pathlib
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

import huaxin_core  # noqa: E402

from huaxin.core import samsung, unisoc  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(description: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((bool(passed), description, detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {description}" + (f"  ({detail})" if detail else ""), flush=True)


class FakeContext:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def log(self, message: str, level: str = "info") -> None:
        self.events.append((level, message))

    def progress(self, percent: int, message: str = "") -> None:
        self.events.append(("progress", f"{percent}% {message}"))

    def check_cancelled(self) -> None:
        pass

    @property
    def cancelled(self) -> bool:
        return False

    def lines(self, level: str | None = None) -> list[str]:
        return [m for lvl, m in self.events if level is None or lvl == level]


def build_pit(entries: list[tuple[str, int, int, int]]) -> bytes:
    """Builds a well-formed PIT from (name, id, blocks, block size)."""
    data = struct.pack("<IIII", huaxin_core.PIT_MAGIC, len(entries), 0, 0) + b"\x00" * 12
    for name, identifier, blocks, block_size in entries:
        data += struct.pack("<9I", 0, 2, identifier, 1, 0, block_size, blocks, 0, 0)
        field = name.encode().ljust(32, b"\x00")
        data += field + field + b"\x00" * 32
    return data


def main() -> int:
    print("1. PitData is exposed and parses")
    check("the PIT magic is exposed", huaxin_core.PIT_MAGIC == 0x12349876,
          hex(huaxin_core.PIT_MAGIC))
    check("the entry size is exposed", huaxin_core.PIT_ENTRY_SIZE == 132)

    raw = build_pit([("BOOT", 1, 2048, 512), ("USERDATA", 2, 1048576, 512)])
    pit = huaxin_core.parse_pit(raw)
    check("both entries parse", len(pit) == 2, str(len(pit)))
    check("names are read", [e.partition_name for e in pit.entries] == ["BOOT", "USERDATA"])
    check("lookup works", pit.find("BOOT") is not None and pit.find("BOOT").identifier == 1)
    check("an absent name returns None", pit.find("NOPE") is None)
    check("size is computed from the geometry",
          pit.entries[0].size_bytes == 2048 * 512, str(pit.entries[0].size_bytes))
    check("the device type is named", pit.entries[0].device_type_name == "MMC")

    print("\n2. malformed PITs are refused")
    for label, data in (
        ("empty", b""),
        ("short header", b"\x00" * 10),
        ("wrong magic", b"\x00" * 28),
    ):
        try:
            huaxin_core.parse_pit(data)
        except huaxin_core.ProtocolError:
            check(f"{label} is refused", True)
        else:
            check(f"{label} is refused", False)

    # The entry count comes off the wire and drives an allocation, so a hostile
    # one has to be caught before it is believed.
    hostile = bytearray(build_pit([("BOOT", 1, 1, 512)]))
    hostile[4:8] = struct.pack("<I", 0xFFFFFFFF)
    try:
        huaxin_core.parse_pit(bytes(hostile))
    except huaxin_core.ProtocolError as exc:
        check("an absurd entry count is refused", True, str(exc)[:60])
    else:
        check("an absurd entry count is refused", False)

    print("\n3. PartitionTable wrapping")
    ctx = FakeContext()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "test.pit"
        path.write_bytes(raw)
        table = samsung.parse_pit_file(ctx, path)

    check("the table parses from a file", table.count == 2, str(table.count))
    check("the source is recorded", table.source.endswith("test.pit"))
    check("find() is case-insensitive", table.find("boot") is not None)
    check("total size sums the partitions", table.total_bytes == 2048 * 512 + 1048576 * 512,
          str(table.total_bytes))
    check("sizes are rendered for humans", table.partitions[0].size_human == "1.0 MiB",
          table.partitions[0].size_human)
    check("partitions are logged", len(ctx.lines("info")) > 2,
          f"{len(ctx.lines('info'))} info lines")

    ctx = FakeContext()
    try:
        samsung.parse_pit_file(ctx, "C:/no/such/file.pit")
    except ValueError as exc:
        check("a missing file is rejected", True, str(exc)[:50])
    else:
        check("a missing file is rejected", False)

    print("\n4. the Odin session is honest about what is missing")
    try:
        samsung.read_pit_from_device(FakeContext())
    except NotImplementedError as exc:
        check("reading a PIT from a device is explicitly unimplemented", True,
              str(exc)[:60])
        check("the message says what is missing", "transport" in str(exc).lower())
    else:
        check("reading a PIT from a device is explicitly unimplemented", False)

    print("\n5. SPD reports its real state rather than pretending")

    # The message has moved on twice: it first said "blocked", then "implemented
    # but not wired", and it now describes a working session that stops short of
    # replaying a package. What matters is that it names the missing part rather
    # than only the working one - a status line that lists what works and stops is
    # how a tool ends up quietly claiming more than it does.
    check("the message says what can be done",
          "handshake" in unisoc.PENDING_REASON.lower(),
          unisoc.PENDING_REASON[:60])
    check("the message names what is still missing",
          "not implemented" in unisoc.PENDING_REASON.lower()
          and "fdl1" in unisoc.PENDING_REASON.lower(),
          unisoc.PENDING_REASON[:120])
    check("and says why it is not attempted",
          "primary source" in unisoc.PENDING_REASON.lower())
    check("it points at the status document", "spd-status.md" in unisoc.PENDING_REASON)
    check("it does not still claim the protocol is blocked",
          "blocked" not in unisoc.PENDING_REASON.lower())
    check("the old name still resolves, so an existing import does not break",
          unisoc.BLOCKED_REASON is unisoc.PENDING_REASON)

    # --- the session wrapper's guards ---------------------------------------
    #
    # The session needs a device, and there is none in this test process, so what
    # can be checked here is the part that runs *before* the device does: the
    # refusals. Each of these is a mistake an operator can make, and each has to
    # fail with something they can act on rather than with a stack trace.
    from huaxin.core.unisoc import UnisocNotReadyError

    check("the session class is bound in the native module",
          hasattr(huaxin_core, "UnisocBsl") and hasattr(huaxin_core, "BslChipInfo"),
          "huaxin_core.UnisocBsl / BslChipInfo")
    check("the native class reports the download-mode ids it looks for",
          hasattr(huaxin_core.UnisocBsl, "devices_present"))

    check("nothing is open to begin with", not unisoc.session_open())
    check("closing nothing is not an error", unisoc.release_session(FakeContext()) is False)

    ctx = FakeContext()
    try:
        unisoc.bsl_read_device_info(ctx)
        check("reading with no session is refused", False, "no exception")
    except UnisocNotReadyError as exc:
        check("reading with no session is refused", True, str(exc)[:50])
        check("and the refusal says how to open one",
              "handshake" in str(exc).lower(), str(exc)[:70])

    ctx = FakeContext()
    try:
        unisoc.bsl_connect(ctx)
        check("connecting with no device attached is refused", False, "no exception")
    except UnisocNotReadyError as exc:
        check("connecting with no device attached is refused", True)
        check("and the refusal names the mode and the driver",
              "research download" in str(exc).lower() and "driver" in str(exc).lower(),
              str(exc)[:90])

    # The read-back guards run before any device call, so they are reachable here.
    ctx = FakeContext()
    for length, address, why in ((0, 0, "a zero-length read"), (-5, 0, "a negative length"),
                                 (16, 0x1_0000_0000, "an address wider than the protocol")):
        try:
            unisoc.bsl_read_region(ctx, address, length, pathlib.Path("nowhere.bin"))
            check(f"{why} is refused", False, "no exception")
        except UnisocNotReadyError:
            check(f"{why} is refused", True, "refused before the device is touched")
        except ValueError as exc:
            check(f"{why} is refused", True, str(exc)[:50])

    ctx = FakeContext()
    unisoc.verify_toolchain(ctx)
    check("it reports the state at warning level, not as an error",
          len(ctx.lines("warn")) == 1 and not ctx.lines("error"),
          str(ctx.lines("warn"))[:1])
    check("no device is claimed", unisoc.devices_present() == [],
          str(unisoc.devices_present()))

    print("\n6. both tabs are wired")
    from PyQt6.QtWidgets import QApplication, QLabel, QPushButton, QTabWidget

    from huaxin.core.backend import BackendService
    from huaxin.ui.main_window import MainWindow
    from huaxin.ui.theme import build_stylesheet

    app = QApplication.instance() or QApplication([])  # noqa: F841
    app.setStyleSheet(build_stylesheet())
    service = BackendService()
    window = MainWindow(service)
    window.resize(1360, 860)
    # The worker has to be running for a button press to *do* anything: without it
    # a submitted job is queued and never executed, which would make the checks
    # below pass by doing nothing at all.
    service.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and service.state not in ("ready", "unavailable"):
        time.sleep(0.05)
        app.processEvents()
    tabs = window.findChild(QTabWidget)

    samsung_index = next(i for i in range(tabs.count()) if tabs.tabText(i) == "Samsung")
    tabs.setCurrentIndex(samsung_index)  # the panels are built on first visit
    samsung_panel = tabs.widget(samsung_index)
    samsung_buttons = {b.text(): b for b in samsung_panel.findChildren(QPushButton)}
    check("the implemented Samsung actions have buttons",
          {"Check Download Mode", "Load PIT File…"}.issubset(samsung_buttons.keys()),
          str(sorted(samsung_buttons)))
    check("the PIT loader needs no device", samsung_buttons["Load PIT File…"].isEnabled())
    # The loaders read a real file, so they are implemented; the writers still
    # are not, and the tooltips are where that distinction is stated.
    # Implemented means the tooltip describes what the button does rather than
    # saying it cannot. Checking for a keyword in the description would be
    # testing the wording, which is not the property that matters.
    package_tip = samsung_buttons["Load Firmware Package…"].toolTip()
    check("the Samsung package loader is implemented",
          "Not implemented" not in package_tip and "Not available" not in package_tip,
          package_tip[:60])
    check("the actions that write say they are unavailable",
          "Not available" in samsung_buttons["Flash (Odin)"].toolTip(),
          samsung_buttons["Flash (Odin)"].toolTip()[:60])
    check("and the repartition button explains why it is the dangerous one",
          "unable to boot" in samsung_buttons["Repartition (PIT)"].toolTip(),
          samsung_buttons["Repartition (PIT)"].toolTip()[:60])

    spd_index = next(i for i in range(tabs.count()) if tabs.tabText(i) == "SPD / Unisoc")
    tabs.setCurrentIndex(spd_index)
    spd_panel = tabs.widget(spd_index)
    labels = [label.text() for label in spd_panel.findChildren(QLabel)]
    spd_buttons = {b.text(): b for b in spd_panel.findChildren(QPushButton)}
    # The Unisoc tab used to be read-only: package inspection worked and every
    # device button said the transport was missing. The transport exists now, so
    # these ask the questions that still matter - does it say what it can do, and
    # is it still honest about the one thing it cannot?
    check("the banner says a device can be read",
          any("does not replay a package yet" in text for text in labels),
          next((t for t in labels if "does not replay" in t), "not found")[:70])
    check("and names the handover that is missing",
          any("FDL1-to-FDL2" in text for text in labels))
    check("the banner no longer says blocked",
          not any("BLOCKED" in text for text in labels))
    check("the SPD actions are present", len(spd_buttons) == 8, str(sorted(spd_buttons)))
    check("the PAC loader is implemented",
          "Not implemented" not in spd_buttons["Load PAC File…"].toolTip(),
          spd_buttons["Load PAC File…"].toolTip()[:60])
    check("the SPD handshake is real now",
          "Not available" not in spd_buttons["Research Download Handshake"].toolTip(),
          spd_buttons["Research Download Handshake"].toolTip()[:60])
    check("and its tooltip explains the driver requirement",
          "driver" in spd_buttons["Research Download Handshake"].toolTip().lower(),
          spd_buttons["Research Download Handshake"].toolTip()[:80])
    check("read-back is wired to a button",
          "Read Back Entry…" in spd_buttons)
    check("flashing a package is still refused, with the reason",
          "primary source" in spd_buttons["Flash PAC Firmware"].toolTip()
          or "FDL1" in spd_buttons["Flash PAC Firmware"].toolTip(),
          spd_buttons["Flash PAC Firmware"].toolTip()[:80])
    # --- every device button actually reaches its job ------------------------
    #
    # This is a regression test for a real bug: the handshake button submitted its
    # job with an extra panel argument the function does not take, so pressing it
    # raised TypeError. The error handling caught it and the tool stayed up, which
    # is why the suite passed - nothing had ever pressed the button. What the
    # buttons must do without a device attached is *report* that there is no
    # device, in a sentence the operator can act on.
    if spd_panel is not None:
        console = window._console if hasattr(window, "_console") else None
        for key, label in (("handshake", "Research Download Handshake"),
                           ("read_info", "Read Device Info"),
                           ("reset", "Reset Device"),
                           ("power_off", "Power Off")):
            button = spd_panel.actions.buttons().get(key)
            if button is None:
                check(f"the {label} button exists", False, "not found")
                continue
            if console is not None:
                console.clear()
            button.click()
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline and window._service.is_busy:
                time.sleep(0.02)
                app.processEvents()
            time.sleep(0.1)
            app.processEvents()
            text = console.text() if console is not None else ""
            problem = next((l for l in text.splitlines()
                            if "TypeError" in l or "AttributeError" in l), "")
            check(f"{label}: reaches its job instead of raising a programming error",
                  problem == "", problem[:90])
            reported = next((l for l in text.splitlines() if "ERROR" in l), "")
            check(f"{label}: says there is no device, rather than failing silently",
                  "UnisocNotReadyError" in reported or "not found" in reported.lower()
                  or "not attached" in reported.lower(),
                  reported[:90] or "nothing logged")

    service.shutdown()
    window.close()

    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("\nFailures:")
        for _, description, detail in failed:
            print(f"  - {description} {detail}")
        return 1
    print("Samsung + SPD integration OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
