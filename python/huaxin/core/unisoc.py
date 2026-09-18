"""Unisoc / Spreadtrum Research Download - the session wrapper.

WHAT THIS IS NOW
----------------
A working session against a device in Research Download mode. The protocol layer
was always complete and tested; what was missing was the wiring, and this module
is that wiring: it opens a device through `huaxin_core.UnisocBsl`, keeps the one
open session, and exposes the operations the tab asks for.

The distinction that matters, and that the tab states in its own words: this can
talk to a device. It does **not** replay a PAC package yet - see below.

WHAT IS NOT HERE, AND WHY
-------------------------
Loading FDL1 and then FDL2 and writing a package through it is the next step, and
it is deliberately not guessed at. The payload primitive exists and is tested
(`execute_payload` sends START_DATA / MIDST_DATA / END_DATA and optionally
EXEC_DATA), so *sending* a loader is proven - but the handover between FDL1 and
FDL2, and what the device expects between them, is not something this project
holds a primary source for. `docs/spd-status.md` records it as an open question.

So `flash_pac` is not offered anywhere in the interface. A button that ran a
guessed sequence against somebody's phone would be worse than a button that is
not there.

WHAT IS STILL UNKNOWN, unchanged from before: the field layout inside
`READ_FLASH_INFO`'s reply, the baud-rate renegotiation sequence, NV CRC refresh on
the write path, and the PDL pre-stage the RDA8910 family uses to reach BSL.

And the caveat that applies to every vendor in this tool: none of it has been run
against real hardware. The tests prove the encoder and the state machine, not that
a particular phone accepts the result.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "BLOCKED_REASON",
    "PENDING_REASON",
    "RESEARCH_DOWNLOAD_USB_ID",
    "UNISOC_VID",
    "BslInfo",
    "UnisocNotReadyError",
    "bsl_connect",
    "bsl_power_off",
    "bsl_read_device_info",
    "bsl_read_region",
    "bsl_reset",
    "devices_present",
    "release_session",
    "session_open",
    "verify_toolchain",
]

UNISOC_VID = 0x1782
#: The only id every reference client opens, and the one in this project's
#: catalogue. Repeated here so the UI can name it without loading the C++ layer.
RESEARCH_DOWNLOAD_USB_ID = "1782:4d00"

#: What is still not wired, said plainly. The tab shows this rather than a button
#: that cannot work.
PENDING_REASON = (
    "Unisoc can talk to a device in Research Download mode: handshake, device and "
    "flash identity, read-back, reset and power-off. Replaying a .pac package is NOT "
    "implemented - the FDL1 to FDL2 handover needs a primary source this project does "
    "not have, and a guessed sequence against a phone is worse than no sequence at "
    "all. See docs/spd-status.md."
)

#: Kept so an existing import does not break; the name is now historical.
BLOCKED_REASON = PENDING_REASON


class UnisocNotReadyError(RuntimeError):
    """A session was used before one was open, or the device stopped answering."""


@dataclass
class BslInfo:
    """What the boot ROM said about itself, as a plain snapshot for the UI.

    `None` and `0` mean different things here: a device that answered "chip type
    is zero" is not the same as one that declined to answer, and the log says
    which happened.
    """

    boot_version: str = ""
    chip_type: int | None = None
    flash_type: int | None = None
    sector_size: int | None = None
    chip_uid: bytes = b""
    describe: str = ""


class _LiveSession:
    """The one open device session. Only ever touched on the worker thread."""

    __slots__ = ("native", "session")

    def __init__(self, native: Any, session: Any) -> None:
        self.native = native
        self.session = session

    def close(self) -> None:
        try:
            self.session.disconnect()
        except Exception:  # a dead link is the normal case here, not an error
            pass


_session_lock = threading.Lock()
_live: _LiveSession | None = None


def _native() -> Any:
    import sys

    if str(Path(__file__).resolve().parents[2]) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import huaxin_core  # noqa: PLC0415

    return huaxin_core


def session_open() -> bool:
    """True when a session object exists, whether or not the link is up."""
    return _live is not None


def _take() -> _LiveSession:
    session = _live
    if session is None:
        raise UnisocNotReadyError(
            "no Unisoc session is open. Put the device in Research Download mode and run "
            "the handshake first."
        )
    return session


def _install(session: _LiveSession | None) -> None:
    global _live
    with _session_lock:
        previous, _live = _live, session
    if previous is not None and previous is not session:
        previous.close()


def release_session(ctx: Any, *, quiet: bool = False) -> bool:
    """Closes the session. Safe to call when none is open."""
    with _session_lock:
        had = _live is not None
    _install(None)
    if had and not quiet:
        ctx.log("Unisoc session closed", "info")
    return had


def _new_session(ctx: Any) -> _LiveSession:
    native = _native()
    session = native.UnisocBsl(
        log=lambda level, message: ctx.log(message, level),
        progress=lambda percent, message: ctx.progress(percent, message),
        cancelled=lambda: bool(ctx.cancelled),
    )
    return _LiveSession(native, session)


# -----------------------------------------------------------------------------
#  Jobs. Each takes a JobContext first and runs on the worker thread.
# -----------------------------------------------------------------------------


def devices_present() -> list[str]:
    """Unisoc download-mode USB IDs currently on the bus.

    This asks the C++ layer, which matches against the catalogue's own VID/PID
    table - a narrower and more honest question than "anything with the Unisoc
    vendor ID": a device in some other Unisoc mode is not a device this can talk
    to.
    """
    try:
        return list(_native().UnisocBsl.devices_present())
    except ImportError:  # pragma: no cover - only when the build is missing
        return []


def bsl_connect(ctx: Any) -> BslInfo:
    """Opens the device, runs the hello, and asks it what it is.

    The one call that has to happen before anything else, and the one that fails
    with something useful when the driver, the cable or the mode is wrong.
    """
    release_session(ctx, quiet=True)

    found = devices_present()
    ctx.log(f"Unisoc devices on the bus: {', '.join(found) if found else 'none'}", "info")
    if not found:
        raise UnisocNotReadyError(
            "no Unisoc device in Research Download mode is attached (expected "
            f"{RESEARCH_DOWNLOAD_USB_ID}). Hold the device's download key while plugging "
            "it in, and check which driver Windows bound - see Help, USB driver help."
        )

    ctx.progress(-1, "opening the device")
    session = _new_session(ctx)
    _install(session)
    try:
        session.session.connect()
        session.session.handshake()
    except Exception:
        # A failed handshake leaves nothing worth keeping open, and a half-open
        # device is the state that makes the next attempt behave strangely.
        release_session(ctx, quiet=True)
        raise

    ctx.log(
        f"link up: {session.session.describe()} (checksum {session.session.checksum_name()})",
        "ok",
    )
    return bsl_read_device_info(ctx)


def bsl_read_device_info(ctx: Any) -> BslInfo:
    """Every query the running link will answer."""
    session = _take()
    ctx.progress(-1, "reading device information")
    info = session.session.read_device_info()

    lines = [f"boot version: {info.boot_version or 'not reported'}"]
    if info.have_chip_type:
        lines.append(f"chip type: 0x{info.chip_type:08x}")
    if info.have_flash_type:
        lines.append(f"flash type: 0x{info.flash_type:08x}")
    if info.have_sector_size:
        lines.append(f"sector size: {info.sector_size}")
    if info.have_chip_uid and info.chip_uid:
        lines.append("chip UID: " + bytes(info.chip_uid).hex())
    for line in lines:
        ctx.log(line, "info")

    refused = [
        name
        for name, have in (
            ("chip type", info.have_chip_type),
            ("flash type", info.have_flash_type),
            ("sector size", info.have_sector_size),
            ("chip UID", info.have_chip_uid),
        )
        if not have
    ]
    if refused:
        # A refusal is not a failure: devices differ in what they answer, and the
        # operator needs to know which of these this one declined.
        ctx.log("the device did not report: " + ", ".join(refused), "warn")

    return BslInfo(
        boot_version=str(info.boot_version),
        chip_type=int(info.chip_type) if info.have_chip_type else None,
        flash_type=int(info.flash_type) if info.have_flash_type else None,
        sector_size=int(info.sector_size) if info.have_sector_size else None,
        chip_uid=bytes(info.chip_uid) if info.have_chip_uid else b"",
        describe=str(info.describe()),
    )


def bsl_read_region(ctx: Any, address: int, length: int, destination: Path) -> Path:
    """Reads a region back and writes it to a file.

    Read-back is how an operator proves what is actually on the device, which is
    the one check that does not depend on trusting the tool that wrote it.
    """
    session = _take()
    if length <= 0:
        raise ValueError("refusing to read a zero-length region")
    if not 0 <= address <= 0xFFFFFFFF:
        raise ValueError(f"address 0x{address:x} does not fit in the 32 bits the protocol uses")

    ctx.log(f"reading {length} bytes at 0x{address:08x}", "info")
    ctx.progress(-1, "reading")
    data = bytes(session.session.read_flash(int(address), int(length)))

    if len(data) != length:
        # Short data is not a smaller read, it is a failed one: the device stopped
        # answering partway through and the bytes that did arrive mean nothing on
        # their own.
        raise UnisocNotReadyError(
            f"asked for {length} bytes and got {len(data)}; the device stopped answering"
        )

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    ctx.log(f"read {len(data)} bytes into {target}", "ok")
    ctx.progress(100, "read complete")
    return target


def bsl_reset(ctx: Any) -> None:
    """Restarts the device out of download mode."""
    session = _take()
    session.session.reset()
    ctx.log("device reset", "ok")
    release_session(ctx, quiet=True)


def bsl_power_off(ctx: Any) -> None:
    """Powers the device off."""
    session = _take()
    session.session.power_off()
    ctx.log("device powered off", "ok")
    release_session(ctx, quiet=True)


def verify_toolchain(ctx: Any) -> None:
    """Reports what is attached, and exactly how far this vendor is implemented."""
    found = devices_present()
    if found:
        ctx.log(f"found Unisoc device(s) in download mode: {', '.join(found)}", "ok")
    else:
        ctx.log(f"no device at {RESEARCH_DOWNLOAD_USB_ID} is attached", "info")

    ctx.log(PENDING_REASON, "warn")
