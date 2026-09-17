"""Animations, and the rules that keep them from being a nuisance.

Five rules, each of which exists because breaking it makes an interface worse
rather than merely different:

1. Short - 200 ms for a colour change, 300 ms for geometry. Past about 400 ms
   an interface stops feeling responsive and starts feeling slow, and no amount
   of easing fixes that.

2. Interruptible - Every helper here cancels whatever it was doing before it
   starts. A user who hovers a button five times in a second should see one
   animation that keeps up, not five queued ones that finish after they have
   moved on.

3. Never on the critical path - A flash's progress bar animating is
   decoration; the flash completing is the point. Animations are driven by the
   event loop, so a blocked loop freezes them - which is fine, and the reason
   nothing here is used to report state that matters. Text and colour change
   immediately; geometry and opacity catch up.

4. Respect the system preference - Every helper checks `animations_enabled()`
   and jumps straight to the end state when motion is off. This is not a nicety:
   for someone with a vestibular disorder, sliding and scaling panels are
   genuinely unpleasant, and Windows, macOS and GNOME all expose a switch for it
   that applications are expected to honour.

5. One easing vocabulary - Standard easing in, decelerate out, and an
   overshoot reserved for a single case (a value that has just been confirmed).
   A different curve for every widget is how a UI feels amateurish without
   anybody being able to point at why.

The `motion` object holds the durations in one place so a "make everything
snappier" pass is two numbers, not a search through twenty files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from PyQt6.QtCore import (
    QAbstractAnimation,
    QPoint,
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    QTimer,
    pyqtProperty,
)
from PyQt6.QtWidgets import QGraphicsOpacityEffect, QWidget

__all__ = [
    "motion",
    "animations_enabled",
    "set_animations_enabled",
    "fade",
    "fade_in",
    "fade_out",
    "animate_property",
    "collapse",
    "expand",
    "stagger",
    "HoverFeedback",
    "pulse",
    "animate_modal",
]


@dataclass(frozen=True)
class Motion:
    """Every duration and curve the interface uses.

    Named for the interaction rather than the number, so a call site reads as
    what it is doing: `fade(widget, motion.hover)` rather than
    `fade(widget, 180)`.
    """

    #: A colour or opacity change under the pointer. Below about 120 ms the eye
    #: reads it as an instant switch, which is the flicker this avoids.
    hover: int = 160
    #: A state change the user asked for: a tab, a panel opening.
    transition: int = 240
    #: A panel resizing. Longer, because the eye is tracking an edge.
    layout: int = 300
    #: A modal appearing.
    modal: int = 180
    #: Per-item delay when a list animates in. Small: ten items at 40 ms is
    #: 400 ms of total choreography, which is the most anyone will tolerate.
    stagger: int = 40
    #: The longest a single stagger sequence may run before later items are
    #: given no extra delay. Without this a fifty-row table takes two seconds.
    stagger_budget: int = 400

    #: Standard easing: starts quickly, settles gently. The default for anything
    #: leaving or appearing.
    ease_out: QEasingCurve.Type = QEasingCurve.Type.OutCubic
    #: For a control being pressed, where the motion should feel immediate.
    ease_in: QEasingCurve.Type = QEasingCurve.Type.InCubic
    #: Both ends eased: for geometry that grows and shrinks.
    ease_both: QEasingCurve.Type = QEasingCurve.Type.InOutCubic
    #: A slight overshoot, used only for a value that was just confirmed.
    ease_overshoot: QEasingCurve.Type = QEasingCurve.Type.OutBack


motion = Motion()

#: Set from the environment at import and toggled by the caller afterwards.
#: Qt exposes the platform preference inconsistently across versions, so the
#: settings dialog is the authority and this is the default.
_enabled: bool | None = None


def animations_enabled() -> bool:
    """Whether motion should be used.

    Defaults to off when `HUAXIN_REDUCE_MOTION` is set in the environment, which
    is also what the test suite sets - a test that waits for animations is a
    test that fails on a loaded machine.
    """
    global _enabled
    if _enabled is None:
        _enabled = os.environ.get("HUAXIN_REDUCE_MOTION", "").strip().lower() not in {
            "1", "true", "yes", "on",
        }
    return _enabled


def set_animations_enabled(enabled: bool) -> None:
    """Turns motion on or off for the rest of the session."""
    global _enabled
    _enabled = bool(enabled)


# -----------------------------------------------------------------------------
#  Opacity
# -----------------------------------------------------------------------------


def _opacity_effect(widget: QWidget) -> QGraphicsOpacityEffect:
    """Gets or creates the widget's opacity effect.

    Reusing the existing effect matters: replacing one detaches the animation
    that is already driving it, and the widget freezes at whatever opacity it
    had reached.
    """
    effect = widget.graphicsEffect()
    if not isinstance(effect, QGraphicsOpacityEffect):
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(1.0)
        widget.setGraphicsEffect(effect)
    return effect


def fade(
    widget: QWidget,
    duration: int,
    *,
    start: float,
    end: float,
    on_finished: Callable[[], None] | None = None,
) -> QPropertyAnimation | None:
    """Animates a widget's opacity. Returns the animation, or None if motion is off.

    The effect is left in place afterwards with opacity restored to 1, rather
    than removed: removing it makes Qt throw away the widget's paint cache and
    redraw it, which shows as a flicker on exactly the frame the animation
    finished.
    """
    if not animations_enabled() or duration <= 0:
        effect = _opacity_effect(widget)
        effect.setOpacity(end)
        if on_finished is not None:
            on_finished()
        return None

    effect = _opacity_effect(widget)
    animation = QPropertyAnimation(effect, b"opacity", widget)
    animation.setDuration(duration)
    animation.setStartValue(start)
    animation.setEndValue(end)
    animation.setEasingCurve(motion.ease_out)
    if on_finished is not None:
        animation.finished.connect(on_finished)
    animation.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    return animation


def fade_in(
    widget: QWidget,
    duration: int | None = None,
    *,
    visible: bool = True,
    on_finished: Callable[[], None] | None = None,
) -> QPropertyAnimation | None:
    """Shows a widget and fades it up."""
    if visible:
        widget.setVisible(True)
    return fade(
        widget,
        motion.transition if duration is None else duration,
        start=0.0,
        end=1.0,
        on_finished=on_finished,
    )


def fade_out(
    widget: QWidget,
    duration: int | None = None,
    *,
    hide: bool = True,
    on_finished: Callable[[], None] | None = None,
) -> QPropertyAnimation | None:
    """Fades a widget out, hiding it at the end when `hide` is set."""
    def done() -> None:
        if hide:
            widget.setVisible(False)
        # Restored so a later fade-in does not start from zero; the widget is
        # invisible either way, so nothing flickers.
        _opacity_effect(widget).setOpacity(1.0)
        if on_finished is not None:
            on_finished()

    return fade(
        widget,
        motion.transition if duration is None else duration,
        start=1.0,
        end=0.0,
        on_finished=done,
    )


# -----------------------------------------------------------------------------
#  Geometry
# -----------------------------------------------------------------------------


def animate_property(
    target: QObject,
    name: bytes,
    start: object,
    end: object,
    duration: int,
    *,
    curve: QEasingCurve.Type | None = None,
) -> QPropertyAnimation | None:
    """The general case: animates any Qt property that has a setter.

    Returns None when motion is off, having jumped straight to the end value -
    so a caller never has to check whether animations are enabled before using
    the result.

    The animation deletes itself when it stops, so a widget animated repeatedly
    does not accumulate them. That makes the returned object unsafe to keep:
    after it finishes, the wrapper refers to a deleted C++ object and touching it
    raises. Anything needing a persistent animation - a hover retargeted on every
    enter and leave - builds its own with KeepWhenStopped instead.
    """
    if not animations_enabled() or duration <= 0:
        target.setProperty(name.decode(), end)
        return None

    animation = QPropertyAnimation(target, name, target)
    animation.setDuration(duration)
    animation.setStartValue(start)
    animation.setEndValue(end)
    animation.setEasingCurve(curve or motion.ease_both)
    animation.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    return animation


def collapse(widget: QWidget, duration: int | None = None) -> QPropertyAnimation | None:
    """Shrinks a widget to nothing and hides it.

    The maximum height is animated rather than the height, and the widget is
    only hidden once the animation finishes: setting it invisible first makes Qt
    skip the paint, so the panel vanishes instantly and the animation runs
    against nothing.
    """
    span = motion.layout if duration is None else duration
    current = widget.maximumHeight()
    if current > 16_000:  # the unset default, meaning "as tall as it likes"
        current = widget.sizeHint().height()

    def finished() -> None:
        widget.setVisible(False)
        # Released so a later expand can measure the real content height rather
        # than inheriting the zero this left behind.
        widget.setMaximumHeight(16_777_215)

    animation = animate_property(widget, b"maximumHeight", current, 0, span,
                                 curve=motion.ease_both)
    if animation is None:
        widget.setVisible(False)
        widget.setMaximumHeight(16_777_215)
        return None
    animation.finished.connect(finished)
    return animation


def expand(
    widget: QWidget,
    duration: int | None = None,
    *,
    maximum: int | None = None,
) -> QPropertyAnimation | None:
    """Grows a hidden widget back to its natural height.

    The target height is measured from the layout rather than assumed, so a
    panel with a wrapping label still ends up the right size.
    """
    span = motion.layout if duration is None else duration
    widget.setVisible(True)
    widget.setMaximumHeight(0)
    target = maximum if maximum is not None else max(widget.sizeHint().height(), 1)

    animation = animate_property(widget, b"maximumHeight", 0, target, span,
                                 curve=motion.ease_both)
    if animation is None:
        widget.setMaximumHeight(16_777_215)
        return None

    def finished() -> None:
        # Back to unconstrained, so the panel can still be resized by the splitter
        # it is in after the animation has had its say.
        widget.setMaximumHeight(16_777_215)

    animation.finished.connect(finished)
    return animation


# -----------------------------------------------------------------------------
#  Feedback
# -----------------------------------------------------------------------------


class HoverFeedback(QObject):
    """Animates a widget's geometry on hover: a small lift, and back down.

    Qt's stylesheet handles colour on hover by itself. What it cannot do is move
    anything, and a control that shifts by a pixel or two under the pointer reads
    as more responsive than one that only changes shade. The movement is
    deliberately tiny - 2px on a button, at most - because a control that leaps
    under the cursor is worse than one that does not move at all.

    Installed as an event filter, so it does not subclass the widget and cannot
    break one that already has a class of its own.
    """

    def __init__(self, widget: QWidget, *, distance: int = 2, duration: int | None = None) -> None:
        super().__init__(widget)
        self._widget = widget
        self._distance = distance
        self._duration = motion.hover if duration is None else duration
        self._active: QPropertyAnimation | None = None
        # Where the widget sat before anything moved it, so the lift is always
        # measured from the layout position rather than from wherever the last
        # animation happened to stop.
        self._home: tuple[int, int] | None = None
        widget.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is not self._widget:
            return False
        if event.type() == QEvent.Type.Enter:
            self._move(up=True)
        elif event.type() in (QEvent.Type.Leave, QEvent.Type.Hide):
            self._move(up=False)
        # Never consumed: the widget still needs its own hover handling, and
        # returning True here would swallow the stylesheet's own hover state.
        return False

    def _move(self, *, up: bool) -> None:
        if not animations_enabled() or self._distance == 0:
            return

        position = self._widget.pos()
        if self._home is None:
            self._home = (position.x(), position.y())
        target_y = self._home[1] - (self._distance if up else 0)

        # One animation, created once and retargeted. `animate_property` deletes
        # its animation when it stops, so calling it per hover and keeping the
        # result means the second hover calls stop() on a deleted object - which
        # is exactly the crash this replaced. KeepWhenStopped keeps it alive for
        # the widget's lifetime, and reusing it avoids allocating on every mouse
        # move over a button.
        if self._active is None:
            animation = QPropertyAnimation(self._widget, b"pos", self._widget)
            animation.setDuration(self._duration)
            animation.setEasingCurve(motion.ease_out)
            self._active = animation

        self._active.stop()
        self._active.setStartValue(position)
        self._active.setEndValue(QPoint(self._home[0], target_y))
        self._active.start(QAbstractAnimation.DeletionPolicy.KeepWhenStopped)


def pulse(widget: QWidget, *, duration: int = 420, low: float = 0.45) -> QPropertyAnimation | None:
    """A brief dimming, for acknowledging a click that has no visible result yet.

    Used where an action starts something slow: without it the button looks like
    it did nothing, and the operator presses it again.
    """
    if not animations_enabled():
        return None
    effect = _opacity_effect(widget)
    animation = QPropertyAnimation(effect, b"opacity", widget)
    animation.setDuration(duration)
    animation.setKeyValueAt(0.0, 1.0)
    animation.setKeyValueAt(0.35, low)
    animation.setKeyValueAt(1.0, 1.0)
    animation.setEasingCurve(motion.ease_both)
    animation.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    return animation


# -----------------------------------------------------------------------------
#  Modals
# -----------------------------------------------------------------------------


def animate_modal(dialog: QWidget, *, show: bool = True) -> None:
    """Scales and fades a dialog in or out.

    The scale part is done by animating the window geometry from slightly
    smaller than the final size, because there is no transform on a top-level
    window. It is a two-line illusion and it is what separates a dialog that
    appears from one that has always been there.

    Called after the dialog has been laid out and positioned, so the final
    geometry is already correct.
    """
    if not animations_enabled():
        return

    geometry = dialog.geometry()
    if show:
        shrink = 0.96
        start = geometry.adjusted(
            int(geometry.width() * (1 - shrink) / 2),
            int(geometry.height() * (1 - shrink) / 2),
            -int(geometry.width() * (1 - shrink) / 2),
            -int(geometry.height() * (1 - shrink) / 2),
        )
        animate_property(dialog, b"geometry", start, geometry, motion.modal,
                         curve=motion.ease_out)
        fade(dialog, motion.modal, start=0.0, end=1.0)


def stagger(widgets: list[QWidget], *, delay: int | None = None) -> None:
    """Fades a list of widgets in one after another.

    The delay is capped by `motion.stagger_budget` so a long list finishes in a
    bounded time rather than taking longer the more rows it has. A list that
    animates for two seconds is a list nobody waits for.
    """
    if not animations_enabled() or not widgets:
        for widget in widgets:
            widget.setVisible(True)
        return

    step = motion.stagger if delay is None else delay
    for index, widget in enumerate(widgets):
        offset = min(index * step, motion.stagger_budget)
        widget.setVisible(False)
        # A timer rather than a queued animation: the point is a delay before the
        # widget is shown at all, and QPropertyAnimation has no "start later".
        QTimer.singleShot(offset, lambda w=widget: fade_in(w, motion.transition))
