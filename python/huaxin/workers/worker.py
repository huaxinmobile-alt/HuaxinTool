"""QThread based job execution - the only place backend work is allowed to run.

Design: one long-lived worker thread that drains a FIFO job queue.

Why not a thread per operation
------------------------------
Every operation this tool performs touches a device that can only be talked to
by one conversation at a time: you cannot interleave a Firehose write with a
partition table read, and re-entering a flash session corrupts it. A single
thread draining an ordered queue gives us that serialisation for free, keeps the
stateful C++ bridge single-threaded, and makes the order of log lines match the
order in which the user clicked.

Delivery model
--------------
    UI thread      --submit(Job)-->  queue  -->  worker thread runs job.fn(ctx)
    worker thread  --pyqtSignal-->   Qt queued connection  -->  UI thread slots

Signals are emitted from the worker thread but delivered by Qt's event loop to
the thread that owns the receiving object - the UI thread. No widget is ever
touched from the worker, and the worker never blocks waiting on the UI, so this
pair cannot deadlock.

Cancellation
------------
Cooperative, and it works in two places: a job that has already started observes
the request when it polls `ctx.check_cancelled()`, and a job still sitting in the
queue is dropped without being run at all. The second case matters because a
Cancel click can easily land in the gap between `submit()` returning and the
worker picking the job up - cancelling only the running job would silently do
nothing there.

A job blocked inside a single long C++ call cannot be interrupted from Python;
per-operation timeouts belong to the transport layer in C++.
"""

from __future__ import annotations

import queue
import threading
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Final

from PyQt6.QtCore import QThread, pyqtSignal

__all__ = ["Job", "JobCancelled", "JobContext", "WorkerThread"]

# Sentinel pushed onto the queue to break the worker's loop. A unique object()
# cannot collide with a real Job, unlike a sentinel string or None.
_STOP: Final = object()


class JobCancelled(Exception):
    """Raised inside a job when a cooperative cancellation request is observed."""


@dataclass(slots=True)
class Job:
    """One unit of backend work.

    `fn` is called on the worker thread as ``fn(ctx, *args, **kwargs)`` - the
    JobContext always comes first so every job can log and check for
    cancellation without any extra plumbing.
    """

    name: str
    fn: Callable[..., Any]
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)

    #: Opt in to cancellation. A job that never polls `ctx.check_cancelled()`
    #: cannot be interrupted mid-flight, but can still be cancelled before it
    #: starts if it is queued.
    cancellable: bool = False

    #: Owned by the job so a queued job can be cancelled before the worker ever
    #: looks at it. Shared with the JobContext while the job runs.
    cancel_event: threading.Event = field(default_factory=threading.Event)


class JobContext:
    """Handle a running job uses to report progress and observe cancellation.

    Valid only for the duration of the job and only on the worker thread. It is
    deliberately not a QObject: it never crosses a thread boundary.
    """

    __slots__ = ("_name", "_emit_progress", "_emit_log", "_emit_detail", "_cancel_event", "_closed")

    def __init__(
        self,
        name: str,
        emit_progress: Callable[[str, int, str], None],
        emit_log: Callable[[str, str, str], None],
        cancel_event: threading.Event,
        emit_detail: Callable[[str, object], None] | None = None,
    ) -> None:
        self._name = name
        self._emit_progress = emit_progress
        self._emit_log = emit_log
        self._emit_detail = emit_detail
        self._cancel_event = cancel_event
        self._closed = False

    @property
    def name(self) -> str:
        return self._name

    def log(self, message: str, level: str = "info") -> None:
        """Emit a log line. Levels: debug, info, ok, warn, error."""
        if not self._closed:
            self._emit_log(self._name, level, str(message))

    def progress(self, percent: int, message: str = "") -> None:
        """Report progress. `percent` < 0 means "busy, duration unknown"."""
        if not self._closed:
            self._emit_progress(self._name, int(percent), str(message))

    def detail(self, progress: object, message: str = "") -> None:
        """Report a full `FlashProgress` alongside the plain percentage.

        The percentage alone is enough to drive a bar, and not enough to tell an
        operator anything: "43%" does not answer "is this stuck?", which is the
        only question anybody watching a flash actually has. Speed, elapsed time
        and an estimate of what is left answer it, and they are already computed
        in the native core - so they are carried through here rather than guessed
        at in the interface.

        The message is also sent as an ordinary progress line, so a caller that
        only understands percentages still sees the text.
        """
        if self._closed:
            return
        percent = int(getattr(progress, "percentage", -1))
        if message:
            self._emit_progress(self._name, percent, str(message))
        if self._emit_detail is not None:
            self._emit_detail(self._name, progress)

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def check_cancelled(self) -> None:
        """Raise JobCancelled if a cancellation was requested."""
        if self._cancel_event.is_set():
            raise JobCancelled(f"job '{self._name}' was cancelled")

    def close(self) -> None:
        """Invalidate the context once the job is over."""
        self._closed = True


class WorkerThread(QThread):
    """Serialises every backend call onto one background thread.

    Signals
    -------
    job_started(job_name)
    job_progress(job_name, percent, message)
    job_log(job_name, level, message)
    job_succeeded(job_name, result)
    job_failed(job_name, error_type, traceback_text)
    job_cancelled(job_name)
    job_finished(job_name)        always emitted: success, failure, cancel or skip
    queue_depth_changed(depth)
    """

    job_started = pyqtSignal(str)
    job_progress = pyqtSignal(str, int, str)
    #: job name, a native progress object. Carries speed, byte counts and an ETA
    #: that the percentage channel above cannot express; see JobContext.detail().
    job_progress_detail = pyqtSignal(str, object)
    job_log = pyqtSignal(str, str, str)
    job_succeeded = pyqtSignal(str, object)
    job_failed = pyqtSignal(str, str, str)
    job_cancelled = pyqtSignal(str)
    job_finished = pyqtSignal(str)
    queue_depth_changed = pyqtSignal(int)

    def __init__(self, parent: Any = None, queue_limit: int = 64) -> None:
        super().__init__(parent)
        self.setObjectName("huaxin-worker")
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_limit)
        self._lock = threading.Lock()
        self._current: Job | None = None
        #: Jobs submitted but not yet started, by name, used to cancel them
        #: before they run. Insertion-ordered, so "oldest" is well defined.
        self._queued: dict[str, Job] = {}
        self._pending = 0
        self._accepting = True
        self._finalizer: Callable[[], None] | None = None

    # -- lifecycle ---------------------------------------------------------

    def set_finalizer(self, fn: Callable[[], None] | None) -> None:
        """Register a callable run on the worker thread just before it exits.

        Used to release backend resources on the owning thread without racing
        the job queue: a shutdown job could be dropped by queue draining, a
        finalizer cannot.
        """
        self._finalizer = fn

    def submit(self, job: Job) -> None:
        """Queue a job. Raises RuntimeError if the worker is shutting down."""
        with self._lock:
            if not self._accepting:
                raise RuntimeError("Worker is shutting down; job rejected")
            self._pending += 1
            self._queued[job.name] = job
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            with self._lock:
                self._pending -= 1
                self._queued.pop(job.name, None)
            raise RuntimeError(
                f"Worker queue is full ({self._queue.maxsize} jobs); "
                "is something flooding the backend?"
            ) from None
        self.queue_depth_changed.emit(self.pending)

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        """Stop accepting work, abandon queued jobs, cancel the running one.

        Returns True if the thread exited within `timeout_ms`. A False return is
        reported rather than forced with terminate(), which would leave a USB
        transfer half-written - worse than a logged warning.
        """
        with self._lock:
            self._accepting = False
        self.cancel()
        self._drop_pending()
        try:
            self._queue.put(_STOP, timeout=1.0)
        except queue.Full:  # pragma: no cover - drain above should prevent this
            return False
        return self.wait(timeout_ms)

    # -- state -------------------------------------------------------------

    @property
    def pending(self) -> int:
        """Jobs submitted but not yet finished, including the running one."""
        with self._lock:
            return self._pending

    @property
    def current_job(self) -> str | None:
        with self._lock:
            return self._current.name if self._current else None

    @property
    def can_cancel_current(self) -> bool:
        """True when a cancellation request would actually reach a job."""
        with self._lock:
            if self._current is not None and self._current.cancellable:
                return True
            return any(job.cancellable for job in self._queued.values())

    def cancel(self, job_name: str | None = None) -> bool:
        """Request cooperative cancellation. Returns True if it was delivered.

        With `job_name`, targets that job whether it is running or still queued.
        Without one, targets the running job if it opted in, otherwise the oldest
        queued job that did - so a Cancel pressed in the gap between submitting a
        job and the worker picking it up still lands on the right operation.

        Deliberately not QThread.requestInterruption(): that flag is sticky for
        the whole thread run, and this thread is reused for every job.
        """
        with self._lock:
            target: Job | None
            if job_name is not None:
                if self._current is not None and self._current.name == job_name:
                    target = self._current
                else:
                    target = self._queued.get(job_name)
            elif self._current is not None and self._current.cancellable:
                target = self._current
            else:
                target = next((job for job in self._queued.values() if job.cancellable), None)

            if target is None or not target.cancellable:
                return False
            target.cancel_event.set()
            return True

    # -- worker thread -----------------------------------------------------

    def run(self) -> None:
        """Runs on the worker thread: drain the queue until told to stop."""
        while True:
            job = self._queue.get()
            if job is _STOP:
                break
            self._execute(job)

        if self._finalizer is not None:
            try:
                self._finalizer()
            except Exception:  # never let teardown kill the thread silently
                traceback.print_exc()

    def _execute(self, job: Job) -> None:
        with self._lock:
            self._queued.pop(job.name, None)
            self._current = job

        if job.cancel_event.is_set():
            # Cancelled while queued: a flash job that was called off must not
            # touch the device at all, so it is dropped rather than started.
            self.job_cancelled.emit(job.name)
            self._finish(job)
            return

        ctx = JobContext(job.name, self.job_progress.emit, self.job_log.emit,
                         job.cancel_event, self.job_progress_detail.emit)
        self.job_started.emit(job.name)

        try:
            result = job.fn(ctx, *job.args, **job.kwargs)
        except JobCancelled:
            self.job_cancelled.emit(job.name)
        except Exception as exc:
            # The whole point of this boundary: a failed job is data, not a
            # crash. The thread survives and the next job still runs.
            self.job_failed.emit(job.name, type(exc).__name__, traceback.format_exc())
        else:
            self.job_succeeded.emit(job.name, result)
        finally:
            ctx.close()
            self._finish(job)

    def _finish(self, job: Job) -> None:
        """Single exit path, so the pending counter can never leak."""
        with self._lock:
            if self._current is job:
                self._current = None
            self._pending = max(0, self._pending - 1)
            pending = self._pending
        self.job_finished.emit(job.name)
        self.queue_depth_changed.emit(pending)

    def _drop_pending(self) -> None:
        """Discard not-yet-started jobs, keeping the pending counter honest."""
        dropped = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is _STOP:  # pragma: no cover - defensive
                continue
            dropped += 1
        with self._lock:
            self._queued.clear()
            if dropped:
                self._pending = max(0, self._pending - dropped)
            pending = self._pending
        if dropped:
            self.queue_depth_changed.emit(pending)
