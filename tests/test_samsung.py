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
import sys
import tempfile
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

    # The .pac parser and the Research Download protocol are implemented and
    # tested in C++ now, so "blocked" would be false. What is missing is the
    # Python wrapper and the tab's wiring, and that is what the message says.
    check("the message says the protocol layer is done",
          "implemented" in unisoc.PENDING_REASON.lower(),
          unisoc.PENDING_REASON[:60])
    check("the message names what is still missing",
          "wrapper" in unisoc.PENDING_REASON.lower()
          and "wired" in unisoc.PENDING_REASON.lower())
    check("it points at the status document", "spd-status.md" in unisoc.PENDING_REASON)
    check("it does not still claim the protocol is blocked",
          "blocked" not in unisoc.PENDING_REASON.lower())
    check("the old name still resolves, so an existing import does not break",
          unisoc.BLOCKED_REASON is unisoc.PENDING_REASON)

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
    window = MainWindow(BackendService())
    window.resize(1360, 860)
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
    check("the SPD panel states that the protocol is implemented and tested",
          any("implemented and covered by" in text for text in labels),
          next((t for t in labels if "implemented and covered" in t), "not found")[:70])
    check("the banner says package inspection works",
          any("Package inspection works" in text for text in labels))
    check("the banner says flashing does not",
          any("flashing does not" in text for text in labels))
    check("the banner no longer says blocked",
          not any("BLOCKED" in text for text in labels))
    check("the SPD actions are present", len(spd_buttons) == 6, str(sorted(spd_buttons)))
    check("the PAC loader is implemented",
          "Not implemented" not in spd_buttons["Load PAC File…"].toolTip(),
          spd_buttons["Load PAC File…"].toolTip()[:60])
    check("the SPD handshake says it is unavailable",
          "Not available" in spd_buttons["Research Download Handshake"].toolTip(),
          spd_buttons["Research Download Handshake"].toolTip()[:60])
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
