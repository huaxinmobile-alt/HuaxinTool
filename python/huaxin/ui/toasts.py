"""Toast notifications: the transient messages a commercial tool shows.

WHY A TOAST AND NOT A MESSAGE BOX. A message box stops the world. It is the right
control for the two questions this tool asks before destroying something, and the
wrong one for everything else: "scan finished, 3 devices" does not deserve a
modal, and a modal that appears while a flash is running is a modal the operator
learns to dismiss without reading - which is exactly the habit that gets a
partition erased.

So anything that merely *reports* goes here: it appears in the corner, it can be
ignored, and it leaves on its own. Anything that *asks* still gets a dialog.

THE FOUR RULES THIS IMPLEMENTS

* It never takes focus. A toast is `Qt.WA_TransparentForMouseEvents` false but
  it never calls `activateWindow` or `setFocus`; the operator's caret stays where
  it was, which matters because a flash may be running.
* It never stacks without limit. Four is the cap, and a message that repeats
  under the same `key` updates the toast already on screen rather than adding a
  fifth - a device that disconnects and reconnects ten times while somebody
  jiggles the cable must not bury the interface in identical cards.
* It never disappears while it is being read. The countdown pauses under the
  pointer and restarts when the pointer leaves.
* It never carries information that exists nowhere else. Everything a toast says
  is also in the log, because a message that vanishes after four seconds is not a
  record of anything.
"""

from __future__ import annotations

from functools import partial
from typing import Callable

from PyQt6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QPropertyAnimation,
    QTimer,
    Qt,
    pyqtSignal,
)
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from huaxin.ui import animations, icons, tokens
from huaxin.ui.components import StyledButton
from huaxin.ui.safe_slot import safe_slot

__all__ = ["Toast", "ToastManager"]


#: level -> (icon name, tone, lifetime in ms).
#:
#: The lifetimes are not uniform, and that is the point: an error states
#: something the operator has to act on and gets twelve seconds, a success needs
#: two words and gets four. A warning sits in between because it is usually a
#: cable or a driver, which is worth a second look rather than an interruption.
_LEVELS: dict[str, tuple[str, str, int]] = {
    "info": ("info", "accent", 5000),
    "ok": ("success", "ok", 4000),
    "warn": ("warning", "warn", 9000),
    "error": ("error", "error", 12000),
}

_DEFAULT_LEVEL = "info"


class Toast(QFrame):
    """One notification card.

    Positioned by its manager rather than by a layout, because the stack has to
    slide as a group when one of them leaves - which a layout cannot do.
    """

    dismissed = pyqtSignal(object)  # the toast itself

    WIDTH = 372

    def __init__(
        self,
        parent: QWidget,
        *,
        level: str = "info",
        title: str = "",
        message: str = "",
        timeout_ms: int | None = None,
        action: tuple[str, Callable[[], None]] | None = None,
        closable: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFixedWidth(self.WIDTH)
        # A toast is not a window: it must never take the keyboard focus away
        # from whatever the operator was doing.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self._level = level if level in _LEVELS else _DEFAULT_LEVEL
        self._title_text = title
        self._count = 1
        self._lifetime = timeout_ms if timeout_ms is not None else _LEVELS[self._level][2]
        #: Set while the pointer is over the card, so the countdown does not run
        #: out under somebody who is reading it.
        self._paused = False
        self._remaining = self._lifetime
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(safe_slot(self.dismiss))

        icon_name, tone, _ = _LEVELS[self._level]
        self.setProperty("tone", tone)

        row = QHBoxLayout(self)
        row.setContentsMargins(
            tokens.METRICS.pad_sm + 2,
            tokens.METRICS.pad_sm,
            tokens.METRICS.gap_sm,
            tokens.METRICS.pad_sm,
        )
        row.setSpacing(tokens.METRICS.gap_sm)

        self._glyph = QLabel(self)
        self._glyph.setPixmap(
            icons.pixmap(icon_name, tokens.active_theme().colour(tone), tokens.METRICS.icon_size_lg)
        )
        self._glyph.setFixedWidth(tokens.METRICS.icon_size_lg + 2)
        row.addWidget(self._glyph, 0, Qt.AlignmentFlag.AlignTop)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)

        self._title = QLabel(title, self)
        self._title.setProperty("role", "heading")
        self._title.setWordWrap(True)
        self._title.setVisible(bool(title))
        column.addWidget(self._title)

        self._message = QLabel(message, self)
        self._message.setProperty("role", "caption")
        self._message.setWordWrap(True)
        self._message.setVisible(bool(message))
        self._message.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        column.addWidget(self._message)

        self._actions = QHBoxLayout()
        self._actions.setContentsMargins(0, tokens.METRICS.gap_xs, 0, 0)
        self._actions.setSpacing(tokens.METRICS.gap_xs)
        if action is not None:
            label, callback = action
            button = StyledButton(label, self, role="ghost", size="small", lift=0)
            button.clicked.connect(safe_slot(partial(self._run_action, callback)))
            self._actions.addWidget(button)
        self._actions.addStretch(1)
        column.addLayout(self._actions)
        row.addLayout(column, 1)

        if closable:
            close = StyledButton("", self, role="icon", icon="close", lift=0)
            close.setIconColourToken("text_muted")
            close.setFixedSize(tokens.METRICS.control_height_sm, tokens.METRICS.control_height_sm)
            close.setToolTip("Dismiss")
            close.setAccessibleName(f"Dismiss: {title or message}")
            close.clicked.connect(safe_slot(self.dismiss))
            row.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)

    # -- api ---------------------------------------------------------------

    def level(self) -> str:
        return self._level

    def title(self) -> str:
        return self._title_text

    def setMessage(self, title: str = "", message: str = "") -> None:
        """Rewrites the card in place, used when the same key fires again."""
        if title:
            self._title_text = title
            self._title.setText(title)
            self._title.setVisible(True)
        if message:
            self._message.setText(message)
            self._message.setVisible(True)
        self.adjustSize()

    def note_repeat(self) -> None:
        """Adds a "×3" marker, so a repeated message says how often it happened.

        Without it, deduplicating silently loses the count - and "the device
        disconnected" once, when it has happened six times, is different news.
        """
        self._count += 1
        base = self._title_text or self._message.text()
        self._title_text = f"{base}  ×{self._count}"
        self._title.setText(self._title_text)
        self._title.setVisible(True)
        # A repeat means the situation is still happening, so the card earns
        # another full lifetime rather than whatever was left of the last one.
        self.restart_timer()

    def restart_timer(self) -> None:
        self._remaining = self._lifetime
        if not self._paused:
            self._timer.start(self._remaining)

    def stop_timer(self) -> None:
        self._timer.stop()

    def start_timer(self) -> None:
        if self._lifetime > 0:
            self._timer.start(self._remaining)

    def dismiss(self) -> None:
        self._timer.stop()
        self.dismissed.emit(self)

    # -- behaviour ---------------------------------------------------------

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Pauses the countdown, remembering what was left of it."""
        if self._timer.isActive():
            self._paused = True
            self._remaining = max(600, self._timer.remainingTime())
            self._timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._paused:
            self._paused = False
            if self._lifetime > 0:
                self._timer.start(self._remaining)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Clicking the card dismisses it - except on a button, which handles its
        own press and must not have the card pulled out from under it."""
        if event.button() == Qt.MouseButton.LeftButton and not self._on_button(event):
            self.dismiss()
            return
        super().mousePressEvent(event)

    def _on_button(self, event) -> bool:
        """Whether the press landed on a button inside this card.

        Walks up from whatever was hit rather than testing the hit widget alone,
        because the press usually lands on the button's internal label.
        """
        widget = self.childAt(event.position().toPoint())
        while widget is not None and widget is not self:
            if isinstance(widget, StyledButton):
                return True
            widget = widget.parentWidget()
        return False

    def _run_action(self, callback: Callable[[], None]) -> None:
        # Dismissed first: an action usually opens a dialog, and leaving the card
        # underneath it means dismissing two things to get back to work.
        self.dismiss()
        callback()


class ToastManager(QObject):
    """Owns the toast stack in one corner of a window.

    The manager is the only thing that positions toasts, so the slide-as-a-group
    behaviour is in one place. It filters the window's resize event rather than
    requiring the window to call it, so a host only has to construct it.
    """

    #: More than this on screen at once and they stop being notifications and
    #: start being a wall. The fifth pushes the oldest out immediately.
    MAX_VISIBLE = 4
    MARGIN = 16
    SPACING = 8

    def __init__(self, window: QWidget, *, bottom_offset: int = 0) -> None:
        super().__init__(window)
        self._window = window
        self._live: list[Toast] = []
        self._by_key: dict[str, Toast] = {}
        #: How much room to leave under the stack. The main window sets this to
        #: the status bar's height so the cards do not cover the state chip.
        self.bottom_offset = bottom_offset
        self._window.installEventFilter(self)

    # -- api ---------------------------------------------------------------

    @property
    def live(self) -> list[Toast]:
        return list(self._live)

    def show(
        self,
        level: str = "info",
        title: str = "",
        message: str = "",
        *,
        timeout_ms: int | None = None,
        action: tuple[str, Callable[[], None]] | None = None,
        key: str | None = None,
    ) -> Toast:
        """Shows a toast, or refreshes the one already showing under `key`.

        `key` is what makes a repeating message behave: the same failure three
        times is one card that says it happened three times, not three cards.
        """
        existing = self._by_key.get(key) if key else None
        if existing is not None and existing in self._live:
            existing.setMessage(title, message)
            existing.note_repeat()
            return existing

        toast = Toast(
            self._window,
            level=level,
            title=title,
            message=message,
            timeout_ms=timeout_ms,
            action=action,
        )
        toast.dismissed.connect(safe_slot(self._on_dismissed))
        self._live.append(toast)
        if key:
            self._by_key[key] = toast

        if len(self._live) > self.MAX_VISIBLE:
            # The oldest goes, not the newest: the card that just arrived is the
            # one describing what is happening now.
            self._live[0].dismiss()

        toast.adjustSize()
        toast.show()
        toast.raise_()

        target = self._position_for(toast)
        if animations.animations_enabled():
            # Slides in from beyond the right edge. Deliberately a geometry
            # animation on `pos` rather than an opacity trick: the card entering
            # from the side reads as "this just arrived", which a fade does not.
            start = QPoint(self._window.width(), target.y())
            toast.move(start)
            animation = QPropertyAnimation(toast, b"pos", toast)
            animation.setDuration(animations.motion.transition)
            animation.setEasingCurve(animations.motion.ease_out)
            animation.setStartValue(start)
            animation.setEndValue(target)
            animation.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
            animations.fade_in(toast, animations.motion.transition)
        else:
            toast.move(target)

        # Layers already on screen move up to make room. The new card is reserved
        # rather than laid out, because it is mid-slide and moving it again would
        # fight the animation it is already running.
        self._relayout(reserve=toast)
        toast.start_timer()
        return toast

    def clear(self) -> None:
        for toast in list(self._live):
            toast.dismiss()

    # -- internals ---------------------------------------------------------

    def _on_dismissed(self, toast: Toast) -> None:
        if toast not in self._live:
            return
        self._live.remove(toast)
        for key, value in list(self._by_key.items()):
            if value is toast:
                del self._by_key[key]

        if animations.animations_enabled():
            # Sliding out while fading, and both end before the card is deleted.
            # `fade_out` is deliberately not used: its completion callback restores
            # the opacity to 1 so the widget can be reused, which on a card that is
            # still on screen for another 80 ms shows as a flash.
            animations.fade(toast, animations.motion.hover, start=1.0, end=0.0)
            animation = QPropertyAnimation(toast, b"pos", toast)
            animation.setDuration(animations.motion.transition)
            animation.setEasingCurve(animations.motion.ease_out)
            animation.setStartValue(toast.pos())
            animation.setEndValue(QPoint(self._window.width() + 8, toast.pos().y()))
            animation.finished.connect(safe_slot(toast.deleteLater))
            animation.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        else:
            toast.deleteLater()
        self._relayout()

    def _stack_height(self) -> int:
        return sum(toast.sizeHint().height() for toast in self._live) + max(
            0, len(self._live) - 1
        ) * self.SPACING

    def _position_for(self, toast: Toast) -> QPoint:
        """Where `toast` belongs if it is the bottom card of the stack."""
        x = self._window.width() - toast.width() - self.MARGIN
        y = self._window.height() - self.bottom_offset - self.MARGIN - toast.sizeHint().height()
        return QPoint(max(self.MARGIN, x), max(self.MARGIN, y))

    def _relayout(self, *, reserve: Toast | None = None, animate: bool = True) -> None:
        """Restacks every live card from the bottom up.

        Bottom-up because the newest card is the one nearest the corner and the
        ones above it are older - the card that just arrived should be where the
        eye already is, which is the corner of the screen it animates towards.

        `reserve` names a card that is not to be moved but whose space must be
        left alone, which is what a card that is still sliding in needs: its
        place is taken, and it is in the middle of an animation that owns its
        position.
        """
        y = self._window.height() - self.bottom_offset - self.MARGIN
        if reserve is not None and reserve in self._live:
            y -= reserve.sizeHint().height() + self.SPACING

        for toast in reversed(self._live):
            if toast is reserve:
                continue
            height = toast.sizeHint().height()
            y -= height
            target = QPoint(
                max(self.MARGIN, self._window.width() - toast.width() - self.MARGIN),
                max(self.MARGIN, y),
            )
            self._move_to(toast, target, animate=animate)
            y -= self.SPACING

    def _move_to(self, toast: Toast, target: QPoint, *, animate: bool) -> None:
        if not animate or not animations.animations_enabled() or toast.pos() == target:
            toast.move(target)
            return
        animation = QPropertyAnimation(toast, b"pos", toast)
        animation.setDuration(animations.motion.transition)
        animation.setEasingCurve(animations.motion.ease_out)
        animation.setStartValue(toast.pos())
        animation.setEndValue(target)
        animation.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt naming
        """Re-anchors the stack when the window changes size.

        THE `__dict__.get` IS NOT DEFENSIVE PADDING. When this manager's Python
        object is freed while its C++ object survives - which happens whenever a
        host widget outlives it, and at application exit - Qt still calls this
        filter, and PyQt builds a *fresh wrapper* around the existing C++ object
        without running `__init__`. Every attribute is then missing, and raising
        from an event filter is not a Python error: PyQt routes it to qFatal, and
        the process aborts with no traceback. That is a crash on exit, in a tool
        that may be halfway through writing a partition.

        So a filter that cannot find its state does nothing, which is correct:
        there is no stack to reposition.
        """
        state = self.__dict__
        window = state.get("_window")
        if window is None:
            return False
        if watched is window and event.type() == QEvent.Type.Resize and state.get("_live"):
            self._relayout(animate=False)
        return super().eventFilter(watched, event)
