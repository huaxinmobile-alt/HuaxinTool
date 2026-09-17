"""Samsung Download mode (Odin) operations.

Same shape as the other protocol wrappers: every function takes the worker's job
context first and matches `fn(ctx, ...)`. The protocol lives in C++; this file
binds the callbacks and turns results into log lines.

What works today: parsing a PIT - from a file or read off a device - and the Odin
session handshake. What does not: transferring a firmware image. That needs the
file-part loop and a `.tar.md5` reader, and is Phase 7b. The buttons say so.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "DOWNLOAD_MODE_USB_IDS",
    "Partition",
    "PartitionTable",
    "SAMSUNG_VID",
    "devices_present",
    "parse_pit_file",
    "verify_toolchain",
]

SAMSUNG_VID = 0x04E8
#: Download mode advertises this product ID. The second is a commonly reported
#: alternate that this project has not confirmed - see the catalogue entry.
DOWNLOAD_MODE_USB_IDS = ("04e8:685d", "04e8:6860")


def _native() -> Any:
    if str(Path(__file__).resolve().parents[2]) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import huaxin_core  # noqa: PLC0415

    return huaxin_core


@dataclass(frozen=True, slots=True)
class Partition:
    """One PIT entry, as the operator needs to see it."""

    name: str
    identifier: int
    flash_filename: str
    device_type: str
    size_bytes: int
    block_count: int
    block_size: int
    writable: bool
    secure: bool

    @property
    def size_human(self) -> str:
        if self.size_bytes <= 0:
            return "unknown"
        value = float(self.size_bytes)
        for unit in ("B", "KiB", "MiB", "GiB"):
            if value < 1024 or unit == "GiB":
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024
        return f"{value:.1f} GiB"

    @classmethod
    def from_native(cls, entry: Any) -> "Partition":
        return cls(
            name=str(entry.partition_name),
            identifier=int(entry.identifier),
            flash_filename=str(entry.flash_filename),
            device_type=str(entry.device_type_name),
            size_bytes=int(entry.size_bytes),
            block_count=int(entry.block_count),
            block_size=int(entry.block_size_or_offset),
            writable=bool(entry.writable),
            secure=bool(entry.secure),
        )


@dataclass(frozen=True, slots=True)
class PartitionTable:
    """A parsed PIT."""

    partitions: tuple[Partition, ...]
    source: str

    @property
    def count(self) -> int:
        return len(self.partitions)

    def find(self, name: str) -> Partition | None:
        wanted = name.strip().lower()
        for partition in self.partitions:
            if partition.name.lower() == wanted:
                return partition
        return None

    @property
    def total_bytes(self) -> int:
        return sum(partition.size_bytes for partition in self.partitions)


def devices_present() -> bool:
    """True when a Samsung download-mode device is on the bus."""
    native = _native()
    from huaxin.core.backend import Device  # noqa: PLC0415  (avoid a cycle at import time)

    del Device  # only imported to document the relationship
    bridge = native.HardwareBridge()
    try:
        if not bridge.init():
            return False
        for info in bridge.get_device_list(False):
            usb_id = f"{info.vid:04x}:{info.pid:04x}"
            if usb_id in DOWNLOAD_MODE_USB_IDS:
                return True
    finally:
        bridge.shutdown()
    return False


def verify_toolchain(ctx: Any) -> None:
    """Report whether a device in download mode is attached."""
    native = _native()
    bridge = native.HardwareBridge()
    try:
        if not bridge.init():
            ctx.log(f"the native backend could not start: {bridge.last_error()}", "error")
            return
        # String descriptors are skipped: this only needs the USB IDs, and EDL
        # style downloads do not need each device opened.
        found = [
            f"{info.vid:04x}:{info.pid:04x}"
            for info in bridge.get_device_list(False)
        ]
    finally:
        bridge.shutdown()

    matches = [usb_id for usb_id in found if usb_id in DOWNLOAD_MODE_USB_IDS]
    if matches:
        for usb_id in matches:
            ctx.log(f"found {usb_id} - the device is in download mode", "ok")
        if "04e8:6860" in matches:
            ctx.log(
                "04e8:6860 is an alternate Samsung download-mode PID this project has not "
                "confirmed against real hardware; the session may or may not be answered",
                "warn",
            )
        return

    ctx.log(
        "no device in Samsung download mode (looking for "
        + ", ".join(DOWNLOAD_MODE_USB_IDS)
        + "). Power the device off, then hold volume-down and volume-up while connecting USB.",
        "warn",
    )


def parse_pit_file(ctx: Any, path: str | Path) -> PartitionTable:
    """Parse a PIT file from disk.

    The PIT is a plain little-endian table, so this needs no device. A dump is
    often exactly what an operator has to hand, and being able to inspect it
    before touching the device is the point.
    """
    native = _native()
    pit_path = Path(path)
    if not pit_path.is_file():
        raise ValueError(f"PIT file does not exist: {pit_path}")

    data = pit_path.read_bytes()
    ctx.log(f"parsing {pit_path.name} ({len(data)} bytes)", "info")

    # The parser raises ProtocolError for anything that is not a coherent PIT;
    # it is deliberately not caught here so the reason reaches the log intact.
    pit = native.parse_pit(data)

    table = PartitionTable(
        partitions=tuple(Partition.from_native(entry) for entry in pit.entries),
        source=str(pit_path),
    )
    _log_table(ctx, table)
    return table


def _log_table(ctx: Any, table: PartitionTable) -> None:
    ctx.log(f"partition table holds {table.count} entries", "ok")
    for partition in table.partitions:
        flags = []
        if partition.writable:
            flags.append("writable")
        if partition.secure:
            flags.append("secure")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        ctx.log(
            f"  {partition.name:<24} id={partition.identifier:<4} "
            f"{partition.size_human:>10}  {partition.device_type}{suffix}",
            "info",
        )


def read_pit_from_device(ctx: Any) -> PartitionTable:
    """Read the PIT from a device in download mode.

    Not wired to a button yet: the Odin session needs the Samsung CDC transport,
    which is not implemented - the existing bulk transport is EDL's. The C++
    session logic is written and tested; only the USB binding is missing.
    """
    raise NotImplementedError(
        "reading the PIT from a device needs the Samsung USB transport, which is not "
        "implemented yet. Parsing a PIT file with Load PIT File… works today."
    )
