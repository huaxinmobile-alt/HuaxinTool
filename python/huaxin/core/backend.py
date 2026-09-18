"""Python side of the native bridge, plus the Qt-facing service that owns it.

Two layers, deliberately separate:

`Backend`
    A thin, thread-confined wrapper over the `huaxin_core` extension module. It
    holds no Qt objects, emits no signals, and refuses to run anywhere except the
    thread that opened it. This is what job functions call.

`BackendService`
    A QObject that owns the worker thread and the Backend, turns every
    operation into a Job, and republishes the results as Qt signals. This is
    what the UI talks to.

Nothing in the UI is allowed to touch `Backend` directly - the thread assertion
in `Backend._assert_owner()` turns that rule into an immediate, loud failure
instead of a rare heisenbug.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import QObject, pyqtSignal

from huaxin.core import progress as progress_module
from huaxin.core.config import Settings, archive_session_log, load_settings
from huaxin.workers.worker import Job, JobContext, WorkerThread

__all__ = [
    "Backend",
    "BackendService",
    "BackendUnavailableError",
    "Device",
    "JOB_APPLY_SETTINGS",
    "JOB_OPEN",
    "JOB_SCAN",
    "JOB_SHUTDOWN",
]

# Job names. They must be unique among in-flight jobs because result handlers
# are registered by name; BackendService.submit() enforces that.
JOB_OPEN = "backend.open"
JOB_SCAN = "scan-devices"
JOB_SHUTDOWN = "backend.shutdown"
JOB_APPLY_SETTINGS = "backend.apply-settings"

# <repo>/python - where CMake stages the compiled extension module.
PYTHON_ROOT = Path(__file__).resolve().parents[2]

# VID/PID -> vendor and mode mapping lives in C++ (core/device_catalog.cpp), not
# here. It is deliberately single-sourced: the same table drives the UI, the
# logs and, from Phase 4 on, which protocol handler a device is routed to. A
# second copy in Python would drift out of step with it.


class BackendUnavailableError(RuntimeError):
    """The compiled `huaxin_core` module could not be imported."""


def _import_native_module() -> Any:
    """Import the compiled backend, with a message that says how to fix it."""
    if str(PYTHON_ROOT) not in sys.path:
        sys.path.insert(0, str(PYTHON_ROOT))
    try:
        import huaxin_core  # noqa: PLC0415 - imported lazily, see below
    except ImportError as exc:
        raise BackendUnavailableError(
            f"The native backend 'huaxin_core' is not importable ({exc}).\n"
            f"Looked in: {PYTHON_ROOT}\n"
            "Build it with:  ./scripts/build.ps1   (Windows)\n"
            "                ./scripts/build.sh    (Linux, macOS)"
        ) from exc
    return huaxin_core


def _exception_summary(tb: str, error_type: str) -> str:
    """Pull the human-readable cause out of a formatted traceback.

    The header line of the last exception in a chain is where the message
    starts - `SomeError: the actual problem`. Taking the last line of the
    traceback instead would clip a multi-line message down to its final
    fragment, which is exactly the part that explains nothing.
    """
    marker = f"{error_type}:"
    header = next((line for line in reversed(tb.splitlines()) if marker in line), "")
    if header.strip():
        return header.split(marker, 1)[1].strip() or error_type
    return next((line.strip() for line in reversed(tb.splitlines()) if line.strip()), error_type)


@dataclass(frozen=True, slots=True)
class Device:
    """Immutable snapshot of a device as it appeared at scan time.

    Plain Python data on purpose: the UI must never hold a reference to a C++
    object that the worker thread could invalidate while the user is looking at
    the list. The worker converts native DeviceInfo objects into these and hands
    copies across the thread boundary.

    The string fields are best effort. `product`, `manufacturer` and `serial`
    are empty when the device could not be opened - on Windows that means no
    WinUSB-class driver is bound to it, which is the normal state for a device
    that has not been prepared for flashing yet.
    """

    vid: int
    pid: int
    bus: int
    port: int
    address: int
    description: str
    vendor: str
    mode: str
    phase: str
    product: str
    manufacturer: str
    serial: str
    kind: str
    recognised: bool
    verified: bool
    is_root_hub: bool

    @property
    def usb_id(self) -> str:
        """VID:PID as lowercase hex, e.g. '05c6:9008'."""
        return f"{self.vid:04x}:{self.pid:04x}"

    @property
    def location(self) -> str:
        """Where it is plugged in, e.g. '1:9'. Falls back to the bus alone."""
        return f"{self.bus}:{self.port}" if self.port else f"bus {self.bus}"

    @property
    def identity(self) -> str:
        """Best human-recognisable name: product string, else vendor, else the label."""
        return self.product or self.manufacturer or self.description

    @property
    def target_label(self) -> str:
        """What the operator sees in the mode column."""
        if not self.recognised:
            return "—"
        return f"{self.mode} (unverified)" if not self.verified else self.mode

    @property
    def detail(self) -> str:
        """Full multi-line description for a tooltip."""
        lines = [
            f"{self.usb_id}   {self.description}",
            f"Bus {self.bus}, port {self.port}, address {self.address}",
            f"Backend kind: {self.kind}",
        ]
        if self.recognised:
            lines.append(f"Protocol: {self.phase}" + ("" if self.verified else " (VID:PID unverified)"))
        else:
            lines.append("Not a recognised flashing target")
        for label, value in (
            ("Product", self.product),
            ("Manufacturer", self.manufacturer),
            ("Serial", self.serial),
        ):
            if value:
                lines.append(f"{label}: {value}")
        return "\n".join(lines)

    @classmethod
    def from_native(cls, info: Any) -> "Device":
        kind = getattr(info, "kind", None)
        return cls(
            vid=int(info.vid),
            pid=int(info.pid),
            bus=int(info.bus_number),
            port=int(info.port_number),
            address=int(info.device_address),
            description=str(info.description),
            vendor=str(info.vendor),
            mode=str(info.mode),
            phase=str(info.phase),
            product=str(info.product),
            manufacturer=str(info.manufacturer),
            serial=str(info.serial),
            kind=str(getattr(kind, "name", kind)),
            recognised=bool(info.recognised),
            verified=bool(info.verified),
            is_root_hub=bool(info.is_root_hub),
        )


class Backend:
    """Thread-confined owner of the native HardwareBridge.

    Every method below runs on the worker thread and asserts it. API-wise it is
    a plain object with no Qt dependency, so it stays testable without a
    QApplication.
    """

    def __init__(self) -> None:
        self._native: Any = None
        self._bridge: Any = None
        self._owner_thread: int | None = None
        self._devices: tuple[Device, ...] = ()

    # -- thread confinement ------------------------------------------------

    def _bind_to_current_thread(self) -> None:
        """Claim the calling thread as the owner, once."""
        if self._owner_thread is None:
            self._owner_thread = threading.get_ident()

    def _assert_owner(self) -> None:
        if self._owner_thread is None:
            raise RuntimeError("Backend.open() has not run yet")
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError(
                "Backend called from the wrong thread. All backend access must go "
                "through BackendService/WorkerThread; the UI thread must never call "
                "into the native bridge directly."
            )

    def _assert_open(self) -> None:
        if self._bridge is None:
            raise RuntimeError("Backend is not open; the native bridge failed to initialise")

    # -- properties --------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._bridge is not None

    @property
    def devices(self) -> tuple[Device, ...]:
        """Last scan result. Read-only snapshot, safe to read from anywhere."""
        return self._devices

    @property
    def version(self) -> str:
        return str(self._native.__version__) if self._native is not None else "unavailable"

    # -- operations (worker thread only) -----------------------------------

    def apply_settings(self, ctx: JobContext, settings: Any) -> None:
        """Stores the settings and pushes the retry policy and timeouts native.

        Kept on the worker thread for the same reason every other backend call
        is: the native side is thread-confined, and a settings change that raced
        a flash would be the worst possible time for one.
        """
        self._settings = settings

        if self._native is not None:
            policy = settings.retry_policy()
            if policy is not None:
                self._native.set_default_retry_policy(policy)
                ctx.log(
                    "retry policy: "
                    f"{policy.attempts} attempt(s), first delay {policy.initial_delay_ms} ms, "
                    f"ceiling {policy.maximum_delay_ms} ms, budget {policy.total_budget_ms} ms",
                    "debug",
                )

            # Milliseconds for the native layer; the settings hold seconds.
            command_ms = int(settings.default_timeout * 1000)
            transfer_ms = int(settings.transfer_timeout * 1000)
            connect_ms = int(settings.connect_timeout * 1000)
            self._native.set_timeout_overrides(command_ms, transfer_ms, connect_ms)
            ctx.log(
                f"timeouts: command {settings.default_timeout:g} s, "
                f"transfer {settings.transfer_timeout:g} s, "
                f"connect {settings.connect_timeout:g} s",
                "debug",
            )

            # Zero in the settings means "no cap", which is what the native side
            # expects too, so no translation is needed.
            self._native.set_speed_limit(int(settings.speed_limit))
            if settings.speed_limit > 0:
                ctx.log(
                    f"transfer speed capped at {settings.speed_limit / 1024:.0f} KB/s",
                    "warn",
                )

        ctx.log(f"settings applied (log level {settings.log_level})", "debug")

    def open(self, ctx: JobContext) -> tuple[str, str]:
        """Import the module and bring up the bridge.

        Returns (backend_version, libusb_version) so the UI can report what it is
        actually linked against without calling back into the backend from the
        UI thread.
        """
        self._bind_to_current_thread()
        self._assert_owner()

        native = _import_native_module()
        self._native = native
        ctx.log(f"loaded native module huaxin_core {native.__version__}")
        ctx.log(f"module path: {native.__file__}", "debug")

        bridge = native.HardwareBridge()
        if not bridge.init():
            detail = bridge.last_error()
            raise RuntimeError(f"HardwareBridge.init() failed: {detail}" if detail
                               else "HardwareBridge.init() failed")
        self._bridge = bridge
        ctx.log(f"libusb {bridge.libusb_version()}", "debug")
        ctx.log(f"VID/PID catalogue holds {bridge.known_target_count()} known targets", "debug")
        ctx.log(f"hardware bridge ready (backend {bridge.backend_version()})", "ok")
        return bridge.backend_version(), bridge.libusb_version()

    def scan(
        self,
        ctx: JobContext,
        read_string_descriptors: bool = True,
        include_root_hubs: bool = False,
    ) -> list[Device]:
        """Enumerate the USB bus through libusb.

        The native call releases the GIL, so the Qt event loop keeps running
        while this works - but it still blocks *this* thread, which is why it
        must never be called from the UI thread.
        """
        self._assert_owner()
        self._assert_open()

        ctx.progress(-1, "enumerating USB devices")
        ctx.log("enumerating USB devices"
                + ("" if read_string_descriptors else " (skipping string descriptors)"))

        started = time.perf_counter()
        native_infos = self._bridge.get_device_list(read_string_descriptors, include_root_hubs)
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        devices = tuple(Device.from_native(info) for info in native_infos)
        self._devices = devices

        targets = sum(1 for device in devices if device.recognised)
        ctx.log(
            f"found {len(devices)} USB device(s), {targets} recognised flashing target(s) "
            f"in {elapsed_ms:.0f} ms",
            "ok" if devices else "warn",
        )
        if not devices:
            ctx.log(
                "libusb sees no USB devices at all. That normally means the service is "
                "running without access to the USB stack.",
                "warn",
            )
        ctx.progress(100, "done")
        return list(devices)

    def close(self) -> None:
        """Release native resources. Called on the worker thread at shutdown."""
        bridge, self._bridge = self._bridge, None
        if bridge is None:
            return
        bridge.shutdown()
        self._native = None
        ctx_owner = self._owner_thread
        self._owner_thread = None
        del ctx_owner  # the thread identity is re-claimed on the next open()


class BackendService(QObject):
    """Qt-facing facade: submits jobs to the worker thread, republishes results.

    Every signal below is emitted on the worker thread and delivered to slots on
    the thread that owns this object (the UI thread), because Qt queues
    cross-thread signal deliveries. Handlers registered with `submit()` are
    invoked from those slots, so they too run on the UI thread and may touch
    widgets.
    """

    log = pyqtSignal(str, str)               # level, message
    backend_state_changed = pyqtSignal(str)  # starting | ready | unavailable | stopped
    devices_changed = pyqtSignal(object)     # list[Device]
    scan_started = pyqtSignal()
    scan_finished = pyqtSignal(bool)         # succeeded
    busy_changed = pyqtSignal(bool)
    job_failed = pyqtSignal(str, str, str)   # job name, error type, traceback
    #: job name, percent (-1 when unknown), message. Republished on the UI thread
    #: so a panel can drive a progress bar without connecting to the worker.
    job_progress_changed = pyqtSignal(str, int, str)
    #: job name, a `huaxin.core.progress.JobProgress`. The same event as the
    #: signal above carrying what a percentage cannot: speed, byte counts and an
    #: ETA. That is the difference between "43%" and an answer to the only
    #: question somebody watching a flash actually has, which is "is it stuck?".
    job_progress_detail = pyqtSignal(str, object)
    #: job name, emitted when a job starts running on the worker. Lets a status
    #: display name the operation before it has reported any progress - a job
    #: that takes ten seconds to reach its first update would otherwise show
    #: nothing at all for those ten seconds.
    job_started = pyqtSignal(str)
    #: Job name, for every job, on every outcome. Lets a panel clear a progress
    #: bar it started without having to enumerate the ways a job can end.
    job_finished = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._backend = Backend()
        self._worker = WorkerThread(self)
        self._devices: tuple[Device, ...] = ()
        self._handlers: dict[str, tuple[Callable[[Any], None] | None, Callable[[str, str], None] | None]] = {}
        self._scan_in_flight = False
        self._state = "stopped"
        self._libusb_version = ""
        self._logger: Any = None
        self._log_path = ""
        # Filled at startup from the settings file; see _open_log().
        self._settings: Settings | None = None

        # Every line the UI shows is also written to flash_log.txt, so the file
        # is one chronological record rather than only the C++ half of it.
        self.log.connect(self._mirror_to_file)

        # Not `connect(self.log)`: job_log carries (job, level, message) and
        # connecting it straight to the 2-argument `log` signal would make Qt
        # silently drop the trailing argument and deliver (job, level).
        self._worker.job_log.connect(self._on_job_log)
        self._worker.job_progress.connect(self._on_progress)
        self._worker.job_progress_detail.connect(self._on_progress_detail)
        self._worker.job_started.connect(self.job_started)
        self._worker.job_succeeded.connect(self._on_succeeded)
        self._worker.job_failed.connect(self._on_failed)
        self._worker.job_cancelled.connect(self._on_cancelled)
        self._worker.job_finished.connect(self._on_finished)
        self._worker.queue_depth_changed.connect(self._on_depth_changed)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the worker thread and the backend, in that order."""
        if self._worker.isRunning():
            return
        self._open_log()
        self._worker.set_finalizer(self._backend.close)
        self._set_state("starting")
        self._worker.start()
        self.submit(
            JOB_OPEN,
            self._backend.open,
            on_success=self._on_backend_ready,
            on_failure=lambda _kind, _tb: self._set_state("unavailable"),
        )

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        """Stop the worker and release the native bridge. Safe to call twice."""
        if not self._worker.isRunning():
            self._set_state("stopped")
            return True
        self.log.emit("info", "shutting down the backend")
        stopped = self._worker.shutdown(timeout_ms)
        if not stopped:
            self.log.emit(
                "error",
                f"worker thread did not stop within {timeout_ms} ms "
                "(a hardware operation is still blocking); not forcing it",
            )
        self._set_state("stopped")
        return stopped

    # -- public API --------------------------------------------------------

    @property
    def devices(self) -> tuple[Device, ...]:
        return self._devices

    @property
    def state(self) -> str:
        return self._state

    @property
    def libusb_version(self) -> str:
        """Version of the linked libusb, or "" before the backend has started."""
        return self._libusb_version

    @property
    def scan_in_flight(self) -> bool:
        """True while a request_scan is queued or running.

        Exposed so the periodic refresh can skip a scan it would only be refused:
        `request_scan` already refuses a second one, but asking first keeps the
        reason in the caller where it can be read.
        """
        return self._scan_in_flight

    @property
    def is_busy(self) -> bool:
        return self._worker.pending > 0

    @property
    def can_cancel_current(self) -> bool:
        """True when the running job opted into cooperative cancellation."""
        return self._worker.can_cancel_current

    @property
    def log_path(self) -> str:
        """Where flash_log.txt was written, or "" when logging is unavailable."""
        return self._log_path

    def _open_log(self) -> None:
        """Opens the file log. A failure here is reported but never fatal: a tool
        that refuses to run because it cannot write a log would be worse than one
        that runs without one.

        The settings decide the destination, the minimum level and whether a file
        is written at all. They are read here, before the worker starts, so the
        very first line the tool emits already goes to the right place - a
        minimum level applied afterwards would silently swallow the startup lines
        that say what went wrong.
        """
        try:
            native = _import_native_module()
            self._logger = native.Logger.instance()

            settings = self._settings
            if settings is None:
                settings = load_settings()[0]
                self._settings = settings

            if not settings.log_to_file:
                self._logger.file_enabled = False
                self._log_path = ""
                self._logger.console_enabled = bool(settings.log_to_console)
                self._logger.set_min_level(settings.log_level)
                return

            path = settings.log_file.strip() or self._logger.default_path()
            if self._logger.open(path):
                self._log_path = str(self._logger.path())
            else:
                self._log_path = ""
                self.log.emit("warn", f"could not open the log file at {path}")

            self._logger.console_enabled = bool(settings.log_to_console)
            if settings.log_max_bytes > 0:
                self._logger.max_file_bytes = settings.log_max_bytes
            self._logger.set_min_level(settings.log_level)
        except Exception as exc:  # noqa: BLE001 - logging must not break startup
            self._logger = None
            self._log_path = ""
            self.log.emit("warn", f"file logging is unavailable: {exc}")

    def apply_settings(self, settings: Any) -> bool:
        """Pushes new settings into the live log and backend.

        The log is updated here, on the UI thread, because the Logger is
        process-wide and every thread writes to it. The backend is updated
        through a job, because the Backend is thread-confined and reaching into
        it from the UI is the exact cross-thread access the rest of this class
        exists to prevent.

        Returns False when the backend is not running. That is not a failure: the
        settings are stored and will apply on the next start.
        """
        self._settings = settings

        if self._logger is not None:
            try:
                settings.apply_to_logger(self._logger)
                self._log_path = str(self._logger.path()) if self._logger.is_open() else ""
            except Exception as exc:  # noqa: BLE001 - a settings change must not raise
                self.log.emit("warn", f"could not apply the logging settings: {exc}")

        if not self._worker.isRunning():
            return False

        return self.submit(
            JOB_APPLY_SETTINGS,
            self._backend.apply_settings,
            settings,
            on_failure=lambda kind, _tb: self.log.emit(
                "warn", f"the backend did not accept the new settings ({kind})"
            ),
        )

    def _mirror_to_file(self, level: str, message: str) -> None:
        if self._logger is None:
            return
        try:
            self._logger.write(level, message)
        except Exception:  # noqa: BLE001 - never let logging raise
            pass

    def close_log(self) -> None:
        """Archives the session log if asked to, then closes it.

        The archive is taken after the close, so the copy includes the closing
        line and nothing is written to the file while it is being copied. A
        failure here is reported and then ignored: the log itself is already on
        disk, and refusing to exit because a convenience copy failed would be
        absurd.
        """
        settings = self._settings
        path = self._log_path

        if self._logger is not None:
            try:
                self._logger.close()
            except Exception:  # noqa: BLE001
                pass

        if settings is None or not settings.save_logs_automatically or not path:
            return
        try:
            archived = archive_session_log(Path(path))
        except Exception as exc:  # noqa: BLE001 - closing must not fail
            self.log.emit("warn", f"could not keep a copy of the session log: {exc}")
            return
        if archived is not None:
            self.log.emit("info", f"session log kept at {archived}")

    def log_message(self, level: str, message: str) -> None:
        """Emit a UI-originated log line through the same channel as backend ones."""
        self.log.emit(level, message)

    def submit(
        self,
        name: str,
        fn: Callable[..., Any],
        *args: Any,
        cancellable: bool = False,
        on_success: Callable[[Any], None] | None = None,
        on_failure: Callable[[str, str], None] | None = None,
        **kwargs: Any,
    ) -> bool:
        """Queue backend work. Returns False if the job was rejected.

        `on_success` / `on_failure` run on the UI thread (see class docstring).
        """
        if name in self._handlers:
            self.log.emit("error", f"job '{name}' is already in flight; refusing to queue a second one")
            return False

        self._handlers[name] = (on_success, on_failure)
        try:
            self._worker.submit(Job(name=name, fn=fn, args=args, kwargs=kwargs, cancellable=cancellable))
        except RuntimeError as exc:
            self._handlers.pop(name, None)
            self.log.emit("warn", str(exc))
            return False
        return True

    def request_scan(
        self,
        read_string_descriptors: bool = True,
        include_root_hubs: bool = False,
    ) -> bool:
        """Ask for a device scan. Ignored if one is already running.

        Both options come from the settings dialog and both change what the scan
        does: `read_string_descriptors` trades time for a readable device list,
        `include_root_hubs` decides whether the host controllers are listed
        alongside the devices.
        """
        if self._scan_in_flight:
            self.log.emit("warn", "a device scan is already running")
            return False
        if self._state == "stopped":
            self.log.emit("error", "cannot scan: the backend is not running")
            return False

        self._scan_in_flight = True
        self.scan_started.emit()
        submitted = self.submit(
            JOB_SCAN,
            self._backend.scan,
            read_string_descriptors,
            include_root_hubs,
            on_success=self._on_scan_success,
            on_failure=self._on_scan_failure,
        )
        if not submitted:
            self._scan_in_flight = False
            self.scan_finished.emit(False)
        return submitted

    def cancel_current(self) -> bool:
        """Cancel the running operation, or the next queued one if it has not started.

        Returns False only when nothing in flight supports cancellation - a
        blocking C++ call with no cooperative check inside it.
        """
        cancelled = self._worker.cancel()
        if cancelled:
            target = self._worker.current_job or "the next queued job"
            self.log.emit("warn", f"cancellation requested for '{target}'")
        else:
            self.log.emit("warn", "the running operation cannot be cancelled")
        return cancelled

    # -- worker signal handlers (UI thread) --------------------------------

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.backend_state_changed.emit(state)

    def _on_job_log(self, job_name: str, level: str, message: str) -> None:
        """Republish a backend log line from the worker thread onto the UI channel."""
        self.log.emit(level, message)

    def _on_progress(self, job_name: str, percent: int, message: str) -> None:
        if message and percent < 0:
            self.log.emit("debug", f"{job_name}: {message}")
        self.job_progress_changed.emit(job_name, percent, message)

    def _on_progress_detail(self, job_name: str, progress: Any) -> None:
        """Normalises a rich progress report before it reaches the interface.

        Normalising here rather than in the display means every consumer - the
        status bar, a panel's bar, anything added later - reads the same shape,
        and the knowledge that the native struct and a Python-built one differ
        lives in exactly one place. See huaxin.core.progress.
        """
        self.job_progress_detail.emit(job_name, progress_module.from_native(progress))

    def _on_succeeded(self, job_name: str, result: Any) -> None:
        on_success, _ = self._handlers.pop(job_name, (None, None))
        if job_name != JOB_OPEN:
            self.log.emit("debug", f"job '{job_name}' succeeded")
        if on_success is not None:
            try:
                on_success(result)
            except Exception:  # a broken UI handler must not take the app down
                import traceback

                self.log.emit("error", f"handler for '{job_name}' raised:\n{traceback.format_exc()}")

    def _on_failed(self, job_name: str, error_type: str, tb: str) -> None:
        _, on_failure = self._handlers.pop(job_name, (None, None))
        self.log.emit("error", f"job '{job_name}' failed with {error_type}: {_exception_summary(tb, error_type)}")
        self.log.emit("debug", tb.rstrip())
        self.job_failed.emit(job_name, error_type, tb)
        if on_failure is not None:
            try:
                on_failure(error_type, tb)
            except Exception:
                import traceback

                self.log.emit("error", f"failure handler for '{job_name}' raised:\n{traceback.format_exc()}")

    def _on_cancelled(self, job_name: str) -> None:
        _, on_failure = self._handlers.pop(job_name, (None, None))
        self.log.emit("warn", f"job '{job_name}' was cancelled")
        if on_failure is not None:
            try:
                on_failure("Cancelled", "")
            except Exception:
                pass

    def _on_finished(self, job_name: str) -> None:
        # Runs for success, failure and cancellation alike, so busy state can
        # never get stuck. job_succeeded/_on_cancelled already popped the handler
        # in the normal cases; this is the safety net.
        self._handlers.pop(job_name, None)
        self.job_finished.emit(job_name)
        self.busy_changed.emit(self.is_busy)

    def _on_depth_changed(self, _depth: int) -> None:
        self.busy_changed.emit(self.is_busy)

    # -- result handlers (UI thread) ---------------------------------------

    def _on_backend_ready(self, result: Any) -> None:
        version, self._libusb_version = result
        self._set_state("ready")
        self.log.emit("ok", f"backend ready (version {version}, libusb {self._libusb_version})")

    def _on_scan_success(self, result: Any) -> None:
        self._scan_in_flight = False
        self._devices = tuple(result) if result else ()
        self.devices_changed.emit(list(self._devices))
        self.scan_finished.emit(True)

    def _on_scan_failure(self, _error_type: str, _tb: str) -> None:
        self._scan_in_flight = False
        self.scan_finished.emit(False)
