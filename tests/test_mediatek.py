"""Verify the MediaTek integration from the Python side.

The protocol itself is verified natively (tests/cpp/test_mediatek.cpp) against a
scripted bootrom. What is checked here is the binding surface, the error paths
that matter when no device is attached, and the MediaTek tab being wired to real
handlers.

    python tests/test_mediatek.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

import huaxin_core  # noqa: E402

from huaxin.core import mediatek  # noqa: E402

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


def _sample_scatter():
    """A ScatterInfo built to the shape the C++ parser produces.

    Built by hand rather than parsed so the UI checks do not fail for a reason
    that belongs to the parser's own tests.
    """
    from huaxin.core.mediatek import PartitionRecord, ScatterInfo

    def record(index, name, file_name, download, start, size, region="", extras=None):
        return PartitionRecord(
            index=index, name=name, file_name=file_name, is_download=download,
            start_address=start, size=size, region=region, storage="HW_STORAGE_EMMC",
            operation_type="", extras=extras or {},
        )

    return ScatterInfo(
        path="C:/fw/MT6765_Android_scatter.txt", format="Modern", platform="MT6765",
        project="k65v1_64_bsp", storage="EMMC",
        partitions=(
            record("SYS1", "boot", "boot.img", True, 0x8000, 0x2000000, "EMMC_USER"),
            record("SYS0", "preloader", "preloader.bin", True, 0x0, 0x40000, "EMMC_BOOT_1"),
            record("SYS2", "userdata", "NONE", False, 0x208000, 0x40000000, "EMMC_USER"),
        ),
        unmodelled_keys=("type",),
    )


def _scatter_text() -> str:
    """A scatter file in the modern shape, as packages ship it."""
    return """- general: MTK_PLATFORM_CFG
  info:
    - config_version: V1.1.2
      platform: MT6765
      project: k65v1_64_bsp
      storage: EMMC
- partition_index: SYS0
  partition_name: preloader
  file_name: preloader.bin
  is_download: true
  linear_start_addr: 0x0
  partition_size: 0x40000
  region: EMMC_BOOT_1
  type: SV5_BL_BIN
- partition_index: SYS1
  partition_name: userdata
  file_name: NONE
  is_download: false
  linear_start_addr: 0x208000
  partition_size: 0x400000
"""


def main() -> int:
    print("1. command codes are exposed and correct")
    # These values are the crux of the protocol; if the binding ever shifted
    # them, the device would simply not answer.
    expected = {
        "Read16": 0xD0, "Read32": 0xD1, "Write16": 0xD2, "Write32": 0xD4,
        "JumpDa": 0xD5, "SendDa": 0xD7, "GetTargetConfig": 0xD8,
        "GetHwCode": 0xFD, "GetVersion": 0xFF,
    }
    for name, value in expected.items():
        member = getattr(huaxin_core.BromCommand, name)
        check(f"{name} is 0x{value:02x}", int(member) == value, hex(int(member)))

    check("the default download agent address is exposed",
          huaxin_core.MTK_DA_DEFAULT_LOAD_ADDRESS == 0x00200000,
          hex(huaxin_core.MTK_DA_DEFAULT_LOAD_ADDRESS))

    print("\n2. target config decoding")
    empty = huaxin_core.TargetConfig()
    check("a default config reports nothing set",
          not empty.secure_boot and not empty.sla_required and "no security" in empty.describe(),
          empty.describe())
    check("the struct renders readably", "TargetConfig(" in repr(empty), repr(empty))

    print("\n3. chip info shape")
    info = huaxin_core.BromChipInfo()
    check("a default chip info has no codes", not info.have_hw_code and info.hardware_code == 0)
    check("chip_name is always empty for now", info.chip_name == "",
          "the hardware-code table is deliberately not implemented")

    print("\n4. no MediaTek device attached (the common case here)")
    check("devices_present() returns a list", isinstance(mediatek.devices_present(), list))
    check("nothing is found on this machine", mediatek.devices_present() == [],
          str(mediatek.devices_present()))

    ctx = FakeContext()
    mediatek.verify_toolchain(ctx)
    warnings = ctx.lines("warn")
    check("the check warns rather than failing", bool(warnings), str(warnings[:1]))
    check("it names the USB IDs it looked for",
          any("0e8d:0003" in message for message in warnings))
    check("it explains how to enter BROM mode",
          any("volume" in message.lower() for message in warnings), str(warnings[:1]))

    ctx = FakeContext()
    try:
        mediatek.read_chip_info(ctx)
    except huaxin_core.ProtocolError as exc:
        check("reading chip info without a device raises ProtocolError", True,
              str(exc).splitlines()[0][:70])
        check("the message is the same actionable one",
              "0e8d:0003" in str(exc))
    else:
        check("reading chip info without a device raises ProtocolError", False)

    print("\n5. argument validation happens before any USB access")
    ctx = FakeContext()
    try:
        mediatek.load_download_agent(ctx, "C:/no/such/agent.bin")
    except ValueError as exc:
        check("a missing agent file is rejected", True, str(exc)[:60])
    else:
        check("a missing agent file is rejected", False)

    ctx = FakeContext()
    try:
        mediatek.load_download_agent(ctx, __file__, load_address=0)
    except ValueError as exc:
        check("a zero load address is rejected", True, str(exc))
    else:
        check("a zero load address is rejected", False)


    print("\n7. scatter parsing across the binding boundary")
    check("the scatter types are exposed",
          all(hasattr(huaxin_core, name)
              for name in ("ScatterFile", "ScatterPartition", "ScatterGeneral",
                           "parse_scatter", "load_scatter", "detect_scatter_format")),
          "missing: " + str([n for n in ("ScatterFile", "ScatterPartition", "ScatterGeneral")
                             if not hasattr(huaxin_core, n)]))

    text = _scatter_text()
    check("the modern shape is detected",
          huaxin_core.detect_scatter_format(text) == huaxin_core.ScatterFormat.Modern,
          str(huaxin_core.detect_scatter_format(text)))

    parsed = huaxin_core.parse_scatter(text)
    check("both partitions are read", len(parsed.partitions) == 2,
          str(len(parsed.partitions)))
    check("the general block is read", str(parsed.general.platform) == "MT6765",
          str(parsed.general.platform))
    check("the config version is read", str(parsed.general.config_version) == "V1.1.2")

    entry = parsed.partitions[0]
    check("the name is read", str(entry.name) == "preloader", str(entry.name))
    check("the index is kept", str(entry.index) == "SYS0", str(entry.index))
    check("the image file is read", str(entry.file_name) == "preloader.bin")
    check("is_download is a boolean", bool(entry.is_download))
    check("the size is read from hex", int(entry.size) == 0x40000, str(entry.size))
    check("the region is read", str(entry.region) == "EMMC_BOOT_1", str(entry.region))
    # The point of the extras map: a field this build does not model is still
    # visible rather than dropped.
    check("an unmodelled field is preserved", str(entry.extra["type"]) == "SV5_BL_BIN",
          str(dict(entry.extra)))
    check("the unmodelled keys are listed", "type" in list(parsed.unmodelled_keys()),
          str(list(parsed.unmodelled_keys())))

    check("only the entries with an image count as downloads", len(parsed.downloads()) == 1,
          str(len(parsed.downloads())))
    check("the download bytes are summed", parsed.total_download_bytes() == 0x40000,
          str(parsed.total_download_bytes()))
    check("a partition is found by name", parsed.find("PRELOADER") is not None)
    check("an unknown name finds nothing", parsed.find("nope") is None)

    # An explicit is_download false must not be flipped to true by the default.
    second = parsed.partitions[1]
    check("is_download false is honoured", not bool(second.is_download), str(second.is_download))
    check("the entry that states it is marked as stated", bool(second.is_download_stated))

    try:
        huaxin_core.parse_scatter("<?xml version=\"1.0\"?><data/>")
    except huaxin_core.ProtocolError as exc:
        check("a file that is not a scatter file is refused", "no partition entries" in str(exc),
              str(exc)[:50])
    else:
        check("a file that is not a scatter file is refused", False)

    print("\n8. the flash commands need a running agent")
    check("no session is open on a fresh import", not mediatek.session_open())
    ctx = FakeContext()
    for name, call in (
        ("read_flash_info", lambda: mediatek.read_flash_info(ctx)),
        ("flash_firmware", lambda: mediatek.flash_firmware(ctx, "scatter.txt")),
        ("readback_partition", lambda: mediatek.readback_partition(ctx, "s.txt", "boot", "out.img")),
        ("format_flash", lambda: mediatek.format_flash(ctx, "scatter.txt")),
        ("shutdown_device", lambda: mediatek.shutdown_device(ctx)),
    ):
        try:
            call()
        except mediatek.MtkNotReadyError as exc:
            check(f"{name} without a session raises MtkNotReadyError", True, str(exc)[:40])
            check(f"{name} names the missing step", "agent" in str(exc).lower()
                  or "session" in str(exc).lower())
        except Exception as exc:  # noqa: BLE001
            check(f"{name} without a session raises MtkNotReadyError", False,
                  f"{type(exc).__name__}: {exc}")
        else:
            check(f"{name} without a session raises MtkNotReadyError", False, "it ran")

    check("releasing with no session open is not an error",
          mediatek.release_session(ctx) is False)
    check("a session that is not open reports so", not mediatek.session_open())

    print("\n9. host-side validation before any device access")
    ctx = FakeContext()
    try:
        mediatek.load_scatter(ctx, "C:/no/such/scatter.txt")
    except ValueError as exc:
        check("a missing scatter file is rejected", True, str(exc)[:50])
    else:
        check("a missing scatter file is rejected", False)

    ctx = FakeContext()
    try:
        mediatek.load_download_agent(ctx, "C:/no/such/agent.bin")
    except ValueError as exc:
        check("a missing agent is still rejected", True, str(exc)[:50])
    else:
        check("a missing agent is still rejected", False)

    print("\n10. scatter files on disk are planned before anything is written")
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        package = Path(folder)
        scatter_path = package / "MT6765_Android_scatter.txt"
        scatter_path.write_text(_scatter_text(), encoding="utf-8")

        ctx = FakeContext()
        info = mediatek.load_scatter(ctx, scatter_path)
        check("the wrapper reports the format and platform",
              info.format == "Modern" and info.platform == "MT6765",
              f"{info.format}/{info.platform}")
        check("the wrapper lists the partitions", len(info.partitions) == 2,
              str(len(info.partitions)))
        check("it reports the downloads", len(info.downloads) == 1, str(len(info.downloads)))
        check("the summary line names what will be written",
              any("1 to write" in line for line in ctx.lines("ok")),
              str(ctx.lines("ok")[:1]))
        check("every partition gets a line", len(ctx.lines("output")) == 2,
              str(len(ctx.lines("output"))))
        check("an unmodelled field is called out rather than ignored",
              any("does not model" in line for line in ctx.lines("warn")),
              str(ctx.lines("warn")[:1]))

        # A package whose image is missing must fail before the device is
        # touched, not halfway through - so the check happens on the host side
        # and the error names the file.
        check("an erase-only entry is recognised as such",
              info.partitions[1].is_empty_region, str(info.partitions[1].display_name))

    print("\n11. a flash run reports what it did, partition by partition")
    from huaxin.core.mediatek import FlashOutcome, FlashReport

    report = FlashReport(
        partitions=(
            FlashOutcome("preloader", "preloader.bin", 0x40000, True, False, ""),
            FlashOutcome("boot", "boot.img", 0x2000000, True, False, ""),
            FlashOutcome("system", "system.img", 0, False, False, "the agent refused the write"),
            FlashOutcome("vendor", "", 0, False, True, "cancelled before this partition"),
        ),
        completed=False,
        summary="2 written, 1 failed",
    )
    check("the written partitions are listed",
          report.written == ("preloader", "boot"), str(report.written))
    check("the failed partitions are listed", report.failed == ("system",), str(report.failed))
    check("a cancelled partition is neither written nor failed",
          "vendor" not in report.written and "vendor" not in report.failed)
    check("the bytes actually written are summed",
          report.total_bytes == 0x40000 + 0x2000000, str(report.total_bytes))
    check("an unfinished run says so", not report.completed)

    print("\n12. progress carries the partition, the percentage and the speed")
    ctx = FakeContext()
    forwarder = mediatek._progress_forwarder(ctx)

    class FakeEvent:
        phase = "writing"
        partition = "boot"
        done = 5 * 1024 * 1024
        total = 10 * 1024 * 1024
        percent = 50
        index = 2
        count = 8

        @staticmethod
        def speed_text() -> str:
            return "12.5 MiB/s"

    forwarder(FakeEvent())
    line = ctx.lines("progress")[-1]
    check("the partition is named", "boot" in line, line)
    check("the percentage is reported", "50%" in line, line)
    check("the byte counts are reported", "5.0 MiB of 10.0 MiB" in line, line)
    check("the speed is reported", "12.5 MiB/s" in line, line)
    check("the position in the run is reported", "2/8" in line, line)

    # A reporter that raises must not abort a flash: the C++ side calls it
    # between packets, and an exception there would strand a write.
    class BrokenContext(FakeContext):
        def progress(self, percent: int, message: str = "") -> None:
            raise RuntimeError("the UI went away mid-flash")

    broken = mediatek._progress_forwarder(BrokenContext())
    try:
        broken(FakeEvent())
    except Exception as exc:  # noqa: BLE001
        check("a failing progress reporter does not raise into the transfer", False, str(exc))
    else:
        check("a failing progress reporter does not raise into the transfer", True)
    print("\n6. the MediaTek tab is wired")
    from PyQt6.QtWidgets import QApplication, QLabel, QPushButton, QTabWidget

    from huaxin.core.backend import BackendService
    from huaxin.ui.main_window import MainWindow
    from huaxin.ui.theme import build_stylesheet

    app = QApplication.instance() or QApplication([])  # noqa: F841
    app.setStyleSheet(build_stylesheet())
    service = BackendService()
    window = MainWindow(service)
    window.resize(1360, 860)

    tabs = window.findChild(QTabWidget)
    index = next(i for i in range(tabs.count()) if tabs.tabText(i) == "MediaTek")
    # Built on first visit; see the lazy tabs in main_window._ensure_panel.
    tabs.setCurrentIndex(index)
    panel = tabs.widget(index)
    buttons = {b.text(): b for b in panel.findChildren(QPushButton)}

    expected = {
        "Check Device",
        "BROM Handshake + Chip Info",
        "Read Target Config",
        "Load Download Agent…",
        "Read Flash Info",
        "Load Scatter File…",
        "Flash Firmware",
        "Readback Partition",
        "Format / Erase Flash",
        "Shut Down Device",
        "Close Session",
    }
    check("every action has a button", expected.issubset(buttons.keys()),
          "missing: " + str(sorted(expected - set(buttons))))
    check("all of them work without a selected device (they are self-contained)",
          all(buttons[name].isEnabled() for name in expected))
    check("the destructive actions are marked",
          all(buttons[name].property("role") == "danger"
              for name in ("Flash Firmware", "Format / Erase Flash", "Shut Down Device")))
    check("the storage actions are no longer stubs",
          "Not implemented yet" not in buttons["Readback Partition"].toolTip(),
          buttons["Readback Partition"].toolTip())

    labels = [label.text() for label in panel.findChildren(QLabel)]
    check("the panel states its implementation status",
          any("Partly implemented" in text for text in labels),
          next((t for t in labels if "Partly implemented" in t), "not found")[:60])
    check("the banner is honest that no hardware run has happened",
          any("None of it has been run against real hardware" in text for text in labels))
    check("the panel explains the Windows driver requirement",
          any("WinUSB" in text for text in labels))
    check("the panel explains that nothing works before a DA is loaded",
          any("no storage commands" in text for text in labels))

    from PyQt6.QtWidgets import QProgressBar, QTableWidget

    table = panel.findChild(QTableWidget, "ScatterTable")
    check("the scatter table is in the tab", table is not None)
    check("it has the columns a scatter plan needs",
          table is not None
          and [table.horizontalHeaderItem(i).text() for i in range(table.columnCount())]
          == ["Name", "Index", "Action", "Address", "Size", "Region", "Image file"],
          str([table.horizontalHeaderItem(i).text() for i in range(table.columnCount())])
          if table is not None else "no table")
    check("the progress bar is in the tab", panel.findChild(QProgressBar) is not None)

    if table is not None:
        table.set_scatter(_sample_scatter())
        check("one row per partition", table.rowCount() == 3, str(table.rowCount()))
        check("an entry with an image reads as a write",
              table.item(0, 2).text() == "write", table.item(0, 2).text())
        check("an entry with no image reads as an erase",
              table.item(2, 2).text() == "erase", table.item(2, 2).text())
        check("the address column is hexadecimal",
              table.item(0, 3).text().startswith("0x"), table.item(0, 3).text())
        check("rows are in flash-layout order, not file order",
              [table.item(r, 0).text() for r in range(table.rowCount())]
              == ["preloader", "boot", "userdata"],
              str([table.item(r, 0).text() for r in range(table.rowCount())]))
        table.selectRow(1)
        selected = table.selected_partition()
        check("a selected row yields its record",
              selected is not None and selected.name == "boot", str(selected))
        check("the download count ignores erase rows", table.download_count() == 2,
              str(table.download_count()))
        table.set_scatter(None)
        check("clearing the view removes the rows", table.rowCount() == 0)

    window.close()

    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("\nFailures:")
        for _, description, detail in failed:
            print(f"  - {description} {detail}")
        return 1
    print("MediaTek integration OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
