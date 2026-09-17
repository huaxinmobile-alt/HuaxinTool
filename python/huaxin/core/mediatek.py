"""MediaTek BROM and download agent operations, driven from the UI.

Same shape as the other protocol wrappers: each function takes the worker's job
context first and matches the `fn(ctx, ...)` contract of a Job. The protocol is
in C++; this file binds the callbacks, turns results into log lines, and states
the ordering the device requires.

Two protocols, one cable
------------------------
The bootrom can handshake, report what it is, receive a download agent and jump
to it. That is all it can do - it cannot read or write the flash. Flashing is the
*agent's* protocol, a different conversation on the same wire, and the order is
strict:

    handshake → identity → SEND_DA → JUMP_DA → configure → read/write/erase

So the session is persistent. Uploading an agent takes seconds and resets the
storage engine, and every flash operation needs the agent that upload left
running; rebuilding the session per click would make the sequence impossible.
The live session is a module global and belongs to the worker thread - created,
used and released there and nowhere else.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "BROM_USB_IDS",
    "ChipInfo",
    "DEFAULT_DA_ADDRESS",
    "FlashOutcome",
    "FlashReport",
    "FlashSummary",
    "MTK_VID",
    "PartitionRecord",
    "ScatterInfo",
    "TargetSecurity",
    "devices_present",
    "flash_firmware",
    "format_flash",
    "load_download_agent",
    "load_scatter",
    "read_chip_info",
    "read_flash_info",
    "read_target_config",
    "readback_partition",
    "release_session",
    "session_open",
    "shutdown_device",
    "verify_toolchain",
]

MTK_VID = 0x0E8D
#: The three USB IDs a MediaTek device advertises depending on boot stage.
BROM_USB_IDS = ("0e8d:0003", "0e8d:2000", "0e8d:2001")

#: Where a download agent is loaded by default. The bootrom takes the address
#: from the host, and different chip generations lay out SRAM differently, so
#: this is a starting point the operator can override - not a fact about any
#: particular chip.
DEFAULT_DA_ADDRESS = 0x00200000


def _native() -> Any:
    if str(Path(__file__).resolve().parents[2]) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import huaxin_core  # noqa: PLC0415

    return huaxin_core


class MtkNotReadyError(RuntimeError):
    """An action was attempted out of order."""


# -- value types ---------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TargetSecurity:
    """What the bootrom says about its own security configuration."""

    raw: int
    secure_boot: bool
    sla_required: bool
    da_authentication: bool
    sw_jtag: bool
    epp_supported: bool
    certificate_required: bool
    memory_read_allowed: bool
    memory_write_allowed: bool
    cmd_c8_supported: bool
    description: str

    @classmethod
    def from_native(cls, config: Any) -> "TargetSecurity":
        return cls(
            raw=int(config.raw),
            secure_boot=bool(config.secure_boot),
            sla_required=bool(config.sla_required),
            da_authentication=bool(config.da_authentication),
            sw_jtag=bool(config.sw_jtag),
            epp_supported=bool(config.epp_supported),
            certificate_required=bool(config.certificate_required),
            memory_read_allowed=bool(config.memory_read_allowed),
            memory_write_allowed=bool(config.memory_write_allowed),
            cmd_c8_supported=bool(config.cmd_c8_supported),
            description=str(config.describe()),
        )


@dataclass(frozen=True, slots=True)
class ChipInfo:
    """Identity reported by the bootrom."""

    hardware_code: int
    hardware_version: int
    hardware_code_hex: str
    hardware_version_hex: str
    hw_sub_code: int
    sw_version: int
    brom_version: int
    have_brom_version: bool
    chip_name: str

    @classmethod
    def from_native(cls, info: Any) -> "ChipInfo":
        return cls(
            hardware_code=int(info.hardware_code),
            hardware_version=int(info.hardware_version),
            hardware_code_hex=f"0x{info.hardware_code:04x}",
            hardware_version_hex=f"0x{info.hardware_version:04x}",
            hw_sub_code=int(info.hw_sw_hw_sub_code),
            sw_version=int(info.hw_sw_sw_version),
            brom_version=int(info.brom_version),
            have_brom_version=bool(info.have_brom_version),
            chip_name=str(info.chip_name),
        )


@dataclass(frozen=True, slots=True)
class PartitionRecord:
    """One scatter entry, flattened for display."""

    index: str
    name: str
    file_name: str
    is_download: bool
    start_address: int
    size: int
    region: str
    storage: str
    operation_type: str
    extras: dict[str, str]

    @property
    def display_name(self) -> str:
        return self.name or self.index or "(unnamed)"

    @property
    def is_empty_region(self) -> bool:
        """A partition with no image, which a format run clears."""
        return not self.is_download and self.size > 0

    @classmethod
    def from_native(cls, entry: Any) -> "PartitionRecord":
        return cls(
            index=str(entry.index),
            name=str(entry.name),
            file_name=str(entry.file_name),
            is_download=bool(entry.is_download),
            start_address=int(entry.start_address),
            size=int(entry.size),
            region=str(entry.region),
            storage=str(entry.storage),
            operation_type=str(entry.operation_type),
            extras={str(key): str(value) for key, value in entry.extra.items()},
        )


@dataclass(frozen=True, slots=True)
class ScatterInfo:
    """A parsed scatter file, plus where it came from."""

    path: str
    format: str
    platform: str
    project: str
    storage: str
    partitions: tuple[PartitionRecord, ...]
    unmodelled_keys: tuple[str, ...]

    @property
    def downloads(self) -> tuple[PartitionRecord, ...]:
        return tuple(entry for entry in self.partitions if entry.is_download and entry.file_name)

    @property
    def total_download_bytes(self) -> int:
        return sum(entry.size for entry in self.downloads)

    def find(self, name: str) -> PartitionRecord | None:
        lowered = name.lower()
        for entry in self.partitions:
            if entry.name.lower() == lowered or entry.index.lower() == lowered:
                return entry
        return None


@dataclass(frozen=True, slots=True)
class FlashSummary:
    """What the agent reports about the flash it is driving."""

    storage_label: str
    total_size: int
    block_size: int
    da_version: str
    connection_agent: str
    sla_enabled: bool
    emmc: Any = None
    nand: Any = None
    nor: Any = None
    ram: Any = None

    @property
    def is_ufs_or_emmc(self) -> bool:
        return self.storage_label in ("eMMC", "UFS")

    @property
    def boot_areas(self) -> dict[str, int]:
        """The eMMC hardware partitions, when the agent reported an eMMC."""
        if self.emmc is None:
            return {}
        return {
            "boot1": int(self.emmc.boot1_size),
            "boot2": int(self.emmc.boot2_size),
            "rpmb": int(self.emmc.rpmb_size),
            "gp1": int(self.emmc.gp1_size),
            "gp2": int(self.emmc.gp2_size),
            "gp3": int(self.emmc.gp3_size),
            "gp4": int(self.emmc.gp4_size),
            "user": int(self.emmc.user_size),
        }

    @classmethod
    def from_native(cls, info: Any) -> "FlashSummary":
        return cls(
            storage_label=str(info.storage_label()),
            total_size=int(info.total_size),
            block_size=int(info.block_size),
            da_version=str(info.da_version),
            connection_agent=str(info.connection_agent),
            sla_enabled=bool(info.sla_enabled),
            emmc=info.emmc if info.have_storage and str(info.storage_label()) == "eMMC" else None,
            nand=info.nand,
            nor=info.nor,
            ram=info.ram,
        )


@dataclass(frozen=True, slots=True)
class FlashOutcome:
    """One partition's fate in a flash run."""

    name: str
    file_name: str
    bytes: int
    succeeded: bool
    skipped: bool
    error: str

    @classmethod
    def from_native(cls, outcome: Any) -> "FlashOutcome":
        return cls(
            name=str(outcome.name),
            file_name=str(outcome.file_name),
            bytes=int(outcome.bytes),
            succeeded=bool(outcome.succeeded),
            skipped=bool(outcome.skipped),
            error=str(outcome.error),
        )


@dataclass(frozen=True, slots=True)
class FlashReport:
    """What a whole flash run did."""

    partitions: tuple[FlashOutcome, ...]
    completed: bool
    summary: str

    @property
    def written(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.partitions if entry.succeeded)

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.partitions if not entry.succeeded and not entry.skipped)

    @property
    def total_bytes(self) -> int:
        return sum(entry.bytes for entry in self.partitions if entry.succeeded)

    @classmethod
    def from_native(cls, result: Any) -> "FlashReport":
        return cls(
            partitions=tuple(FlashOutcome.from_native(item) for item in result.partitions),
            completed=bool(result.completed),
            summary=str(result.summary()),
        )


# -- the live session ----------------------------------------------------------
class _LiveSession:
    """The one open device session.

    Only ever touched on the worker thread. Holds a flag for whether the download
    agent is running, because every flash operation needs it and the C++ side
    refuses to guess.
    """

    __slots__ = ("native", "session", "agent_running", "scatter")

    def __init__(self, native: Any, session: Any) -> None:
        self.native = native
        self.session = session
        self.agent_running = False
        self.scatter: ScatterInfo | None = None

    def close(self) -> None:
        try:
            self.session.disconnect()
        except Exception:  # a dead link is the normal case here, not an error
            pass


_session_lock = threading.Lock()
_live: _LiveSession | None = None


def session_open() -> bool:
    """True when a mediaTek session is open, whether or not an agent is running."""
    return _live is not None


def _take() -> _LiveSession:
    if _live is None:
        raise MtkNotReadyError(
            "no MediaTek session is open: connect to the device in BROM or preloader "
            "mode and upload a download agent first."
        )
    return _live


def _take_with_agent() -> _LiveSession:
    session = _take()
    if not session.agent_running:
        raise MtkNotReadyError(
            "no download agent is running. The bootrom can only load an agent and jump to "
            "it; the flash commands are the agent's protocol and cannot reach a device "
            "that is still sitting in the bootrom."
        )
    return session


def _install(session: _LiveSession | None) -> None:
    global _live
    with _session_lock:
        previous, _live = _live, session
    if previous is not None and previous is not session:
        previous.close()


def release_session(ctx: Any, *, quiet: bool = False) -> bool:
    """Close the session. Safe to call when none is open."""
    with _session_lock:
        had_session = _live is not None
    _install(None)
    if had_session and not quiet:
        ctx.log("MediaTek session closed", "info")
    return had_session


def _new_session(ctx: Any) -> _LiveSession:
    native = _native()
    session = native.MediaTekBrom(
        log=lambda level, message: ctx.log(message, level),
        progress=lambda percent, message: ctx.progress(percent, message),
        cancelled=lambda: bool(ctx.cancelled),
    )
    return _LiveSession(native, session)


def _human_bytes(count: float) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _progress_forwarder(ctx: Any) -> Any:
    """Turns the agent's flash progress into a job progress event.

    The C++ side throttles to one event per 100 ms and computes the speed from
    the wall clock, so nothing here needs to. A raise in this callback must not
    abort a flash, so a failure disables reporting rather than propagating.
    """
    state = {"broken": False}

    def report(event: Any) -> None:
        if state["broken"]:
            return
        try:
            phase = str(event.phase)
            where = f"{event.index}/{event.count} " if event.count else ""
            speed = str(event.speed_text())
            parts = [
                f"{where}{event.partition or phase}:",
                f"{int(event.percent)}%",
                f"({_human_bytes(event.done)} of {_human_bytes(event.total)})",
            ]
            if speed:
                parts.append(speed)
            ctx.progress(int(event.percent), " ".join(parts))
        except Exception:  # noqa: BLE001 - reporting must never break a transfer
            state["broken"] = True

    return report


def devices_present() -> list[str]:
    """MediaTek USB IDs currently on the bus. Safe on the UI thread: it only
    opens a libusb context and enumerates."""
    return list(_native().MediaTekBrom.devices_present())


# -- bootrom actions -----------------------------------------------------------
def verify_toolchain(ctx: Any) -> None:
    """Report whether a MediaTek device is attached and which stage it is in."""
    found = devices_present()
    if not found:
        ctx.log(
            "no MediaTek device is in BROM or preloader mode (looking for "
            + ", ".join(BROM_USB_IDS)
            + "). Power the device off, then hold the volume keys while connecting USB.",
            "warn",
        )
        return

    for usb_id in found:
        ctx.log(f"found {usb_id}", "ok")
    if "0e8d:0003" in found:
        ctx.log("0e8d:0003 is the boot ROM: the chip itself, before any firmware runs", "info")
    else:
        ctx.log(
            "the device is advertising a preloader ID, not the boot ROM. Preloader mode "
            "needs a different entry path and can be locked down by the firmware.",
            "info",
        )

    if _live is not None:
        ctx.log(f"a session is already open: {_live.session.describe()}", "ok")
        return

    session = _new_session(ctx)
    try:
        session.session.connect()
        ctx.log(f"link open: {session.session.describe()}", "ok")
    finally:
        session.close()


def read_chip_info(ctx: Any) -> ChipInfo:
    """Handshake, then read the hardware code, version block and BROM version."""
    if _live is not None:
        raise MtkNotReadyError(
            "a session is already open. Reading the chip identity restarts the handshake, "
            "which would drop a running download agent. Close the session first."
        )
    ctx.log("starting the bootrom handshake", "info")
    session = _new_session(ctx)
    try:
        session.session.handshake()
        info = ChipInfo.from_native(session.session.read_chip_info())
    finally:
        session.close()

    ctx.log(
        f"hardware code {info.hardware_code_hex}, hardware version "
        f"{info.hardware_version_hex}, HW sub-code 0x{info.hw_sub_code:04x}, "
        f"SW version 0x{info.sw_version:04x}",
        "ok",
    )
    if info.have_brom_version:
        ctx.log(f"boot ROM version {info.brom_version}", "info")
    if not info.chip_name:
        ctx.log(
            "the hardware code to chip name table is not implemented, so the SoC is "
            "reported by number only. Use it to pick the matching download agent.",
            "warn",
        )
    return info


def read_target_config(ctx: Any) -> TargetSecurity:
    """Handshake, then read the security configuration."""
    if _live is not None:
        raise MtkNotReadyError(
            "a session is already open; reading the target config would restart the "
            "handshake and drop the running agent. Close the session first."
        )
    session = _new_session(ctx)
    try:
        session.session.handshake()
        config = TargetSecurity.from_native(session.session.read_target_config())
    finally:
        session.close()

    ctx.log(f"target config 0x{config.raw:08x}: {config.description}", "ok")
    if config.sla_required:
        ctx.log(
            "this device requires SLA authentication: the bootrom will refuse a download "
            "agent until a signed challenge is answered, which is not implemented.",
            "error",
        )
    if config.certificate_required:
        ctx.log("the bootrom requires a signed certificate chain for the agent", "warn")
    if not config.memory_write_allowed:
        ctx.log("the bootrom reports that memory writes are not permitted", "warn")
    return config


def load_download_agent(
    ctx: Any,
    path: str | Path,
    load_address: int = DEFAULT_DA_ADDRESS,
    signature_length: int = 0,
    start: bool = True,
) -> None:
    """Upload a download agent (SEND_DA), start it (JUMP_DA) and keep the session.

    The session stays open afterwards because the agent is what everything else
    talks to; it is closed by the release button, by a failed action, or by the
    next upload.
    """
    agent = Path(path)
    if not agent.is_file():
        raise ValueError(f"download agent file does not exist: {agent}")
    if load_address <= 0:
        raise ValueError(f"invalid load address: {load_address}")

    if _live is not None:
        ctx.log("closing the existing session before a new upload", "info")
        release_session(ctx, quiet=True)

    ctx.log(
        f"uploading {agent.name} ({_human_bytes(agent.stat().st_size)}) to "
        f"0x{load_address:08x}"
        + (f", {signature_length} byte signature" if signature_length else ""),
        "info",
    )

    session = _new_session(ctx)
    try:
        session.session.connect()
        session.session.handshake()
        session.session.load_download_agent(
            str(agent), int(load_address), int(signature_length), bool(start)
        )
        session.agent_running = bool(start) and bool(session.session.agent_running())
    except BaseException:
        session.close()
        raise

    if start and not session.agent_running:
        session.close()
        raise MtkNotReadyError("the agent upload finished but the bootrom did not start it")

    _install(session)
    if session.agent_running:
        ctx.log(
            "the agent is running: the bootrom has handed over and the device now speaks "
            "the download agent's protocol",
            "ok",
        )
    else:
        ctx.log("the agent is loaded but has not been started", "warn")


# -- agent actions -------------------------------------------------------------
def read_flash_info(ctx: Any) -> FlashSummary:
    """Ask the running agent what it is driving.

    Every query here is optional to the agent. An older one that does not
    implement GET_DA_VERSION or the geometry calls still flashes, so each missing
    answer is reported as a gap rather than raised.
    """
    session = _take_with_agent()
    ctx.log("querying the download agent", "info")
    info = session.session.read_flash_info()
    summary = FlashSummary.from_native(info)

    if summary.da_version:
        ctx.log(f"download agent version {summary.da_version}", "ok")
    else:
        ctx.log(
            "the agent did not report a version; older ones do not implement "
            "GET_DA_VERSION, so this is not necessarily a fault",
            "warn",
        )

    if summary.connection_agent:
        ctx.log(f"the agent took over from the {summary.connection_agent}", "ok")

    if summary.total_size:
        ctx.log(
            f"{summary.storage_label}: {_human_bytes(summary.total_size)}"
            + (f" in {summary.block_size}-byte blocks" if summary.block_size else ""),
            "ok",
        )
    else:
        ctx.log(
            "the agent reported no storage geometry. The geometry queries are optional in "
            "this protocol, so this is a gap in the report rather than a fault.",
            "warn",
        )

    for name, size in summary.boot_areas.items():
        if size:
            ctx.log(f"  {name}: {_human_bytes(size)}", "output")

    if summary.ram is not None:
        ctx.log(
            f"DRAM 0x{int(summary.ram.dram_base):x} + {_human_bytes(int(summary.ram.dram_size))}, "
            f"SRAM 0x{int(summary.ram.sram_base):x} + {_human_bytes(int(summary.ram.sram_size))}",
            "debug",
        )

    if summary.sla_enabled:
        ctx.log(
            "the agent reports that SLA is enabled on this device. Writes may be refused "
            "even though the agent itself started.",
            "warn",
        )
    return summary


def load_scatter(ctx: Any, path: str | Path, *, keep: bool = True) -> ScatterInfo:
    """Parses a scatter file. Pure host work: no device is touched.

    This is what fills the partition table in the UI before anything is written,
    which is the point - an operator should see the whole plan first.
    """
    native = _native()
    scatter_path = Path(path)
    if not scatter_path.is_file():
        raise ValueError(f"scatter file does not exist: {scatter_path}")

    parsed = native.load_scatter(str(scatter_path))
    info = ScatterInfo(
        path=str(scatter_path),
        format=str(parsed.format).rsplit(".", 1)[-1],
        platform=str(parsed.general.platform),
        project=str(parsed.general.project),
        storage=str(parsed.general.storage),
        partitions=tuple(PartitionRecord.from_native(entry) for entry in parsed.partitions),
        unmodelled_keys=tuple(str(key) for key in parsed.unmodelled_keys()),
    )

    ctx.log(
        f"{scatter_path.name}: {len(info.partitions)} partition(s), "
        f"{len(info.downloads)} to write ({_human_bytes(info.total_download_bytes)}), "
        f"{info.format} format"
        + (f", platform {info.platform}" if info.platform else ""),
        "ok",
    )
    for entry in info.partitions:
        marker = "write" if entry.is_download and entry.file_name else "erase"
        ctx.log(
            f"  {entry.display_name:<24} {marker:<5} sector 0x{entry.start_address:x} "
            f"+{_human_bytes(entry.size):>10}"
            + (f"  {entry.file_name}" if entry.file_name else "")
            + (f"  [{entry.region}]" if entry.region else ""),
            "output",
        )
    if info.unmodelled_keys:
        ctx.log(
            "this package carries fields this build does not model and passes through as "
            "written: " + ", ".join(info.unmodelled_keys),
            "warn",
        )

    if keep and _live is not None:
        _live.scatter = info
    return info


def flash_firmware(
    ctx: Any,
    scatter_path: str | Path,
    image_dir: str | Path = "",
    *,
    only: Iterable[str] | None = None,
    continue_on_error: bool = False,
) -> FlashReport:
    """Replays a scatter file through the running agent.

    Image paths resolve against `image_dir`, or the scatter file's own directory,
    which is how these packages are laid out.
    """
    session = _take_with_agent()
    scatter = Path(scatter_path)
    if not scatter.is_file():
        raise ValueError(f"scatter file does not exist: {scatter}")

    selected = list(only) if only is not None else []
    info = load_scatter(ctx, scatter, keep=False)

    ctx.log(
        f"flashing {len(info.downloads)} partition(s) from {scatter.name}"
        + (f", selection: {', '.join(selected)}" if selected else ""),
        "warn",
    )
    session.session.set_flash_progress(_progress_forwarder(ctx))
    try:
        result = session.session.flash_scatter(
            str(scatter), str(image_dir), bool(continue_on_error), selected
        )
    finally:
        session.session.set_flash_progress(None)

    report = FlashReport.from_native(result)
    ctx.progress(-1, report.summary)
    for outcome in report.partitions:
        if outcome.succeeded:
            ctx.log(f"  {outcome.name}: written ({_human_bytes(outcome.bytes)})", "ok")
        elif outcome.skipped:
            ctx.log(f"  {outcome.name}: skipped ({outcome.error})", "warn")
        else:
            ctx.log(f"  {outcome.name}: FAILED — {outcome.error}", "error")

    ctx.log(report.summary, "ok" if report.completed else "error")
    if not report.completed:
        raise RuntimeError(
            "the flash run did not finish: "
            + report.summary
            + ". The partitions before the failure are written and the ones after it are "
            "not; write the whole package again once the cause is fixed."
        )
    return report


def readback_partition(
    ctx: Any,
    scatter_path: str | Path,
    partition: str,
    destination: str | Path,
) -> int:
    """Reads one partition named by a scatter file out to a file."""
    session = _take_with_agent()
    info = load_scatter(ctx, scatter_path, keep=False)
    entry = info.find(partition)
    if entry is None:
        raise ValueError(
            f"{partition} is not in {Path(scatter_path).name}; the partitions it lists are "
            "the ones that can be read back"
        )
    if entry.size == 0:
        raise ValueError(f"{entry.display_name} has no size in the scatter file")

    ctx.log(
        f"reading {entry.display_name} ({_human_bytes(entry.size)}) from "
        f"0x{entry.start_address:x} to {destination}",
        "info",
    )
    session.session.set_flash_progress(_progress_forwarder(ctx))
    try:
        written = session.session.read_back(str(scatter_path), partition, str(destination))
    finally:
        session.session.set_flash_progress(None)
    ctx.progress(-1, f"{entry.display_name}: {_human_bytes(written)} read")
    return int(written)


def format_flash(
    ctx: Any,
    scatter_path: str | Path,
    *,
    only: Iterable[str] | None = None,
) -> int:
    """Erases every partition the scatter file lists with no image.

    This is the "format" half of a firmware upgrade: the package names regions it
    means to clear rather than write.
    """
    session = _take_with_agent()
    scatter = Path(scatter_path)
    if not scatter.is_file():
        raise ValueError(f"scatter file does not exist: {scatter}")

    selected = list(only) if only is not None else []
    ctx.log(
        f"erasing the partitions {scatter.name} lists with no image"
        + (f", selection: {', '.join(selected)}" if selected else ""),
        "warn",
    )
    session.session.set_flash_progress(_progress_forwarder(ctx))
    try:
        erased = session.session.format_scatter(str(scatter), selected)
    finally:
        session.session.set_flash_progress(None)
    ctx.log(f"erased {_human_bytes(erased)} across the listed partitions", "ok")
    return int(erased)


def shutdown_device(ctx: Any, mode: int = 0) -> None:
    """Tell the agent to shut the device down. The link is not usable after."""
    session = _take_with_agent()
    session.session.shutdown_device(int(mode))
    # The device re-enumerates, so this conversation is over whatever it answered.
    release_session(ctx, quiet=True)
    ctx.log("the device has been told to shut down and will re-enumerate", "ok")
