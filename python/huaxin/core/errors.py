"""Turns anything the backend raises into one classified failure type.

The C++ protocol layers raise their own exceptions - ProtocolError and friends -
because that is what packet code knows about. Python needs something the UI can
act on without reading the message: is a retry worth offering, is the device
possibly mid-write, what should the operator be told.

`describe()` is that translation, and it is the only place it happens. The UI
never inspects an exception's text to decide what to do.

TWO RULES WORTH STATING

* Nothing here swallows a failure. Every path returns a classified value or
  re-raises; a bare `except: pass` would turn "the flash failed" into "the flash
  silently did nothing", which is the worst outcome for a tool that writes to
  hardware.
* A failure that is already classified is left alone. Re-classifying would lose
  the context frames the C++ layer attached.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

__all__ = [
    "FlashFailure",
    "BACKEND_IMPORT_ERROR",
    "backend",
    "classify",
    "describe",
    "guard",
    "retry_policy_from",
    "run_with_retry",
    "log_failure",
]

T = TypeVar("T")

#: Set on first use. Importing huaxin_core here rather than at module scope keeps
#: a missing build from breaking anything that only wants the pure-Python parts
#: of this module - which is what the tests for it do.
backend: Any = None
BACKEND_IMPORT_ERROR: str | None = None

try:  # pragma: no cover - the happy path is exercised by every other test
    import huaxin_core as backend  # type: ignore[no-redef]
except ImportError as exc:  # pragma: no cover - only when the build is missing
    BACKEND_IMPORT_ERROR = str(exc)


# -----------------------------------------------------------------------------
#  The value type
# -----------------------------------------------------------------------------


@dataclass
class FlashFailure:
    """A classified failure, whether or not the C++ layer classified it.

    Deliberately a plain dataclass rather than the exception itself: the UI shows
    failures in a list, stores them, and serialises them into the log, and an
    exception object is a poor thing to carry around for that.
    """

    #: The FlashError name: "USB_ERROR", "FLASH_ERROR", and so on.
    kind: str = "INTERNAL_ERROR"
    #: "Qualcomm", "MediaTek", or "" when it happened before a protocol was chosen.
    vendor: str = ""
    #: The failure without the [KIND] [Vendor] prefix.
    message: str = ""
    #: One imperative sentence for the operator. Never empty.
    advice: str = ""
    #: Whether trying again could plausibly work.
    retryable: bool = False
    #: Whether the device may be left mid-write.
    dangerous: bool = False
    #: When it happened, seconds since the epoch.
    timestamp: float = field(default_factory=time.time)
    #: What the tool was doing, outermost frame first.
    context: list[tuple[str, str]] = field(default_factory=list)
    #: The exception's own type name, for a bug report.
    exception_type: str = ""

    @property
    def summary(self) -> str:
        """One line: kind, vendor and message, without the advice."""
        parts = [self.kind]
        if self.vendor:
            parts.append(self.vendor)
        head = "  ".join(parts)
        return f"{head}: {self.message}" if self.message else head

    @property
    def timestamp_text(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp))

    def report(self) -> str:
        """The whole thing as a block, for the log and the bug-report dialog."""
        lines = [f"{self.timestamp_text}  {self.kind}" + (f"  {self.vendor}" if self.vendor else "")]
        lines.append(f"  what: {self.message}")
        if self.context:
            frames = " > ".join(
                operation + (f" ({detail})" if detail else "")
                for operation, detail in self.context
            )
            lines.append(f"  while: {frames}")
        if self.exception_type:
            lines.append(f"  type: {self.exception_type}")
        lines.append(f"  fix:  {self.advice}")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "vendor": self.vendor,
            "message": self.message,
            "advice": self.advice,
            "retryable": self.retryable,
            "dangerous": self.dangerous,
            "timestamp": self.timestamp,
            "context": [list(frame) for frame in self.context],
            "exception_type": self.exception_type,
        }


#: Fallback advice, used only when the native classifier is unavailable - which
#: means the build is missing, and the honest thing to say is that.
_FALLBACK_ADVICE = {
    "USB_ERROR": "Reconnect the device with a known-good cable, into a rear USB "
                 "port directly on the motherboard, then scan again.",
    "PROTOCOL_ERROR": "Disconnect the device, put it back into the correct mode, "
                      "and start the operation again from the beginning.",
    "FILE_ERROR": "Check the firmware package: the file may be missing, truncated, "
                  "or in a format this build does not read.",
    "FLASH_ERROR": "Do not unplug the device. Retry once; if it fails again, reflash "
                   "the full package rather than a single partition.",
    "AUTH_ERROR": "This device's security policy rejected the operation. Signed "
                  "firmware or an authorised account is required.",
    "INTERNAL_ERROR": "This is a bug in the tool. Save the log and report it with "
                      "the device model.",
    "CANCELLED": "The operation was cancelled. The device may be left in a partial "
                 "state - rescan it and reflash before using it.",
}
_FALLBACK_RETRYABLE = {"USB_ERROR", "PROTOCOL_ERROR"}
_FALLBACK_DANGEROUS = {"PROTOCOL_ERROR", "FLASH_ERROR", "CANCELLED"}


def classify(error: BaseException) -> str:
    """The FlashError name for a Python exception.

    Delegates to the native classifier when it is available, so the mapping is
    the same one the C++ layer uses and there is no second copy of it to drift.

    The name comes from `flash_error_name()` rather than from the enum's `.name`:
    pybind11 reports the C++ *enumerator* ("UsbError"), while everything else in
    this project - the log, the UI, the settings file - uses the upper-case tag
    ("USB_ERROR"). Converting once, here, is what keeps those two in step.
    """
    if backend is not None:
        try:
            return str(backend.flash_error_name(backend.classify_exception(error)))
        except Exception:  # noqa: BLE001 - a classifier must never raise
            pass

    # Without the native module, place the exception by type and message. This is
    # a fallback for a broken build, not a second implementation to keep in step.
    text = str(error).lower()
    # TimeoutError and ConnectionError are checked before OSError because in
    # Python 3 both derive from it: a timeout that fell through to the file rule
    # would tell the operator to check their firmware package.
    if isinstance(error, (TimeoutError, ConnectionError)):
        return "USB_ERROR"
    if isinstance(error, (FileNotFoundError, IsADirectoryError, PermissionError)):
        return "FILE_ERROR"
    if isinstance(error, MemoryError):
        return "INTERNAL_ERROR"
    if isinstance(error, KeyboardInterrupt):
        return "CANCELLED"
    if isinstance(error, (ValueError, TypeError, KeyError, IndexError)):
        return "FILE_ERROR"
    if isinstance(error, OSError):
        return "FILE_ERROR"
    if "cancel" in text:
        return "CANCELLED"
    if "timed out" in text or "timeout" in text or "disconnect" in text or "no device" in text:
        return "USB_ERROR"
    if "no such file" in text or "not found" in text or "corrupt" in text or "truncated" in text:
        return "FILE_ERROR"
    if "secure boot" in text or "authentication" in text or "locked" in text:
        return "AUTH_ERROR"
    return "PROTOCOL_ERROR"

def describe(
    error: BaseException,
    *,
    vendor: str = "",
    operation: str = "",
) -> FlashFailure:
    """Classifies an exception into a FlashFailure.

    A FlashException that came across the boundary already carries its
    classification, its vendor and its context frames, so it is read directly
    rather than re-derived - re-classifying would throw away the frames.
    """
    if backend is not None and isinstance(error, backend.FlashException):
        failure = FlashFailure(
            kind=str(error.error_name),
            vendor=str(error.vendor_name),
            message=str(error.message),
            advice=str(error.advice),
            retryable=bool(error.retryable),
            dangerous=bool(error.dangerous),
            timestamp=float(error.timestamp),
            context=[(str(op), str(detail)) for op, detail in error.context],
            exception_type=type(error).__name__,
        )
        if operation and not failure.context:
            failure.context.append((operation, ""))
        return failure

    kind = classify(error)
    advice = _FALLBACK_ADVICE.get(kind, _FALLBACK_ADVICE["INTERNAL_ERROR"])
    retryable = kind in _FALLBACK_RETRYABLE
    dangerous = kind in _FALLBACK_DANGEROUS

    if backend is not None:
        # The native side owns the advice and the two flags; the fallbacks above
        # are only for a build where it could not be imported.
        try:
            enum = getattr(backend.FlashError, _enum_member(kind))
            advice = str(backend.advice_for(enum))
            retryable = bool(backend.is_retryable(enum))
            dangerous = bool(backend.is_dangerous(enum))
        except Exception:  # noqa: BLE001
            pass

    return FlashFailure(
        kind=kind,
        vendor=vendor,
        message=str(error),
        advice=advice,
        retryable=retryable,
        dangerous=dangerous,
        context=[(operation, "")] if operation else [],
        exception_type=type(error).__name__,
    )


def _enum_member(kind: str) -> str:
    """Maps an error name onto its FlashError member.

    Accepts both spellings that circulate: the upper-case tag this project uses
    ("USB_ERROR") and the C++ enumerator ("UsbError"). Anything unrecognised
    becomes InternalError, which is the right answer for a name that does not
    exist - it means the caller invented it.
    """
    folded = kind.replace("_", "").lower()
    by_folded = {
        "success": "Success",
        "usberror": "UsbError",
        "protocolerror": "ProtocolError",
        "fileerror": "FileError",
        "flasherror": "FlashError",
        "autherror": "AuthError",
        "internalerror": "InternalError",
        "cancelled": "Cancelled",
        "canceled": "Cancelled",
    }
    return by_folded.get(folded, "InternalError")


def guard(vendor: str, operation: str) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """A decorator that re-raises anything from a backend call as a FlashException.

    Used on the Python wrappers around each vendor protocol, so a caller can
    write one `except huaxin_core.FlashException` block and know it will catch
    everything - including a plain RuntimeError from a protocol layer that has
    not been taught about the error system yet.
    """
    def decorate(function: Callable[..., T]) -> Callable[..., T]:
        def wrapper(*args: Any, **kwargs: Any) -> T:
            try:
                return function(*args, **kwargs)
            except BaseException as error:  # noqa: BLE001 - re-raised immediately
                raise_as_flash_error(error, vendor=vendor, operation=operation)
        wrapper.__name__ = getattr(function, "__name__", "wrapper")
        wrapper.__doc__ = function.__doc__
        return wrapper  # type: ignore[return-value]
    return decorate


def raise_as_flash_error(
    error: BaseException,
    *,
    vendor: str = "",
    operation: str = "",
) -> None:
    """Re-raises `error` as a classified FlashException.

    Always raises. If the native module is unavailable the original exception is
    re-raised unchanged, because inventing a classified failure without the
    classifier would be a guess.
    """
    if backend is None:
        raise error

    if isinstance(error, backend.FlashException):
        if not operation:
            raise
        # Add the frame the Python layer knows about and re-raise, keeping the
        # classification and every frame already on it.
        try:
            backend.raise_flash_error(
                error.error, error.vendor, error.message, operation
            )
        except backend.FlashException as combined:
            raise combined from error

    kind = classify(error)
    vendor_enum = _vendor_member(vendor)
    try:
        backend.raise_flash_error(
            getattr(backend.FlashError, _enum_member(kind)), vendor_enum, str(error), operation
        )
    except backend.FlashException as classified:  # pragma: no branch - always raises
        raise classified from error


def _vendor_member(vendor: str) -> Any:
    """Maps a vendor display name onto the Vendor enum member."""
    if backend is None:
        return None
    wanted = vendor.strip().lower()
    for name in ("Core", "Adb", "Fastboot", "Qualcomm", "MediaTek", "Unisoc", "Samsung"):
        if name.lower() == wanted:
            return getattr(backend.Vendor, name)
    # A vendor string that is not one of ours: the failure is still ours to
    # report, but it is not attributable to a protocol, so it goes out unknown.
    return backend.Vendor.Unknown


# -----------------------------------------------------------------------------
#  Retrying
# -----------------------------------------------------------------------------


def retry_policy_from(settings: Any) -> Any:
    """The native RetryPolicy a Settings object describes, or None."""
    if backend is None:
        return None
    return settings.retry_policy()


def current_policy() -> Any:
    """The process-wide policy, which the settings dialog keeps up to date.

    A caller that has no settings object to hand - a panel, a job function -
    reads this instead of inventing its own default, so there is one answer to
    "how many attempts" rather than several.
    """
    if backend is None:
        return None
    try:
        return backend.default_retry_policy()
    except Exception:  # noqa: BLE001
        return None


def run_with_retry(
    operation: Callable[[], T],
    policy: Any = None,
    *,
    on_retry: Callable[[int, FlashFailure], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> T:
    """Runs `operation`, retrying the failures that are worth retrying.

    The native layer does the same thing for work that happens inside C++, but a
    large part of this tool - ADB, Fastboot, file validation - runs entirely in
    Python, and a second, subtly different retry rule there is how the two halves
    end up disagreeing about what "three attempts" means.

    Only `retryable` failures are repeated, and a caller-supplied `should_stop`
    is consulted before every wait so a cancellation does not have to wait out a
    backoff.
    """
    attempts = 1
    initial_delay = 0
    maximum_delay = 0
    budget_ms = 0

    if policy is None:
        # Fall back to the process-wide policy rather than to "no retrying": a
        # caller that forgot to pass one should get the operator's configured
        # behaviour, not a silent behaviour change.
        policy = current_policy()

    if policy is not None:
        attempts = max(1, int(policy.attempts))
        initial_delay = max(0, int(policy.initial_delay_ms))
        maximum_delay = max(0, int(policy.maximum_delay_ms))
        budget_ms = max(0, int(policy.total_budget_ms))

    spent_ms = 0
    last_failure: FlashFailure | None = None

    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except BaseException as error:  # noqa: BLE001 - classified below
            failure = describe(error)
            last_failure = failure

            if not failure.retryable:
                raise

            if attempt >= attempts:
                raise

            delay = _delay_for(attempt, initial_delay, maximum_delay)
            if budget_ms and spent_ms + delay > budget_ms:
                raise
            if should_stop is not None and should_stop():
                raise

            if on_retry is not None:
                on_retry(attempt, failure)
            time.sleep(delay / 1000.0)
            spent_ms += delay

    # Unreachable: the loop either returns or raises. Kept so the type checker and
    # a reader both see that there is no silent fall-through.
    if last_failure is not None:  # pragma: no cover
        raise RuntimeError(last_failure.report())
    raise RuntimeError("run_with_retry finished without returning or raising")  # pragma: no cover


def _delay_for(attempt: int, initial: int, maximum: int) -> int:
    """Exponential backoff, capped. Mirrors the C++ backoff_delay_ms()."""
    if initial <= 0:
        return 0
    delay = initial << min(attempt - 1, 20)
    if maximum > 0:
        delay = min(delay, maximum)
    return delay


def log_failure(logger: Any, failure: FlashFailure) -> None:
    """Writes a failure to the native log at the severity its kind deserves.

    A logger that fails is swallowed, deliberately: this runs inside the handler
    for a real failure, and letting a logging error propagate would replace the
    thing that went wrong on the device with the thing that went wrong in the
    log. The same rule is stated in `BackendService._mirror_to_file`.
    """
    if logger is None:
        return
    if failure.kind == "INTERNAL_ERROR":
        level = "critical"
    elif failure.kind == "CANCELLED":
        level = "warning"
    else:
        level = "error"
    try:
        logger.write(level, failure.report())
    except Exception:  # noqa: BLE001 - never let logging raise
        pass
