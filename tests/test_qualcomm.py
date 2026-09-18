"""Verify the Qualcomm EDL integration from the Python side.

The protocol itself is verified in C++ (tests/cpp/test_qualcomm.cpp) against a
scripted transport, because that is where the packet handling lives. What is
checked here is the binding surface and the behaviour the UI depends on:

  * the Firehose XML that Python can hand to the device
  * response parsing reaching Python intact
  * the error path when no EDL device is attached - the common case for anyone
    reading this code, and the one that must not look like a crash
  * the GPT, rawprogram and patch parsers across the binding boundary
  * the Firehose-before-programmer ordering, and the session that enforces it
  * the Qualcomm tab being wired to real handlers

    python tests/test_qualcomm.py
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch as mock_patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

import huaxin_core  # noqa: E402

from huaxin.core import qualcomm  # noqa: E402
from huaxin.core.qualcomm import DeviceIdentity  # noqa: E402
from huaxin.workers.worker import JobCancelled  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(description: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((bool(passed), description, detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {description}" + (f"  ({detail})" if detail else ""), flush=True)


class FakeContext:
    """Stands in for the worker's JobContext."""

    def __init__(self, cancel: bool = False) -> None:
        self.events: list[tuple[str, str]] = []
        self._cancelled = cancel

    def log(self, message: str, level: str = "info") -> None:
        self.events.append((level, message))

    def progress(self, percent: int, message: str = "") -> None:
        self.events.append(("progress", f"{percent}% {message}"))

    def check_cancelled(self) -> None:
        if self._cancelled:
            raise JobCancelled("test")

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def lines(self, level: str | None = None) -> list[str]:
        return [m for lvl, m in self.events if level is None or lvl == level]


#: A rawprogram0.xml in the shape Qualcomm's packages ship: attributes quoted,
#: all on one line, unknown extras present. Built here rather than read from a
#: file so the test does not depend on a firmware package being on disk.
_RAWPROGRAM = """<?xml version="1.0" ?>
<data>
  <program SECTOR_SIZE_IN_BYTES="4096" file_sector_offset="0" filename="gpt_main0.bin"
           label="PrimaryGPT" num_partition_sectors="6" physical_partition_number="0"
           start_sector="0" />
  <program SECTOR_SIZE_IN_BYTES="4096" file_sector_offset="0" filename="boot.img"
           label="boot" num_partition_sectors="16384" physical_partition_number="0"
           start_sector="48" />
</data>"""


def _build_gpt() -> tuple[bytes, bytes]:
    """A GPT header and entry array built to the UEFI spec, byte for byte.

    Written out by hand rather than with a library so the test is checking the
    parser against the specification, not against another implementation of it.
    """
    def entry(name: str, first: int, last: int, type_id: int, unique: int) -> bytes:
        raw = bytearray(128)
        # type_guid and unique_guid are 4+2+2+8 bytes, little-endian throughout,
        # because that is how the UEFI spec lays a GUID out on disk.
        struct.pack_into("<IHH", raw, 0, type_id, 0x1234, 0x5678)
        struct.pack_into("<IHH", raw, 16, unique, 0xAAAA, 0xBBBB)
        struct.pack_into("<QQQ", raw, 32, first, last, 0)
        encoded = name.encode("utf-16-le")
        raw[56 : 56 + len(encoded)] = encoded
        return bytes(raw)

    header = bytearray(512)
    header[0:8] = b"EFI PART"
    struct.pack_into("<III", header, 8, 0x00010000, 92, 0)  # revision 1.0, header size
    struct.pack_into("<QQQQ", header, 24, 1, 127, 34, 126)  # current/backup/first/last
    struct.pack_into("<IHH", header, 56, 0xDEADBEEF, 0x1111, 0x2222)
    header[64:72] = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF, 0x00, 0x11])
    struct.pack_into("<QIII", header, 72, 2, 4, 128, 0)  # array at LBA 2, 4 slots

    entries = (
        entry("boot", 34, 100, 1, 2)
        + entry("system", 101, 5000, 1, 3)
        + bytes(256)  # two unused slots
    )
    return bytes(header), entries


class _StubGuid:
    def __init__(self, text: str) -> None:
        self._text = text

    def to_string(self) -> str:
        return self._text


class _StubHeader:
    revision_string = "1.0"
    disk_guid = _StubGuid("00000000-0000-0000-0000-000000000000")


class _StubEntry:
    """One GPT entry as the binding presents it: properties, not attributes."""

    def __init__(self, name: str, first: int, last: int, unused: bool) -> None:
        self.name = name
        self.first_lba = first
        self.last_lba = last
        self.is_unused = unused
        self.type_guid = _StubGuid("t" * 36)
        self.unique_guid = _StubGuid("u" * 36)

    @property
    def sector_count(self) -> int:
        return (self.last_lba - self.first_lba + 1) if not self.is_unused else 0

    def size_bytes(self, sector_size: int) -> int:
        return self.sector_count * sector_size


class _StubTable:
    header = _StubHeader()
    entries = (
        _StubEntry("boot", 34, 41, False),
        _StubEntry("system", 42, 200, False),
        _StubEntry("", 0, 0, True),
    )


class _StubGeometry:
    def __init__(self, block_size: int) -> None:
        self.total_blocks = 1024
        self.block_size = block_size
        self.storage_type = "UFS"
        self.raw_json = "{}"

    def total_bytes(self) -> int:
        return self.total_blocks * self.block_size


class _StubEdl:
    """Records the calls the wrapper makes, so their order can be asserted."""

    def __init__(self, calls: list[tuple], block_size: int) -> None:
        self._calls = calls
        self._block_size = block_size

    def get_storage_info(self, lun: int) -> _StubGeometry:
        self._calls.append(("get_storage_info", lun))
        return _StubGeometry(self._block_size)

    def read_gpt(self, lun: int, sector_size: int) -> _StubTable:
        self._calls.append(("read_gpt", lun, sector_size))
        return _StubTable()

    def set_progress_callback(self, _callback: object) -> None:
        pass

    def disconnect(self) -> None:
        pass


def _stub_session(block_size: int = 4096) -> tuple[object, list[tuple]]:
    """A live session backed by the stub above, installed into the wrapper."""
    calls: list[tuple] = []
    session = qualcomm._LiveSession(native=huaxin_core, edl=_StubEdl(calls, block_size))
    return session, calls


def main() -> int:
    print("1. Firehose XML built from Python")
    request = huaxin_core.ConfigureRequest()
    request.memory_name = "eMMC"
    request.max_payload_size_to_target = 4096
    xml = huaxin_core.build_configure_xml(request)
    check("configure carries MemoryName and payload size",
          'MemoryName="eMMC"' in xml and 'MaxPayloadSizeToTargetInBytes="4096"' in xml, xml)
    check("the document has the prolog and data root",
          xml.startswith("<?xml") and xml.endswith("</data>"))

    read = huaxin_core.ReadRequest()
    read.sector_size_in_bytes = 512
    read.num_partition_sectors = 2048
    read.start_sector = 34
    read.filename = "gpt_main0.bin"
    read_xml = huaxin_core.build_read_xml(read)
    check("read carries the GPT geometry",
          'num_partition_sectors="2048"' in read_xml and 'start_sector="34"' in read_xml, read_xml)

    power = huaxin_core.PowerRequest()
    check("power defaults to reset", 'value="reset"' in huaxin_core.build_power_xml(power))
    check("ping uses the self-closing form", "<ping/>" in huaxin_core.build_ping_xml())

    # The injection guard has to hold at the binding boundary too.
    hostile = huaxin_core.ReadRequest()
    hostile.filename = 'a" value="injected'
    escaped = huaxin_core.build_read_xml(hostile)
    check("a quote in a filename cannot escape the attribute",
          "&quot;" in escaped and 'value="injected"' not in escaped, escaped)

    print("\n2. response parsing reaches Python")
    response = huaxin_core.parse_firehose_response(
        '<?xml version="1.0" ?><data><log value="sector 0 written"/>'
        '<response value="ACK" rawmode="true"/></data>'
    )
    check("ACK is recognised", response.acknowledged)
    check("the status enum is exposed", response.status == huaxin_core.FirehoseStatus.Ack,
          str(response.status))
    check("rawmode is surfaced", response.raw_mode)
    check("log lines arrive unescaped", list(response.logs) == ["sector 0 written"], str(response.logs))

    nak = huaxin_core.parse_firehose_response(
        '<?xml version="1.0" ?><data><log value="bad sector"/>'
        '<response value="NAK"/></data>'
    )
    check("NAK is not acknowledged", not nak.acknowledged)
    check("the NAK reason is readable", list(nak.logs) == ["bad sector"])

    try:
        huaxin_core.parse_firehose_response("definitely not xml")
    except huaxin_core.ProtocolError as exc:
        check("non-XML raises ProtocolError", True, str(exc)[:50])
    else:
        check("non-XML raises ProtocolError", False)

    print("\n3. deterministic USB discovery failure paths")
    # Unit tests must not enumerate or send Sahara commands to a real phone.
    with mock_patch.object(huaxin_core.QualcommEdl, "device_present", return_value=False):
        check("device_present() reports False", qualcomm.device_present() is False)

        ctx = FakeContext()
        qualcomm.verify_toolchain(ctx)
        warnings = ctx.lines("warn")
        check("the toolchain check warns rather than failing",
              any("05c6:9008" in message for message in warnings), str(warnings[:1]))
        check("it explains how to reach EDL mode",
              any("reboot edl" in message for message in warnings))

    with mock_patch.object(huaxin_core.QualcommEdl, "read_device_info",
                      side_effect=huaxin_core.ProtocolError("no device with USB ID 05c6:9008")):
        ctx = FakeContext()
        try:
            qualcomm.read_device_info(ctx)
        except huaxin_core.ProtocolError as exc:
            message = str(exc)
            check("reading identity without a device raises ProtocolError", True, message.splitlines()[0])
            check("the error says which USB ID was looked for", "05c6:9008" in message or "9008" in message)
        else:
            check("reading identity without a device raises ProtocolError", False)

    check("USB discovery errors remain catchable as protocol errors",
          issubclass(huaxin_core.UsbDiscoveryError, huaxin_core.ProtocolError))
    for stage in ("libusb_init", "libusb_get_device_list"):
        ctx = FakeContext()
        failure = huaxin_core.UsbDiscoveryError(stage + " failed: presence unknown")
        with mock_patch.object(huaxin_core.QualcommEdl, "device_present", side_effect=failure):
            try:
                qualcomm.verify_toolchain(ctx)
            except huaxin_core.UsbDiscoveryError as exc:
                check(stage + " failure is propagated", str(exc) == str(failure))
            else:
                check(stage + " failure is propagated", False)
        check(stage + " failure does not claim the phone is absent",
              not any("no device" in message for message in ctx.lines("warn")))

    print("\n4. argument validation happens before any USB access")
    ctx = FakeContext()
    try:
        qualcomm.load_programmer(ctx, "C:/no/such/programmer.elf")
    except ValueError as exc:
        check("a missing programmer file is rejected", True, str(exc)[:60])
    else:
        check("a missing programmer file is rejected", False)

    ctx = FakeContext()
    try:
        qualcomm.send_firehose(ctx, "   ")
    except ValueError as exc:
        check("an empty XML command is rejected", True, str(exc))
    else:
        check("an empty XML command is rejected", False)

    print("\n5. the Firehose-before-programmer ordering is enforced")
    # A fresh session has no programmer loaded, so any Firehose call must refuse
    # rather than write XML into a device that is still in Sahara.
    edl = huaxin_core.QualcommEdl()
    check("a fresh session reports no programmer", not edl.programmer_loaded())
    try:
        edl.ping()
    except huaxin_core.ProtocolError as exc:
        check("a Firehose command without a programmer is refused", True, str(exc)[:60])
        check("the refusal names the missing step", "programmer" in str(exc).lower())
    else:
        check("a Firehose command without a programmer is refused", False)
    edl.disconnect()

    # The Python wrapper keeps one session open between jobs, so the same
    # ordering has to hold at that level too.
    check("no session is open on a fresh import", not qualcomm.session_open())
    ctx = FakeContext()
    for name, call in (
        ("configure", lambda: qualcomm.configure(ctx)),
        ("storage_info", lambda: qualcomm.storage_info(ctx)),
        ("read_gpt", lambda: qualcomm.read_gpt(ctx)),
        ("erase_partition", lambda: qualcomm.erase_partition(ctx, 0, 1)),
        ("apply_patch", lambda: qualcomm.apply_patch(ctx, "patch0.xml")),
    ):
        try:
            call()
        except qualcomm.EdlNotReadyError as exc:
            check(f"{name} without a session raises EdlNotReadyError", True, str(exc)[:40])
            check(f"{name} says which step is missing", "programmer" in str(exc).lower())
        except Exception as exc:  # noqa: BLE001
            check(f"{name} without a session raises EdlNotReadyError", False,
                  f"{type(exc).__name__}: {exc}")
        else:
            check(f"{name} without a session raises EdlNotReadyError", False, "it ran")

    check("releasing with no session open is not an error", qualcomm.release_session(ctx) is False)

    print("\n6. identity formatting")
    class NativeInfo:
        protocol_version = 2
        have_serial = True
        serial = 0xDEADBEEF
        have_hardware_id = True
        hardware_id = 0x0000008C00AB1234
        msm_id = 0x8C
        oem_id = 0xAB
        model_id = 0x1234
        pk_hash = "0102"
        chip_name = ""

    identity = DeviceIdentity.from_native(NativeInfo())
    check("serial is rendered as hex", identity.serial == "0xdeadbeef", identity.serial)
    check("hardware id is rendered as 64-bit hex", len(identity.hardware_id) == 18,
          identity.hardware_id)
    check("msm/oem/model ids survive", (identity.msm_id, identity.oem_id, identity.model_id)
          == (0x8C, 0xAB, 0x1234))
    check("the absence of a chip name is detectable", identity.chip_name == "")

    print("\n7. the Qualcomm tab is wired")
    from PyQt6.QtWidgets import QApplication, QLabel, QPushButton

    from huaxin.core.backend import BackendService
    from huaxin.ui.main_window import MainWindow
    from huaxin.ui.theme import build_stylesheet

    app = QApplication.instance() or QApplication([])  # noqa: F841
    app.setStyleSheet(build_stylesheet())
    service = BackendService()
    window = MainWindow(service)
    window.resize(1360, 860)

    from PyQt6.QtWidgets import QTabWidget

    tabs = window.findChild(QTabWidget)
    index = next(i for i in range(tabs.count()) if tabs.tabText(i) == "Qualcomm")
    # The panels are built on first visit, so the tab is opened the way a user
    # opens it before anything on it is inspected.
    tabs.setCurrentIndex(index)
    panel = tabs.widget(index)

    buttons = {b.text(): b for b in panel.findChildren(QPushButton)}
    expected = {
        "Check EDL Device",
        "Read Chip Info (Sahara)",
        "Load Firehose Programmer…",
        "Configure Device",
        "Get Storage Info",
        "Read GPT",
        "Flash Firmware…",
        "Read Partition…",
        "Erase Partition…",
        "Apply Patch (patch0.xml)…",
        "Reset Device",
        "Close Firehose Session",
    }
    check("every action has a button", expected.issubset(buttons.keys()),
          "missing: " + str(sorted(expected - set(buttons))))
    check("the destructive actions are marked",
          all(buttons[label].property("role") == "danger"
              for label in ("Reset Device", "Flash Firmware…", "Erase Partition…")))
    check("a device is not required to open a programmer dialog",
          buttons["Load Firehose Programmer…"].isEnabled())
    labels = [label.text() for label in panel.findChildren(QLabel)]
    check("the panel states its implementation status",
          any("Partly implemented" in text for text in labels),
          next((t for t in labels if "Partly implemented" in t), "not found"))
    check("the banner is honest that no hardware run has happened",
          any("None of it has been run against real hardware" in text for text in labels))
    check("the panel explains the Windows driver requirement",
          any("WinUSB" in text for text in labels))
    check("the GPT read is no longer a stub",
          "Not implemented yet" not in buttons["Read GPT"].toolTip(),
          buttons["Read GPT"].toolTip())
    check("implemented actions do not carry the warning",
          "Not implemented yet" not in buttons["Read Chip Info (Sahara)"].toolTip())

    from PyQt6.QtWidgets import QProgressBar, QTableWidget

    table = panel.findChild(QTableWidget, "PartitionTable")
    check("the partition table is in the tab", table is not None)
    check("it has the columns the GPT fills",
          table is not None
          and [table.horizontalHeaderItem(i).text() for i in range(table.columnCount())]
          == ["Name", "Start LBA", "Sectors", "Size", "LUN", "Type GUID"],
          str([table.horizontalHeaderItem(i).text() for i in range(table.columnCount())])
          if table is not None
          else "no table")
    check("the progress bar is in the tab", panel.findChild(QProgressBar) is not None)

    if table is not None:
        table.set_table(qualcomm.PartitionTable(
            lun=0, sector_size=4096, revision="1.0",
            disk_guid="deadbeef-1111-2222-aabb-ccddeeff0011", entry_slots=4,
            partitions=(
                qualcomm.PartitionRecord(0, "boot", 34, 100, 67, 67 * 4096, "t", "u", False),
                qualcomm.PartitionRecord(1, "system", 101, 5000, 4900, 4900 * 4096, "t", "u", False),
                qualcomm.PartitionRecord(2, "", 0, 0, 0, 0, "0", "0", True),
            ),
        ))
        check("unused slots are not given a row", table.rowCount() == 2, str(table.rowCount()))
        check("the size column is formatted for a human",
              table.item(0, 3).text() == "268.00 KiB", table.item(0, 3).text())
        check("rows sort by the value, not the text",
              table.item(0, 3) < table.item(1, 3))
        table.selectRow(1)
        selected = table.selected_partition()
        check("a selected row yields its partition record",
              selected is not None and selected.name == "system", str(selected))
        table.set_table(None)
        check("clearing the view removes the rows", table.rowCount() == 0)

    check("the tab is in the window", tabs.tabText(index) == "Qualcomm")

    print("\n7b. the progress bar behaves for a real transfer")
    from huaxin.ui.widgets import JobProgressBar

    bar = JobProgressBar()
    check("it starts hidden", not bar.isVisible())
    bar.begin("Flashing boot.img…")
    check("it appears when a transfer starts", bar.isVisible())
    check("it starts at zero", bar._bar.value() == 0, str(bar._bar.value()))
    bar.set_progress(42, "42% done")
    check("a percentage moves the bar", bar._bar.value() == 42, str(bar._bar.value()))
    check("the message is shown", "42% done" in bar._label.text(), bar._label.text())
    bar.set_progress(-1, "working")
    check("an unknown duration switches to the busy animation", bar._bar.maximum() == 0,
          str(bar._bar.maximum()))
    bar.set_progress(100, "")
    check("a percentage switches it back to determinate", bar._bar.maximum() == 100,
          str(bar._bar.maximum()))
    bar.finish("done")
    check("finishing leaves it full", bar._bar.value() == 100 and bar._label.text() == "done")
    bar.reset()
    check("resetting hides it again", not bar.isVisible())

    window.close()

    print("\n8. GPT parsing across the binding boundary")
    check("the GPT types are exposed",
          all(hasattr(huaxin_core, name)
              for name in ("GptGuid", "GptHeader", "GptEntry", "GptTable")),
          "missing: "
          + str([n for n in ("GptGuid", "GptHeader", "GptEntry", "GptTable")
                 if not hasattr(huaxin_core, n)]))

    header_bytes, entries_bytes = _build_gpt()
    table = huaxin_core.parse_gpt(header_bytes, entries_bytes)
    check("the header parses", table.header.revision_string == "1.0",
          table.header.revision_string)
    check("the entry array location is read from the header",
          table.header.part_entry_lba == 2, str(table.header.part_entry_lba))
    check("the disk GUID is rendered canonically",
          table.header.disk_guid.to_string() == "deadbeef-1111-2222-aabb-ccddeeff0011",
          table.header.disk_guid.to_string())
    check("entries are decoded with their UTF-16LE names",
          [e.name for e in table.used_entries()] == ["boot", "system"],
          str([e.name for e in table.entries]))
    check("unused slots are kept, not dropped",
          len(table.entries) == 4 and len(table.used_entries()) == 2,
          f"{len(table.used_entries())} used of {len(table.entries)}")
    check("an unused slot is identifiable", table.entries[2].is_unused)
    check("the sector count is inclusive", table.entries[0].sector_count == 67,
          str(table.entries[0].sector_count))
    check("size_bytes scales with the sector size",
          table.used_entries()[1].size_bytes(4096) == 4900 * 4096,
          str(table.used_entries()[1].size_bytes(4096)))

    found = table.find("system")
    check("a partition can be found by name", found is not None and found.first_lba == 101,
          str(found))
    check("a name that is not there returns None", table.find("nope") is None)
    # Regression: the returned pointer aims into the table's own vector, so it
    # must not be copied out from under it.
    del table
    check("a found entry survives the table going out of scope", found.name == "system",
          found.name)

    try:
        huaxin_core.parse_gpt_header(b"\x00" * 512)
    except huaxin_core.ProtocolError as exc:
        check("a missing GPT signature raises ProtocolError", True, str(exc)[:50])
    else:
        check("a missing GPT signature raises ProtocolError", False)

    check("utf16le_to_utf8 decodes and stops at the terminator",
          huaxin_core.utf16le_to_utf8("boot".encode("utf-16-le") + b"\x00\x00\x00\x00") == "boot")

    print("\n9. storage geometry out of a <getstorageinfo> reply")
    reply = huaxin_core.parse_firehose_response(
        '<?xml version="1.0" ?><data>'
        '<log value="{&quot;storage_info&quot;:{&quot;total_blocks&quot;:61071360,'
        '&quot;block_size&quot;:512,&quot;storage_type&quot;:&quot;UFS&quot;}}"/>'
        '<response value="ACK"/></data>'
    )
    info = huaxin_core.extract_storage_info(reply)
    check("the geometry comes out of the escaped JSON", info is not None, str(info))
    check("total_blocks is parsed", info.total_blocks == 61071360, str(info.total_blocks))
    check("block_size is parsed", info.block_size == 512, str(info.block_size))
    check("the storage type is carried through", info.storage_type == "UFS", info.storage_type)
    check("total_bytes multiplies the two", info.total_bytes == 61071360 * 512)
    check("the raw JSON is kept for the fields not modelled",
          "storage_info" in info.raw_json, info.raw_json[:60])

    ack_only = huaxin_core.parse_firehose_response(
        '<?xml version="1.0" ?><data><response value="ACK"/></data>'
    )
    check("a bare ACK yields no geometry rather than an invented one",
          huaxin_core.extract_storage_info(ack_only) is None)
    check("json_scalar returns None for a member that is not there",
          huaxin_core.json_scalar(info.raw_json, "nope") is None)

    print("\n10. rawprogram and patch files")
    entries = huaxin_core.parse_rawprogram_xml(_RAWPROGRAM)
    check("every <program> element is read", len(entries) == 2, str(len(entries)))
    first = entries[0].program
    check("the sector geometry survives the parse",
          (first.start_sector, first.num_partition_sectors, first.sector_size_in_bytes)
          == (0, 6, 4096),
          f"{first.start_sector}/{first.num_partition_sectors}/{first.sector_size_in_bytes}")
    check("the GPT label is kept", entries[1].label == "boot", entries[1].label)
    check("the file name is kept", entries[1].program.filename == "boot.img")

    try:
        huaxin_core.parse_rawprogram_xml("<data><erase/></data>")
    except huaxin_core.ProtocolError as exc:
        check("a file with no <program> element is rejected", "no <program>" in str(exc),
              str(exc)[:50])
    else:
        check("a file with no <program> element is rejected", False)

    patch = huaxin_core.PatchEntry()
    patch.filename = "boot.img"
    patch.value = "HUAXIN-TEST"
    patch.start_sector = 48
    patch.byte_offset = 8
    patch.size_in_bytes = 12
    patch.sector_size_in_bytes = 4096
    patch_xml = huaxin_core.build_patch_xml(patch)
    check("the patch command carries every attribute",
          all(f'{key}="' in patch_xml
              for key in ("filename", "value", "start_sector", "byte_offset", "size_in_bytes")),
          patch_xml)
    check("a patch value cannot escape its attribute",
          "&quot;" not in patch_xml or "HUAXIN-TEST" in patch_xml)
    round_tripped = huaxin_core.parse_patch_xml(
        "<data>" + patch_xml[patch_xml.index("<patch") :] + "</data>"
    )
    check("the patch round-trips through build and parse",
          len(round_tripped) == 1
          and round_tripped[0].filename == "boot.img"
          and round_tripped[0].value == "HUAXIN-TEST"
          and round_tripped[0].byte_offset == 8,
          str(round_tripped[0]) if round_tripped else "empty")

    print("\n11. a package on disk is planned before it is written")
    with tempfile.TemporaryDirectory() as folder:
        package = Path(folder)
        (package / "rawprogram0.xml").write_text(_RAWPROGRAM, encoding="utf-8")
        (package / "boot.img").write_bytes(b"\x00" * 4096)

        steps = qualcomm.describe_rawprogram(package / "rawprogram0.xml")
        check("the steps keep the package's order", [s.label for s in steps] == ["PrimaryGPT", "boot"],
              str([s.label for s in steps]))
        check("a relative image path resolves next to the XML",
              steps[1].path == package / "boot.img", str(steps[1].path))
        check("a file that is there is reported as present", steps[1].exists)
        check("a file that is missing is reported, not dropped", not steps[0].exists)
        check("the claimed size is sectors times sector size",
              steps[1].size_bytes == 16384 * 4096, str(steps[1].size_bytes))

        ctx = FakeContext()
        try:
            qualcomm.flash_rawprogram(ctx, package / "rawprogram0.xml")
        except ValueError as exc:
            check("a package with a missing image is refused outright", "gpt_main0.bin" in str(exc),
                  str(exc)[:70])
            check("nothing is written when a file is missing",
                  not any("flashing" in line for line in ctx.lines()))
        else:
            check("a package with a missing image is refused outright", False)

    print("\n12. progress is coalesced, not flooded")
    total = 2 * 1024 * 1024
    ctx = FakeContext()
    report = qualcomm._attach_progress(ctx, "boot.img", total)
    report(0, total)
    check("the first report is emitted", len(ctx.lines("progress")) == 1,
          str(ctx.lines("progress")))
    report(total // 2, total)
    check("a second report inside the interval is coalesced",
          len(ctx.lines("progress")) == 1, str(ctx.lines("progress")))
    report(total, total)
    check("the final report is always emitted", len(ctx.lines("progress")) == 2,
          str(ctx.lines("progress")))
    check("the message carries both counts, scaled to the size",
          ctx.lines("progress")[0].endswith("0 B of 2.0 MiB")
          and ctx.lines("progress")[1] == f"100% boot.img: 2.0 MiB of 2.0 MiB",
          str(ctx.lines("progress")))

    # A reporter that raises must not take the flash down with it: the C++ side
    # calls it between payload chunks, and an exception there would abort a write.
    class BrokenContext(FakeContext):
        def progress(self, percent: int, message: str = "") -> None:
            raise RuntimeError("the UI went away mid-flash")

    broken = qualcomm._attach_progress(BrokenContext(), "boot.img", total)
    try:
        broken(0, total)
        broken(total, total)
    except Exception as exc:  # noqa: BLE001
        check("a failing progress reporter does not raise into the transfer", False, str(exc))
    else:
        check("a failing progress reporter does not raise into the transfer", True)

    print("\n13. the progress callback survives the job boundary")
    edl = huaxin_core.QualcommEdl()
    seen: list[tuple[int, int]] = []
    edl.set_progress_callback(lambda done, total: seen.append((done, total)))
    edl.set_progress_callback(None)
    edl.set_progress_callback(lambda done, total: seen.append((done, total)))
    check("a callback can be installed, cleared and replaced", True)
    edl.disconnect()

    print("\n14. the sector geometry reaches the GPT read")
    # A stub session, so the ordering between the geometry query and the GPT
    # read can be checked without a device. A 4096-byte device read as 512 would
    # produce a table that is wrong by a factor of eight without looking wrong,
    # so this is the check that the number the device reports is the one used.
    session, calls = _stub_session(block_size=4096)
    qualcomm._install(session)
    try:
        check("the stub session reports itself open", qualcomm.session_open())
        ctx = FakeContext()
        table = qualcomm.read_gpt(ctx)

        check("the geometry was queried before the read",
              calls[0][0] == "get_storage_info", str([c[0] for c in calls]))
        check("the GPT was read with the device's own sector size",
              calls[1] == ("read_gpt", 0, 4096), str(calls[1]))
        check("the table carries that sector size onward", table.sector_size == 4096,
              str(table.sector_size))
        check("partition sizes are computed at that sector size",
              table.used[0].size_bytes == 8 * 4096, str(table.used[0].size_bytes))
        check("the partitions are flattened for the UI",
              [p.display_name for p in table.used] == ["boot", "system"],
              str([p.display_name for p in table.used]))
        check("an unused slot is excluded from the UI view", len(table.partitions) == 3,
              str(len(table.partitions)))

        # A second read must reuse the cached geometry rather than ask again.
        calls.clear()
        qualcomm.read_gpt(ctx)
        check("a second read asks the device nothing",
              [c[0] for c in calls] == ["read_gpt"], str([c[0] for c in calls]))

        # An explicit size overrides the cache.
        calls.clear()
        qualcomm.read_gpt(ctx, 1, 512)
        check("an explicit sector size is used as given",
              calls == [("read_gpt", 1, 512)], str(calls))
    finally:
        qualcomm.release_session(FakeContext(), quiet=True)
    check("the session is closed again", not qualcomm.session_open())

    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("\nFailures:")
        for _, description, detail in failed:
            print(f"  - {description} {detail}")
        return 1
    print("Qualcomm EDL integration OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
