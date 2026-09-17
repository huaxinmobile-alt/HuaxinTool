"""The live readout of whatever is running, for the status bar.

WHY THIS IS NOT JUST A PROGRESS BAR. A bar answers "how far along is it?", which
nobody actually asks. The question an operator watching a 4 GB image crawl down a
USB 2.0 cable has is "is it stuck?", and a bar at 43% answers that question not at
all - 43% could be a device writing steadily or a device that stopped responding
ninety seconds ago.

So this shows the four things that answer it:

* **what** is happening, by name;
* **how fast**, in the units the vendor's own tool reports, so the two can be
  compared;
* **how long it has been going**, which is the number that exposes a stall; and
* **how long is left**, marked "~" whenever it is derived rather than reported.

THE RULE ABOUT THE "~". An ETA from a device's own tracker is a fact and is shown
plain. An ETA this widget calculated from the rate of progress so far is an
estimate, and is shown with a tilde. Presenting the second as the first is how an
operator ends up staring at "2 minutes left" for twenty, and it is the single most
common way a flashing tool loses somebody's trust.
"""

from __future__ import annotations

import time

from PyQt6.QtCore import Qt, QTimer, pyqtSlot
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from huaxin.core.progress import JobProgress, format_bytes, format_duration, format_speed
from huaxin.ui.components import CircularProgress
from huaxin.ui.safe_slot import safe_slot

__all__ = ["OperationStatus"]

#: How often the elapsed clock and the estimate are refreshed. Twice a second is
#: enough for a clock that shows whole seconds, and is cheap enough to leave
#: running while a flash is saturating the bus.
_TICK_MS = 500

#: Below this many seconds of history there is not enough evidence to estimate
#: anything, and an estimate from four seconds of data is noise. The tilde says
#: estimate; this says "not yet".
_MIN_ESTIMATE_SECONDS = 3.0

#: A stall is not declared until the percentage has not moved for this long.
#: Generous on purpose: a device erasing a large partition can sit at the same
#: percentage for a minute while it is doing exactly what it should.
_STALL_SECONDS = 45.0


class OperationStatus(QWidget):
    """The status bar's live readout. Hidden until a job starts.

    Owned by the main window and driven by `BackendService`, but it has no
    dependency on either - it is a widget that is told things, which is what
    makes it testable without a backend and reusable in a panel.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("OperationStatus")

        self._name = ""
        self._progress = JobProgress()
        self._started_at: float | None = None
        self._finished = False
        #: (monotonic time, percentage) of the last time the percentage moved,
        #: used to notice a stall and to discard the stalled stretch from the
        #: rate calculation - a device that paused for a minute has not been
        #: writing at the average rate for that minute.
        self._last_move: tuple[float, float] | None = None
        self._stalled = False

        self._ring = CircularProgress(self, size=22, thickness=3)
        self._ring.setIndeterminate(True)

        self._name_label = QLabel("", self)
        self._name_label.setObjectName("OperationName")

        self._detail_label = QLabel("", self)
        self._detail_label.setObjectName("OperationDetail")

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(0)
        text.addWidget(self._name_label)
        text.addWidget(self._detail_label)
        self._text_column = text

        self._figures = QLabel("", self)
        self._figures.setObjectName("OperationFigures")
        self._figures.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 3, 10, 3)
        row.setSpacing(8)
        row.addWidget(self._ring, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addLayout(text, 1)
        row.addWidget(self._figures, 0)

        self._clock = QTimer(self)
        self._clock.setInterval(_TICK_MS)
        self._clock.timeout.connect(safe_slot(self._on_tick))

        self.setVisible(False)

    # -- api ---------------------------------------------------------------

    def begin(self, name: str, *, total_bytes: int = 0) -> None:
        """Starts a new operation. Any previous one is discarded, not merged."""
        self._name = name or "working"
        self._progress = JobProgress(operation_type=self._name, total_bytes=total_bytes)
        self._started_at = time.monotonic()
        self._last_move = (self._started_at, -1.0)
        self._finished = False
        self._stalled = False
        self._ring.setState("")
        self._ring.setValue(0)
        self._ring.setIndeterminate(True)
        self._name_label.setText(_humanise(name))
        self._detail_label.setText("starting…")
        self._figures.setText("")
        self._figures.setProperty("state", "")
        self.setToolTip("")
        self.setVisible(True)
        self._clock.start()

    def report(self, progress: JobProgress | None, *, message: str = "") -> None:
        """Applies a progress report. Safe to call before `begin()`."""
        if self._started_at is None:
            self.begin(progress.operation_type if progress else "working")

        if progress is not None:
            self._progress = progress
        if message:
            self._progress.status_message = message

        percentage = self._progress.percentage
        if self._progress.has_percentage:
            self._ring.setIndeterminate(False)
            self._ring.setValue(percentage)
            self._note_movement(percentage)
        else:
            self._ring.setIndeterminate(True)

        self._refresh()

    def finish(self, *, ok: bool = True, message: str = "") -> None:
        """Marks the operation done, leaving the result on screen.

        Deliberately not an immediate `reset()`: the outcome is the one thing the
        operator must see, and a status bar that clears itself the moment a flash
        ends is a status bar whose result nobody ever reads. The main window
        clears it on the next job or on a timer.
        """
        self._finished = True
        self._clock.stop()
        self._ring.setIndeterminate(False)
        self._ring.setValue(100.0 if ok else self._progress.percentage)
        self._ring.setState("ok" if ok else "error")
        self._figures.setProperty("state", "ok" if ok else "error")
        # The property has to be re-polished or the stylesheet keeps the old
        # colour - the same trap as every other property selector here.
        from huaxin.ui import style

        style.restyle(self._figures)

        if message:
            self._detail_label.setText(message)
        suffix = f"  ·  {format_duration(self.elapsed)}" if self._started_at else ""
        self._figures.setText(("done" if ok else "stopped") + suffix)

    def reset(self) -> None:
        """Hides the readout and forgets the operation."""
        self._clock.stop()
        self._started_at = None
        self._progress = JobProgress()
        self._finished = False
        self._stalled = False
        self._name = ""
        self._ring.setIndeterminate(True)
        self.setVisible(False)
        self.setToolTip("")

    # -- read-only state, for the window and for tests ----------------------

    @property
    def operation(self) -> str:
        return self._name

    @property
    def progress(self) -> JobProgress:
        return self._progress

    @property
    def elapsed(self) -> float:
        """Seconds since `begin()`, or 0 when nothing is running."""
        if self._started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self._started_at)

    @property
    def stalled(self) -> bool:
        """Whether the percentage has not moved for `_STALL_SECONDS`."""
        return self._stalled

    def estimate_remaining(self) -> float:
        """Seconds left, or -1 when it cannot be estimated.

        Derived from the percentage and the time it took to get there, which is
        crude and correct often enough to be worth showing - because the
        alternative is showing nothing, and "nothing" is what sends an operator
        to check whether the tool has hung.
        """
        if not self._progress.has_percentage:
            return -1.0
        if self._progress.has_eta:
            return self._progress.eta_seconds
        percentage = self._progress.percentage
        elapsed = self.elapsed
        if percentage <= 0.0 or elapsed < _MIN_ESTIMATE_SECONDS:
            return -1.0
        if percentage >= 100.0:
            return 0.0
        return elapsed * (100.0 - percentage) / percentage

    def is_estimated_eta(self) -> bool:
        """Whether the ETA on screen is this widget's own arithmetic."""
        return not self._progress.has_eta and self.estimate_remaining() >= 0.0

    # -- internals ---------------------------------------------------------

    def _note_movement(self, percentage: float) -> None:
        now = time.monotonic()
        if self._last_move is None or percentage > self._last_move[1]:
            self._last_move = (now, percentage)
            self._stalled = False
            return
        if now - self._last_move[0] >= _STALL_SECONDS and not self._finished:
            self._stalled = True

    def _on_tick(self) -> None:
        if not self._finished:
            self._note_movement(self._progress.percentage)
            self._refresh()

    def _refresh(self) -> None:
        """Rebuilds the two text fields from the current report.

        The figures are laid out right-aligned in a monospaced face, and the
        fields are fixed in order, so a number changing width does not make the
        whole line jump - which at two updates a second is genuinely unpleasant
        to look at.
        """
        progress = self._progress

        detail = progress.status_message
        if not detail and progress.total_partitions > 1 and progress.current_partition:
            position = progress.current_partition_index + 1
            detail = f"{progress.current_partition}  ·  partition {position} of {progress.total_partitions}"
        if self._stalled:
            detail = "no progress for a while — the device may have stopped responding"
        self._detail_label.setText(detail or "")

        figures: list[str] = []
        if progress.has_percentage:
            figures.append(f"{progress.percentage:.1f}%")
        if progress.has_bytes:
            figures.append(
                f"{format_bytes(progress.bytes_written)} / {format_bytes(progress.total_bytes)}"
            )
        if progress.speed_mbps > 0:
            figures.append(format_speed(progress.speed_mbps))
        if self._started_at is not None:
            figures.append(f"{format_duration(self.elapsed)} elapsed")
        remaining = self.estimate_remaining()
        if remaining >= 0:
            marker = "~" if self.is_estimated_eta() else ""
            figures.append(f"{marker}{format_duration(remaining)} left")
        self._figures.setText("  ·  ".join(figures))

        self._figures.setToolTip(
            f"{_humanise(self._name)}\n{progress.describe()}"
            + ("\n\nEstimated by the tool from the rate of progress so far."
               if self.is_estimated_eta() else "")
        )


def _humanise(name: str) -> str:
    """Turns a job name into something readable: "flash_partition" -> "Flash partition"."""
    text = (name or "").replace("_", " ").strip()
    if not text:
        return "Working"
    return text[0].upper() + text[1:]
