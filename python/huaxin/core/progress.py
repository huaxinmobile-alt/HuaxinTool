"""Job progress as the interface consumes it.

WHY THIS EXISTS WHEN `FlashProgress` ALREADY DOES. The native `FlashProgress`
struct is the authoritative description of a running flash, and it is the right
thing for the core to emit. It is not something Python can construct, though -
and the qualcomm and mediatek paths report progress through a two-argument
callback that has already collapsed bytes into a percentage by the time it
crosses into Python.

So this module does two jobs:

* it *normalises* whatever a job reports into one shape, so the status display
  does not care whether the numbers came from the native tracker or from a
  Python loop copying a file; and
* it gives a Python-side job a way to report real figures - bytes moved, and the
  rate - rather than a percentage it invented.

WHERE THE NUMBERS ARE GUESSED, THE GUESS IS LABELLED. `OperationStatus` estimates
an ETA from the observed rate when nothing reports one, and marks it with a "~".
An unlabelled estimate presented as a fact is how an operator sits watching a
bar that says "2 minutes left" for twenty.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["JobProgress", "from_native", "format_bytes", "format_duration", "format_speed"]

#: Reported when a figure is genuinely not known. Distinct from 0, which is a
#: real value - an ETA of zero means it is finished, and unknown means nobody
#: can say.
UNKNOWN = -1.0


@dataclass
class JobProgress:
    """One progress report, in the shape the interface reads.

    Field names match the native struct so a caller can build one of these from
    a `FlashProgress` without a translation table, and so the two can be read
    side by side without a mapping in your head.
    """

    operation_type: str = "working"
    current_partition: str = ""
    #: Zero-based, as the native struct has it. Shown one-based, because "partition
    #: 0 of 12" is a computer's way of counting and this line is read by a person.
    current_partition_index: int = 0
    total_partitions: int = 0
    bytes_written: int = 0
    total_bytes: int = 0
    percentage: float = UNKNOWN
    #: Megabytes per second, decimal, as every tool that flashes a phone reports
    #: it. Not mebibits: the number is compared against what the vendor's own tool
    #: says, and matching their units is the only way that comparison means
    #: anything.
    speed_mbps: float = 0.0
    #: Seconds remaining, or `UNKNOWN`.
    eta_seconds: float = UNKNOWN
    status_message: str = ""
    running: bool = True

    @property
    def has_percentage(self) -> bool:
        return 0.0 <= self.percentage <= 100.0

    @property
    def has_eta(self) -> bool:
        return self.eta_seconds >= 0.0

    @property
    def has_bytes(self) -> bool:
        return self.total_bytes > 0

    def describe(self) -> str:
        """One line, for a tooltip or a log entry. Mirrors the native version."""
        parts = [self.operation_type or "working"]
        if self.current_partition:
            parts.append(self.current_partition)
        if self.has_percentage:
            parts.append(f"{self.percentage:.0f}%")
        if self.has_bytes:
            parts.append(f"{format_bytes(self.bytes_written)} / {format_bytes(self.total_bytes)}")
        if self.speed_mbps > 0:
            parts.append(format_speed(self.speed_mbps))
        if self.has_eta:
            parts.append(f"{format_duration(self.eta_seconds)} left")
        if self.status_message:
            parts.append(self.status_message)
        return "  ·  ".join(parts)


def from_native(progress: object) -> JobProgress:
    """Reads a progress object of either kind into a `JobProgress`.

    Duck-typed on purpose: the native type is a pybind11 class that this module
    must not import, because the module has to keep working when the native
    extension is not built at all - which is the normal state of a fresh
    checkout before the first CMake run.
    """
    if isinstance(progress, JobProgress):
        return progress

    def number(name: str, default: float) -> float:
        value = getattr(progress, name, None)
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    return JobProgress(
        operation_type=str(getattr(progress, "operation_type", "") or "working"),
        current_partition=str(getattr(progress, "current_partition", "") or ""),
        current_partition_index=int(number("current_partition_index", 0)),
        total_partitions=int(number("total_partitions", 0)),
        bytes_written=int(number("bytes_written", 0)),
        total_bytes=int(number("total_bytes", 0)),
        percentage=number("percentage", UNKNOWN),
        speed_mbps=number("speed_mbps", 0.0),
        eta_seconds=number("eta_seconds", UNKNOWN),
        status_message=str(getattr(progress, "status_message", "") or ""),
        running=bool(getattr(progress, "running", True)),
    )


# -----------------------------------------------------------------------------
#  Formatting
#
#  These mirror huaxin::core::format_bytes / format_duration, character for
#  character, so a size printed by the status bar matches the same size printed
#  in the log by the native core. Two formatters that disagree by a decimal place
#  make an operator wonder which one is lying.
# -----------------------------------------------------------------------------

_UNITS = ("B", "KB", "MB", "GB", "TB")


def format_bytes(count: float) -> str:
    """Binary units, labelled the way people say them: 4.0 GB, not 4.0 GiB."""
    value = float(count)
    unit = 0
    while value >= 1024.0 and unit + 1 < len(_UNITS):
        value /= 1024.0
        unit += 1
    if unit == 0:
        return f"{int(count)} B"
    if value < 10.0:
        return f"{value:.2f} {_UNITS[unit]}"
    return f"{value:.1f} {_UNITS[unit]}"


def format_duration(seconds: float) -> str:
    """A duration a person reads: 45s, 2m 07s, 1h 03m."""
    if seconds is None or seconds < 0:
        return "unknown"
    whole = int(seconds + 0.5)
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m {whole % 60:02d}s"
    return f"{whole // 3600}h {(whole % 3600) // 60:02d}m"


def format_speed(mbps: float) -> str:
    """A transfer rate. Sub-megabyte rates keep two decimals, above one is enough."""
    if mbps <= 0:
        return "—"
    if mbps < 10.0:
        return f"{mbps:.2f} MB/s"
    return f"{mbps:.1f} MB/s"
