"""Qualcomm EDL operations, driven from the UI.

Same shape as the adb wrapper: every function takes the worker's job context as
its first argument and matches the `fn(ctx, ...)` contract of a Job. The protocol
itself lives in C++; this module only binds the callbacks, turns the results into
log lines, and enforces the ordering the device requires.

The one rule worth stating plainly: **a Firehose command cannot work before a
programmer has been uploaded**. Sahara uploads it, the device then switches from
"asking for an image" to "answering Firehose", and only then is the device
addressable with XML. Every entry point here checks that, and says so rather than
sending a command into the void.

The session is persistent
-------------------------
That ordering is why a Firehose session is kept alive between jobs instead of
being opened per action. Uploading a programmer takes seconds and resets the
storage engine, so configure -> read GPT -> flash has to happen inside one
session; rebuilding it per click would make the sequence impossible.

The live session is a module global, and it belongs to the worker thread: it is
created, used and released there and nowhere else. The lock around it is cheap
insurance, not a licence to touch it from the UI thread.
"""

from __future__ import annotations

import math
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

__all__ = [
    "DeviceIdentity",
    "EdlNotReadyError",
    "EDL_VID",
    "EDL_PID",
    "MEMORY_NAME_CHOICES",
    "PartitionRecord",
    "PartitionTable",
    "StorageGeometry",
    "apply_patch",
    "build_read_command",
    "configure",
    "describe_rawprogram",
    "device_present",
    "erase_partition",
    "flash_partition",
    "flash_firmware",
    "flash_rawprogram",
    "load_programmer",
    "memory_name_choices",
    "power_reset",
    "read_device_info",
    "read_gpt",
    "read_partition",
    "release_session",
    "session_open",
    "storage_info",
    "verify_toolchain",
]

EDL_VID = 0x05C6
EDL_PID = 0x9008

#: Qualcomm's EDL identity in the VID/PID catalogue.
EDL_USB_ID = "05c6:9008"

_DEFAULT_CONFIGURE_TIMEOUT_MS = 5000
_IDENTITY_TIMEOUT_MS = 30000
#: Used when the device does not report a block size of its own. Every eMMC/UFS
#: logical sector this tool has a spec for is 512 bytes; a device that differs
#: says so through getstorageinfo.
_DEFAULT_SECTOR_SIZE = 512
#: The C++ read path holds the whole transfer in memory and refuses more.
_MAX_READ_BYTES = 512 * 1024 * 1024

#: Progress lines to the UI are coalesced to this interval. A write reports per
#: megabyte, so a large image would otherwise put hundreds of events on the Qt
#: event loop for a progress bar nobody can read that fast.
_PROGRESS_INTERVAL_S = 0.1

#: What may be sent as <configure MemoryName="...">.
#:
#: PROVENANCE: the storage *types* come from qdl's `enum qdl_storage_type`, but
#: the strings its `encode_storage_type()` maps them to live in a file that is
#: not vendored in this project, so the spellings below are NOT verified against
#: a primary source and are offered as candidates rather than applied as
#: defaults. The field is editable for that reason: if a target refuses the
#: label, the correct string is whatever its programmer accepts, and sending it
#: must not require editing the tool. Leaving it empty omits the attribute
#: entirely, which lets the programmer use its own default - the safe choice
#: when the storage type is not known.
MEMORY_NAME_CHOICES = ("", "eMMC", "UFS", "nand", "NVMe", "spi")

#: The two the task named, for the UI to offer first.
_MEMORY_NAME_COMMON = ("", "eMMC", "UFS")


class EdlNotReadyError(RuntimeError):
    """An action was attempted out of order."""


def _native() -> Any:
    """The compiled module, imported lazily so this file stays importable
    (and its docs readable) before the extension has been built."""
    if str(Path(__file__).resolve().parents[2]) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import huaxin_core  # noqa: PLC0415

    return huaxin_core


# -- value types ---------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """Chip identity read over Sahara command mode."""

    protocol_version: int
    serial: str
    hardware_id: str
    msm_id: int
    oem_id: int
    model_id: int
    pk_hash: str
    chip_name: str

    @property
    def has_hardware_id(self) -> bool:
        return self.msm_id != 0 or self.oem_id != 0 or self.model_id != 0

    @classmethod
    def from_native(cls, info: Any) -> "DeviceIdentity":
        return cls(
            protocol_version=int(info.protocol_version),
            serial=f"{info.serial:#010x}" if info.have_serial else "",
            hardware_id=f"{info.hardware_id:#018x}" if info.have_hardware_id else "",
            msm_id=int(info.msm_id) if info.have_hardware_id else 0,
            oem_id=int(info.oem_id) if info.have_hardware_id else 0,
            model_id=int(info.model_id) if info.have_hardware_id else 0,
            pk_hash=str(info.pk_hash),
            chip_name=str(info.chip_name),
        )


@dataclass(frozen=True, slots=True)
class StorageGeometry:
    """What the programmer reports about the flash it is driving."""

    lun: int
    total_blocks: int
    block_size: int
    storage_type: str
    raw_json: str

    @property
    def total_bytes(self) -> int:
        return self.total_blocks * self.block_size

    @property
    def is_known(self) -> bool:
        """False when the device answered getstorageinfo without any geometry."""
        return self.total_blocks > 0 and self.block_size > 0

    @classmethod
    def from_native(cls, info: Any, lun: int) -> "StorageGeometry":
        return cls(
            lun=int(lun),
            total_blocks=int(info.total_blocks),
            block_size=int(info.block_size),
            storage_type=str(info.storage_type),
            raw_json=str(info.raw_json),
        )


@dataclass(frozen=True, slots=True)
class PartitionRecord:
    """One GPT entry, flattened for display."""

    index: int
    name: str
    first_sector: int
    last_sector: int
    sector_count: int
    size_bytes: int
    type_guid: str
    unique_guid: str
    is_unused: bool

    @property
    def display_name(self) -> str:
        return self.name or "(unnamed)"

    @classmethod
    def from_native(cls, entry: Any, index: int, sector_size: int) -> "PartitionRecord":
        return cls(
            index=index,
            name=str(entry.name),
            first_sector=int(entry.first_lba),
            last_sector=int(entry.last_lba),
            sector_count=int(entry.sector_count),
            size_bytes=int(entry.size_bytes(sector_size)),
            type_guid=entry.type_guid.to_string(),
            unique_guid=entry.unique_guid.to_string(),
            is_unused=bool(entry.is_unused),
        )


@dataclass(frozen=True, slots=True)
class PartitionTable:
    """A parsed GPT plus the geometry it was read with."""

    lun: int
    sector_size: int
    revision: str
    disk_guid: str
    entry_slots: int
    partitions: tuple[PartitionRecord, ...]

    @property
    def used(self) -> tuple[PartitionRecord, ...]:
        return tuple(entry for entry in self.partitions if not entry.is_unused)

    def find(self, name: str) -> PartitionRecord | None:
        for entry in self.partitions:
            if not entry.is_unused and entry.name == name:
                return entry
        return None


# -- the live session ----------------------------------------------------------
class _LiveSession:
    """The one open programmer session.

    Only ever touched on the worker thread. `geometry` caches the last
    getstorageinfo answer so a read or a write does not have to ask again.
    """

    __slots__ = ("native", "edl", "geometry")

    def __init__(self, native: Any, edl: Any) -> None:
        self.native = native
        self.edl = edl
        self.geometry: StorageGeometry | None = None

    def sector_size(self) -> int:
        if self.geometry is not None and self.geometry.block_size:
            return self.geometry.block_size
        return _DEFAULT_SECTOR_SIZE

    def close(self) -> None:
        try:
            self.edl.disconnect()
        except Exception:  # a dead link is the normal case here, not an error
            pass


_session_lock = threading.Lock()
_live: _LiveSession | None = None


def session_open() -> bool:
    """True when a programmer is loaded and Firehose commands will be accepted."""
    return _live is not None


def _take() -> _LiveSession:
    if _live is None:
        raise EdlNotReadyError(
            "no Firehose session is open: upload a programmer first "
            "(Load Firehose Programmer…), then run this again. A Firehose command "
            "cannot be sent to a device that is still waiting for its programmer."
        )
    return _live


def _install(session: _LiveSession | None) -> None:
    """Replaces the live session, closing whatever was there before."""
    global _live
    with _session_lock:
        previous, _live = _live, session
    if previous is not None and previous is not session:
        previous.close()


def release_session(ctx: Any, *, quiet: bool = False) -> bool:
    """Close the Firehose session. Safe to call when none is open.

    Dropping the USB handle does not reset the device, but the programmer is
    left running with nobody talking to it, so the next action needs a fresh
    upload either way.
    """
    with _session_lock:
        had_session = _live is not None
    _install(None)
    if had_session and not quiet:
        ctx.log("Firehose session closed", "info")
    return had_session


def _new_session(ctx: Any) -> _LiveSession:
    """Opens a session whose callbacks forward to the job context.

    The C++ side invokes these from the thread that called it, so they arrive
    with the GIL re-acquired - which is what lets `ctx.log` touch the worker's
    signal machinery.
    """
    native = _native()
    edl = native.QualcommEdl(
        log=lambda level, message: ctx.log(message, level),
        progress=lambda percent, message: ctx.progress(percent, message),
        cancelled=lambda: bool(ctx.cancelled),
    )
    return _LiveSession(native, edl)


def _attach_progress(ctx: Any, label: str, total_hint: int = 0) -> Callable[[int, int], None]:
    """Installs a throttle that turns C++ byte counts into UI progress.

    A raise in a callback must not abort a flash, so anything that goes wrong
    here is swallowed and logged once rather than propagated.
    """
    state = {"last": 0.0, "percent": -1, "failed": False}

    def report(done: int, total: int) -> None:
        if state["failed"]:
            return
        try:
            span = total or total_hint
            percent = int(min(100, (done * 100) // span)) if span else -1
            now = time.monotonic()
            final = percent >= 100
            if not final and percent == state["percent"]:
                return
            if not final and (now - state["last"]) < _PROGRESS_INTERVAL_S and percent >= 0:
                return
            state["last"] = now
            state["percent"] = percent
            ctx.progress(
                percent,
                f"{label}: {_human_bytes(done)} of {_human_bytes(span)}",
            )
        except Exception:  # noqa: BLE001 - never let reporting break the transfer
            state["failed"] = True

    return report


def _human_bytes(count: float) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def device_present() -> bool:
    """True when a 05c6:9008 device is on the bus. Safe to call on the UI thread:
    it opens a libusb context and enumerates, nothing more."""
    return bool(_native().QualcommEdl.device_present())


def memory_name_choices() -> tuple[str, ...]:
    """The MemoryName candidates, most likely first.

    The common two come first because the UI offers them by default; the rest
    are here so an unusual target does not need the tool to be edited.
    """
    rest = tuple(name for name in MEMORY_NAME_CHOICES if name not in _MEMORY_NAME_COMMON)
    return _MEMORY_NAME_COMMON + rest


# -- standalone actions --------------------------------------------------------
def verify_toolchain(ctx: Any) -> None:
    """Report whether an EDL device is visible and whether it can be opened."""
    native = _native()
    if not native.QualcommEdl.device_present():
        ctx.log(
            f"no device with USB ID {EDL_USB_ID} is on the bus. Put the target into EDL "
            "mode first - from adb: 'adb reboot edl'.",
            "warn",
        )
        return
    ctx.log(f"found a device with USB ID {EDL_USB_ID}", "ok")

    if _live is not None:
        ctx.log(f"a Firehose session is already open: {_live.edl.describe()}", "ok")
        return

    edl = _new_session(ctx)
    try:
        edl.edl.connect()
        ctx.log(f"link open: {edl.edl.describe()}", "ok")
    finally:
        edl.close()


def read_device_info(ctx: Any) -> DeviceIdentity:
    """Sahara: read the chip identity without uploading anything.

    The device is returned to its initial EDL state afterwards, so this is safe
    to run repeatedly and safe to run before a programmer upload. It is refused
    while a session is open, because a Sahara conversation would reset the
    programmer that session is using.
    """
    if _live is not None:
        raise EdlNotReadyError(
            "a Firehose session is open; reading chip identity would re-enter Sahara "
            "and drop the programmer. Close the session first."
        )

    ctx.log("reading chip identity over Sahara (no programmer is uploaded)", "info")
    edl = _new_session(ctx)
    try:
        info = DeviceIdentity.from_native(edl.edl.read_device_info())
    finally:
        edl.close()

    _log_identity(ctx, info)
    return info


def _log_identity(ctx: Any, info: DeviceIdentity) -> None:
    if info.serial:
        ctx.log(f"chip serial: {info.serial}", "ok")
    else:
        ctx.log("the device did not report a serial number", "warn")

    if info.has_hardware_id:
        ctx.log(
            f"MSM id {info.msm_id:#010x}, OEM id {info.oem_id:#06x}, "
            f"model id {info.model_id:#06x} (raw HW id {info.hardware_id})",
            "ok",
        )
    else:
        ctx.log("the device did not report a hardware id", "warn")

    if info.pk_hash:
        ctx.log(f"OEM PK hash: {info.pk_hash}", "ok")
    else:
        ctx.log(
            "no OEM PK hash was returned. Some firmware refuses this command, and some "
            "devices pad or repeat the digest; it is not required for flashing.",
            "warn",
        )

    if not info.chip_name:
        ctx.log(
            "MSM id to chip name mapping is not implemented yet, so the SoC is identified "
            "by its numeric ids only.",
            "debug",
        )


def load_programmer(ctx: Any, path: str | Path, read_identity: bool = True) -> DeviceIdentity:
    """Sahara: upload a Firehose programmer and open a session.

    The session stays open so configure/read/write can follow; it is closed by
    the release button, by a failed action, or by the next upload.
    """
    programmer = Path(path)
    if not programmer.is_file():
        raise ValueError(f"programmer file does not exist: {programmer}")

    if _live is not None:
        ctx.log("closing the existing Firehose session before a new upload", "info")
        release_session(ctx, quiet=True)

    size_kb = programmer.stat().st_size / 1024
    ctx.log(f"uploading programmer {programmer.name} ({size_kb:.0f} KiB)", "info")

    session = _new_session(ctx)
    try:
        info = DeviceIdentity.from_native(
            session.edl.load_programmer(str(programmer), bool(read_identity))
        )
        loaded = session.edl.programmer_loaded()
    except BaseException:
        session.close()
        raise

    if not loaded:
        session.close()
        raise EdlNotReadyError("the programmer upload did not complete")

    _install(session)
    if read_identity:
        _log_identity(ctx, info)
    ctx.log("the device is in Firehose mode and will now accept commands", "ok")
    return info


# -- Firehose session commands -------------------------------------------------
def configure(
    ctx: Any,
    memory_name: str = "",
    max_payload_bytes: int = 1024 * 1024,
) -> None:
    """Firehose <configure>. Must be the first command after a programmer loads."""
    session = _take()
    native = session.native
    request = native.ConfigureRequest()
    request.memory_name = memory_name
    request.max_payload_size_to_target = max_payload_bytes

    xml = native.build_configure_xml(request)
    ctx.log(f"sending {xml}", "debug")

    response = session.edl.configure(request, _DEFAULT_CONFIGURE_TIMEOUT_MS)
    _report_response(ctx, "configure", response)
    if not response.acknowledged:
        raise EdlNotReadyError("the programmer refused <configure>")

    if memory_name:
        ctx.log(f"storage type sent as MemoryName={memory_name!r}", "ok")
        ctx.log(
            "the spelling of MemoryName is not verified in this build (docs/firehose-commands.md). "
            "If the programmer refuses <configure>, try the other spelling or leave it empty.",
            "warn",
        )
    else:
        ctx.log(
            "no MemoryName was sent, so the programmer keeps its own storage default. That is "
            "the safe choice for both UFS and eMMC targets; nothing in the read, write, erase "
            "or GPT path depends on the label - the sector geometry comes from the device.",
            "warn",
        )


def storage_info(ctx: Any, lun: int = 0) -> StorageGeometry:
    """Firehose <getstorageinfo>: what the programmer sees on the storage device."""
    session = _take()
    info = session.edl.get_storage_info(int(lun))
    geometry = StorageGeometry.from_native(info, int(lun))

    if geometry.is_known:
        ctx.log(
            f"LUN {geometry.lun}: {geometry.total_blocks} blocks of "
            f"{geometry.block_size} bytes ({_human_bytes(geometry.total_bytes)})"
            + (f", {geometry.storage_type}" if geometry.storage_type else ""),
            "ok",
        )
    else:
        ctx.log(
            "the programmer reported no storage geometry. Some firmware answers "
            "getstorageinfo with a bare ACK; a 512-byte sector is assumed from here on.",
            "warn",
        )

    if geometry.storage_type:
        ctx.log(
            f"the device names its storage type {geometry.storage_type!r}; rawprogram and "
            "patch files are written against the geometry the GPT reports, not this label.",
            "debug",
        )
    session.geometry = geometry
    return geometry


def read_gpt(ctx: Any, lun: int = 0, sector_size: int = 0) -> PartitionTable:
    """Firehose: read and parse the GPT of a LUN."""
    session = _take()
    if sector_size <= 0:
        # Ask the device rather than assuming: the sector size decides which LBA
        # the entry array is read from and how every partition size is computed,
        # and a device with 4096-byte sectors read as 512 would report a table
        # that is wrong by a factor of eight without looking wrong.
        if session.geometry is None or not session.geometry.is_known:
            storage_info(ctx, lun)
        sector_size = session.sector_size()

    ctx.log(f"reading the partition table of LUN {lun} ({sector_size}-byte sectors)", "info")
    table = session.edl.read_gpt(int(lun), int(sector_size))
    used = [entry for entry in table.entries if not entry.is_unused]
    ctx.log(
        f"GPT revision {table.header.revision_string}, {len(used)} of "
        f"{len(table.entries)} slots in use, disk {table.header.disk_guid}",
        "ok",
    )

    partitions = tuple(
        PartitionRecord.from_native(entry, index, sector_size)
        for index, entry in enumerate(table.entries)
    )
    for record in partitions:
        if record.is_unused:
            continue
        ctx.log(
            f"  {record.display_name:<24} sector {record.first_sector:>10} +"
            f"{record.sector_count:<10} {_human_bytes(record.size_bytes):>10}",
            "output",
        )

    return PartitionTable(
        lun=int(lun),
        sector_size=int(sector_size),
        revision=str(table.header.revision_string),
        disk_guid=table.header.disk_guid.to_string(),
        entry_slots=len(table.entries),
        partitions=partitions,
    )


def flash_partition(
    ctx: Any,
    image_path: str | Path,
    start_sector: int,
    num_sectors: int = 0,
    *,
    sector_size: int = 0,
    physical_partition: int = 0,
    label: str = "",
) -> int:
    """Firehose <program>: write one image file to a sector range.

    Returns the number of bytes written. `num_sectors` 0 means "as many as the
    file needs, rounded up", which is what a rawprogram entry states anyway.
    """
    session = _take()
    image = Path(image_path)
    if not image.is_file():
        raise ValueError(f"image file does not exist: {image}")

    payload = image.read_bytes()
    if not payload:
        raise ValueError(f"refusing to flash an empty image: {image}")

    size = int(sector_size) or session.sector_size()
    needed = math.ceil(len(payload) / size)
    sectors = int(num_sectors) or needed
    if sectors < needed:
        ctx.log(
            f"{image.name} needs {needed} sectors of {size} bytes but the entry claims "
            f"{sectors}; using the file's own size",
            "warn",
        )
        sectors = needed

    request = session.native.ProgramRequest()
    request.sector_size_in_bytes = size
    request.physical_partition_number = int(physical_partition)
    request.start_sector = int(start_sector)
    request.num_partition_sectors = sectors
    request.filename = image.name

    where = f"LUN {physical_partition} sector {start_sector}"
    ctx.log(
        f"flashing {label or image.name}: {_human_bytes(len(payload))} to {where} "
        f"({sectors} sectors of {size} bytes)",
        "info",
    )
    ctx.log(f"sending {session.native.build_program_xml(request)}", "debug")

    session.edl.set_progress_callback(_attach_progress(ctx, image.name, len(payload)))
    try:
        session.edl.program_partition(request, payload)
    finally:
        session.edl.set_progress_callback(None)

    ctx.log(f"{label or image.name}: {_human_bytes(len(payload))} written to {where}", "ok")
    return len(payload)


def read_partition(
    ctx: Any,
    destination: str | Path,
    start_sector: int,
    num_sectors: int,
    *,
    sector_size: int = 0,
    physical_partition: int = 0,
) -> int:
    """Firehose <read>: dump a sector range to a file. Returns the byte count."""
    session = _take()
    size = int(sector_size) or session.sector_size()
    total = int(num_sectors) * size
    if total <= 0:
        raise ValueError("the read range is empty")
    if total > _MAX_READ_BYTES:
        raise ValueError(
            f"the range is {_human_bytes(total)}, above the {_human_bytes(_MAX_READ_BYTES)} "
            "this build holds in memory. Read it in parts."
        )

    request = session.native.ReadRequest()
    request.sector_size_in_bytes = size
    request.physical_partition_number = int(physical_partition)
    request.start_sector = int(start_sector)
    request.num_partition_sectors = int(num_sectors)

    ctx.log(
        f"reading {_human_bytes(total)} from LUN {physical_partition} sector {start_sector}", "info"
    )
    session.edl.set_progress_callback(_attach_progress(ctx, f"sector {start_sector}", total))
    try:
        data = session.edl.read_partition(request)
    finally:
        session.edl.set_progress_callback(None)

    out = Path(destination)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    ctx.log(f"wrote {_human_bytes(len(data))} to {out}", "ok")
    return len(data)


def erase_partition(
    ctx: Any,
    start_sector: int,
    num_sectors: int,
    *,
    sector_size: int = 0,
    physical_partition: int = 0,
    label: str = "",
) -> None:
    """Firehose <erase>: erase a sector range."""
    session = _take()
    size = int(sector_size) or session.sector_size()

    request = session.native.EraseRequest()
    request.sector_size_in_bytes = size
    request.physical_partition_number = int(physical_partition)
    request.start_sector = int(start_sector)
    request.num_partition_sectors = int(num_sectors)

    what = label or f"{num_sectors} sectors"
    ctx.log(
        f"erasing {what} at LUN {physical_partition} sector {start_sector} "
        f"({_human_bytes(int(num_sectors) * size)})",
        "warn",
    )
    session.edl.erase_sectors(request)
    ctx.log(f"erase command accepted for {what}", "ok")


def power_reset(ctx: Any, delay_seconds: int = 10) -> None:
    """Firehose <power value="reset">: restart the device out of EDL."""
    session = _take()
    request = session.native.PowerRequest()
    request.value = "reset"
    request.delay_seconds = int(delay_seconds)

    ctx.log("requesting a device reset", "info")
    try:
        response = session.edl.power(request)
        _report_response(ctx, "power", response)
    finally:
        # Whatever the device did with it, this session is over.
        release_session(ctx, quiet=True)
    ctx.log("the device is restarting; the Firehose session is closed", "ok")


def send_firehose(ctx: Any, xml: str, timeout_ms: int = 10000) -> None:
    """Send one hand-written XML document. For diagnostics."""
    if not xml.strip():
        raise ValueError("the XML command is empty")
    session = _take()
    ctx.log(f"sending {xml}", "info")
    _report_response(ctx, "command", session.edl.send_firehose(xml, int(timeout_ms)))


def _report_response(ctx: Any, label: str, response: Any) -> None:
    for line in response.logs:
        ctx.log(f"{label}: {line}", "output")
    if response.acknowledged:
        ctx.log(
            f"{label}: ACK" + (" (the device is switching to raw data)" if response.raw_mode else ""),
            "ok",
        )
    else:
        ctx.log(f"{label}: {response.status.name.upper()} - the device refused the command", "error")


# -- rawprogram / patch files --------------------------------------------------
@dataclass(frozen=True, slots=True)
class RawProgramStep:
    """One <program> element resolved against the filesystem."""

    index: int
    label: str
    filename: str
    path: Path
    start_sector: int
    num_sectors: int
    sector_size: int
    physical_partition: int
    exists: bool

    @property
    def size_bytes(self) -> int:
        return self.num_sectors * self.sector_size


def describe_rawprogram(rawprogram_path: str | Path) -> tuple[RawProgramStep, ...]:
    """Parses a rawprogramN.xml into steps, without touching a device.

    Image paths are resolved relative to the XML's own directory, which is how
    Qualcomm's packages are laid out: the XML sits next to the .img files it
    names. A step whose file is missing is reported rather than dropped, so the
    operator sees the whole plan before anything is written.
    """
    native = _native()
    xml_path = Path(rawprogram_path)
    if not xml_path.is_file():
        raise ValueError(f"rawprogram file does not exist: {xml_path}")

    entries = native.parse_rawprogram_xml(xml_path.read_text(encoding="utf-8", errors="replace"))
    base = xml_path.parent

    steps: list[RawProgramStep] = []
    for index, entry in enumerate(entries):
        program = entry.program
        # The XML may carry a path of its own; a relative one is relative to the XML.
        candidate = Path(program.filename)
        path = candidate if candidate.is_absolute() else base / candidate
        steps.append(
            RawProgramStep(
                index=index,
                label=str(entry.label),
                filename=str(program.filename),
                path=path,
                start_sector=int(program.start_sector),
                num_sectors=int(program.num_partition_sectors),
                sector_size=int(program.sector_size_in_bytes),
                physical_partition=int(program.physical_partition_number),
                exists=path.is_file(),
            )
        )
    return tuple(steps)


def flash_rawprogram(
    ctx: Any,
    rawprogram_path: str | Path,
    *,
    only: Iterable[str] | None = None,
    dry_run: bool = False,
) -> int:
    """Replays a rawprogramN.xml through Firehose. Returns the number of images written.

    `only` restricts the run to the named labels/files, which is what the UI
    passes when the operator selects a subset. The whole file is still parsed, so
    the ordering and the sector addresses are the ones the package shipped.
    """
    steps = describe_rawprogram(rawprogram_path)
    wanted = {name for name in only} if only is not None else None

    if not steps:
        raise ValueError(f"{Path(rawprogram_path).name} lists no partitions")

    missing = [step for step in steps if not step.exists]
    ctx.log(
        f"{Path(rawprogram_path).name}: {len(steps)} partition(s) listed"
        + (f", {len(missing)} image file(s) missing" if missing else ""),
        "warn" if missing else "info",
    )
    for step in steps:
        mark = "ok" if step.exists else "error"
        ctx.log(
            f"  [{step.index:>2}] {step.label or step.filename:<24} "
            f"{_human_bytes(step.size_bytes):>10} at sector {step.start_sector}"
            + ("" if step.exists else f"   MISSING: {step.path}"),
            mark,
        )

    if missing:
        names = ", ".join(step.filename for step in missing[:5])
        raise ValueError(
            f"{len(missing)} image file(s) named by {Path(rawprogram_path).name} are not "
            f"next to it: {names}. Nothing was written."
        )
    if dry_run:
        ctx.log("dry run: the device was not touched", "info")
        return 0

    written = 0
    for step in steps:
        if wanted is not None and step.label not in wanted and step.filename not in wanted:
            continue
        ctx.check_cancelled()
        flash_partition(
            ctx,
            step.path,
            step.start_sector,
            step.num_sectors,
            sector_size=step.sector_size,
            physical_partition=step.physical_partition,
            label=step.label or step.filename,
        )
        written += 1

    ctx.log(f"{Path(rawprogram_path).name}: {written} partition(s) written", "ok")
    return written


def flash_firmware(
    ctx: Any,
    rawprogram_path: str | Path,
    patch_path: str | Path | None = None,
    *,
    dry_run: bool = False,
) -> int:
    """A whole package: every <program> in rawprogramN.xml, then patchN.xml.

    The order is not a preference. A patch writes into an image that is already
    on the flash, so applying one before its partition has been programmed
    writes into whatever was there before.
    """
    written = flash_rawprogram(ctx, rawprogram_path, dry_run=dry_run)
    if patch_path:
        if dry_run:
            ctx.log(f"dry run: {Path(patch_path).name} was not applied", "info")
        else:
            apply_patch(ctx, patch_path)
    return written


def apply_patch(ctx: Any, patch_path: str | Path) -> int:
    """Replays a patchN.xml: values written into images that are already flashed.

    This is the step that stores things like the bootloader version into a
    partition after it has been programmed, so it runs *after* the rawprogram,
    never before.
    """
    session = _take()
    native = session.native
    xml_path = Path(patch_path)
    if not xml_path.is_file():
        raise ValueError(f"patch file does not exist: {xml_path}")

    entries = native.parse_patch_xml(xml_path.read_text(encoding="utf-8", errors="replace"))
    if not entries:
        ctx.log(f"{xml_path.name} contains no <patch> elements; nothing to do", "warn")
        return 0

    ctx.log(f"applying {len(entries)} patch(es) from {xml_path.name}", "info")
    applied = 0
    for index, entry in enumerate(entries):
        ctx.check_cancelled()
        if entry.size_in_bytes and len(entry.value) > entry.size_in_bytes:
            ctx.log(
                f"patch {index}: value is {len(entry.value)} bytes but only "
                f"{entry.size_in_bytes} are claimed; skipping rather than truncating",
                "error",
            )
            continue

        response = session.edl.patch(entry)
        _report_response(ctx, f"patch {index} ({entry.filename})", response)
        if response.acknowledged:
            applied += 1

    ctx.log(f"{xml_path.name}: {applied} of {len(entries)} patch(es) applied", "ok")
    return applied


# -- Firehose XML preview ------------------------------------------------------
def build_read_command(
    sector_size: int,
    num_sectors: int,
    partition_number: int,
    start_sector: int,
    filename: str = "",
) -> str:
    """Builds a <read> document without sending it, so the exact command the
    device will receive can be inspected first."""
    native = _native()
    request = native.ReadRequest()
    request.sector_size_in_bytes = int(sector_size)
    request.num_partition_sectors = int(num_sectors)
    request.physical_partition_number = int(partition_number)
    request.start_sector = int(start_sector)
    request.filename = filename
    return native.build_read_xml(request)
