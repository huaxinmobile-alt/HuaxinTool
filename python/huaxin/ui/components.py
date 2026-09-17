"""The widget library: everything the interface is built from.

Each class here exists because the same shape is needed in more than one place,
and each one is styled by a `role` property that the stylesheet understands
rather than by inline styles. That indirection is the point: a button does not
know what colour it is, so changing the palette changes every button, and
switching to the light theme cannot leave one control behind.

WHAT IS DELIBERATELY NOT HERE

* Anything that knows about a device. This module is the design system; a panel
  that flashes firmware belongs in panels.py, and mixing the two is how a theme
  change ends up requiring a protocol change.
* Layout. The components are leaves; how they are arranged is the caller's
  business, so a card can hold a form here and a device list there without
  either knowing about the other.

THE ONE AWKWARDNESS WORTH KNOWING. Qt evaluates a property selector when a widget
is polished and will not re-evaluate it when the property changes. Every setter
below that affects appearance therefore calls `style.restyle()`, which unpolishes
and repolishes the widget. Skipping that is why a widget sometimes keeps its old
look - the property is right, Qt just never looked again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from PyQt6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QCheckBox,
    QMenu,
    QGraphicsOpacityEffect,
    QComboBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTabBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from huaxin.ui import animations, icons, style, tokens

__all__ = [
    "ButtonRole",
    "StyledButton",
    "IconButton",
    "ButtonRow",
    "StatusDot",
    "Chip",
    "SectionHeader",
    "Card",
    "Callout",
    "StatTile",
    "StyledProgressBar",
    "SegmentedProgress",
    "CircularProgress",
    "PulseRing",
    "StyledTable",
    "StyledTabBar",
    "StyledTabWidget",
    "SearchField",
    "StyledComboBox",
    "StyledCheckBox",
    "EmptyState",
    "Divider",
    "apply_shadow",
    "button_row",
]

#: The button roles the stylesheet defines. A plain string enum would let a typo
#: through to Qt, which drops the rule silently and leaves a primary button
#: looking like an ordinary one - so these are the only values the class accepts.
ButtonRole = str  # one of: "primary", "secondary", "danger", "ghost", "icon"

_ROLES = frozenset({"primary", "secondary", "danger", "ghost", "icon"})


# -----------------------------------------------------------------------------
#  Effects
# -----------------------------------------------------------------------------


def apply_shadow(
    widget: QWidget,
    *,
    blur: int = 24,
    y_offset: int = 4,
    alpha: float = 0.45,
) -> QGraphicsDropShadowEffect:
    """Gives a widget a drop shadow.

    Qt stylesheets have no box-shadow, so elevation has to be an effect object.
    The colour is a black at partial alpha rather than a themed colour: a shadow
    tinted with the surface colour looks like a halo, and on the light theme it
    has to be much lighter than on the dark one, which is what `alpha` is for.

    A shadow is applied sparingly - cards, dialogs, and a hovered primary button.
    One on everything is the fastest way to make an interface look muddy.
    """
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setXOffset(0)
    effect.setYOffset(y_offset)
    # Black at a fraction, so it darkens whatever is behind it on either theme.
    effect.setColor(QColor(0, 0, 0, int(255 * alpha)))
    widget.setGraphicsEffect(effect)
    return effect


# -----------------------------------------------------------------------------
#  Buttons
# -----------------------------------------------------------------------------


class StyledButton(QPushButton):
    """A button with a role, an optional icon, and hover feedback.

    The role decides the appearance and is set as a Qt property so the
    stylesheet does the drawing - see the `QPushButton[role=...]` rules. Nothing
    in this class sets a colour.
    """

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        role: ButtonRole = "secondary",
        icon: str | None = None,
        icon_colour_token: str | None = None,
        tooltip: str = "",
        size: str = "normal",
        lift: int = 1,
        checkable: bool = False,
    ) -> None:
        super().__init__(text, parent)
        self._role = "secondary"
        self._icon_name = icon
        self._icon_colour_token = icon_colour_token
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setRole(role)
        if size != "normal":
            self.setProperty("size", size)
        if tooltip:
            self.setToolTip(tooltip)
        if icon:
            self._refresh_icon()
        if checkable:
            self.setCheckable(True)
        # A single pixel of lift: enough to feel alive under the pointer, small
        # enough that a row of buttons does not visibly wobble.
        if lift:
            self._hover = animations.HoverFeedback(self, distance=lift)

    # -- api ---------------------------------------------------------------

    def setRole(self, role: ButtonRole) -> None:
        if role not in _ROLES:
            raise ValueError(f"unknown button role {role!r}; expected one of {sorted(_ROLES)}")
        self._role = role
        self.setProperty("role", role)
        style.restyle(self)

    def role(self) -> str:
        return self._role

    def setIcon(self, icon: str | None) -> None:  # type: ignore[override]
        """Takes an icon *name* from the icon set, not a QIcon.

        Overriding the Qt method with a different meaning is deliberate - every
        call site in this application passes a name, and two methods that look
        the same but take different types is worse than one that is renamed.
        Use `setQtIcon` for the inherited behaviour.
        """
        self._icon_name = icon
        self._refresh_icon()

    def setQtIcon(self, value) -> None:
        super().setIcon(value)

    def setIconColourToken(self, token: str | None) -> None:
        """Colours the icon from a theme token instead of the text colour.

        A danger button with a red border and a grey icon looks unfinished, and
        the token is what keeps the icon in step with the theme.
        """
        self._icon_colour_token = token
        self._refresh_icon()

    def _refresh_icon(self) -> None:
        if not self._icon_name:
            return
        colour = None
        if self._icon_colour_token:
            colour = tokens.active_theme().colour(self._icon_colour_token)
        size = tokens.METRICS.icon_size_lg if self.property("size") == "large" else tokens.METRICS.icon_size
        super().setIcon(icons.icon(self._icon_name, colour, size))

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        super().setText(text)
        self.setAccessibleName(text)


def IconButton(
    name: str,
    parent: QWidget | None = None,
    *,
    tooltip: str = "",
    size: int = 32,
    role: ButtonRole = "icon",
    checkable: bool = False,
) -> StyledButton:
    """A square button carrying only an icon, with a tooltip.

    A function rather than a class: it is a StyledButton with two properties
    set, and a subclass would add a type to learn for no behaviour of its own.
    The tooltip is not optional in practice - an icon alone is ambiguous, which
    is why the signature asks for one.
    """
    button = StyledButton("", parent, role=role, icon=name, tooltip=tooltip, lift=0)
    button.setFixedSize(QSize(size, size))
    button.setCheckable(checkable)
    return button


def button_row(*buttons: QWidget, spacing: int | None = None, stretch: bool = False) -> QWidget:
    """A horizontal row of buttons, right-aligned by default.

    Returns a widget rather than a layout because callers want to add one thing
    to their own layout, and a bare layout cannot be hidden or disabled as a
    unit - which a row of action buttons sometimes needs to be.
    """
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(tokens.METRICS.gap_sm if spacing is None else spacing)
    if not stretch:
        row.addStretch(1)
    for button in buttons:
        row.addWidget(button)
    return holder


class ButtonRow(QWidget):
    """A row of buttons that manages its own enabled state.

    Wraps the common pattern of "enable these while something is selected and
    those while something is running", which otherwise turns into a dozen
    setEnabled calls scattered through a panel.
    """

    def __init__(self, parent: QWidget | None = None, *, spacing: int | None = None) -> None:
        super().__init__(parent)
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(tokens.METRICS.gap_sm if spacing is None else spacing)
        self._buttons: list[StyledButton] = []
        self._needs_selection: list[StyledButton] = []
        self._needs_idle: list[StyledButton] = []

    def add(
        self,
        button: StyledButton,
        *,
        needs_selection: bool = False,
        needs_idle: bool = False,
        stretch_after: bool = False,
    ) -> "ButtonRow":
        """Adds a button and returns the row, so calls chain.

        Returning the button instead would be the more obvious signature and the
        wrong one: `ButtonRow().add(a).add(b)` would then call `add` on a button,
        and the row itself - having no parent and no reference - would be
        collected by Python while the first button was still being added to it.
        Returning the row makes the chained form mean what it looks like.
        """
        self._row.addWidget(button)
        self._buttons.append(button)
        if needs_selection:
            self._needs_selection.append(button)
        if needs_idle:
            self._needs_idle.append(button)
        if stretch_after:
            self._row.addStretch(1)
        return self

    def finish(self) -> "ButtonRow":
        self._row.addStretch(1)
        return self

    def refresh(self, *, has_selection: bool, busy: bool) -> None:
        """Applies the enablement rules to every button at once."""
        for button in self._needs_selection:
            button.setEnabled(has_selection and not busy)
        for button in self._needs_idle:
            button.setEnabled(not busy)

    def buttons(self) -> list[StyledButton]:
        return list(self._buttons)


# -----------------------------------------------------------------------------
#  Status
# -----------------------------------------------------------------------------


class StatusDot(QWidget):
    """A small filled circle, optionally pulsing while a job is running.

    Drawn rather than shown as an image so it stays crisp at any size and takes
    its colour from a token. The pulse is a slow opacity cycle rather than a
    position animation, because a moving dot in the corner of the eye is
    distracting where a breathing one is not.
    """

    def __init__(self, parent: QWidget | None = None, *, size: int = 9, tone: str = "ok") -> None:
        super().__init__(parent)
        self._size = size
        self._tone = tone
        self._pulsing = False
        self._pulse: QPropertyAnimation | None = None
        self.setFixedSize(QSize(size + 4, size + 4))
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def setTone(self, tone: str) -> None:
        """One of ok, warn, error, muted, accent."""
        self._tone = tone
        self.update()

    def tone(self) -> str:
        return self._tone

    def setPulsing(self, pulsing: bool) -> None:
        """Breathes while a long operation runs, to show the tool is alive."""
        if pulsing == self._pulsing:
            return
        self._pulsing = pulsing
        if not pulsing or not animations.animations_enabled():
            # Stopped, not merely ignored: a looping animation left running is
            # work the CPU does forever for a widget that is no longer pulsing.
            if self._pulse is not None:
                self._pulse.stop()
            effect = self.graphicsEffect()
            if isinstance(effect, QGraphicsOpacityEffect):
                effect.setOpacity(1.0)
            self.update()
            return

        effect = self.graphicsEffect()
        if not isinstance(effect, QGraphicsOpacityEffect):
            effect = QGraphicsOpacityEffect(self)
            effect.setOpacity(1.0)
            self.setGraphicsEffect(effect)

        # Created once and reused, so calling setPulsing(True) twice does not
        # stack two animations fighting over the same opacity.
        if self._pulse is None:
            animation = QPropertyAnimation(effect, b"opacity", self)
            animation.setDuration(1400)
            animation.setStartValue(1.0)
            animation.setKeyValueAt(0.5, 0.35)
            animation.setEndValue(1.0)
            animation.setLoopCount(-1)
            animation.setEasingCurve(QEasingCurve.Type.InOutSine)
            self._pulse = animation
        self._pulse.start()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        palette = {
            "ok": tokens.active_theme().ok,
            "warn": tokens.active_theme().warn,
            "error": tokens.active_theme().error,
            "accent": tokens.active_theme().accent,
            "muted": tokens.active_theme().text_disabled,
        }
        colour = QColor(palette.get(self._tone, tokens.active_theme().text_muted))

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # A soft ring around the dot, so a status light on a coloured row still
        # reads as a light rather than as part of the row.
        halo = QColor(colour)
        halo.setAlpha(60)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(0, 0, self._size + 4, self._size + 4)
        painter.setBrush(colour)
        painter.drawEllipse(2, 2, self._size, self._size)
        painter.end()


class Chip(QLabel):
    """A small rounded label: a status, a count, a tag.

    Sized to its text and vertically centred, which is what makes a row of them
    read as a row of tags rather than as a paragraph.
    """

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        tone: str = "neutral",
        icon: str | None = None,
    ) -> None:
        super().__init__(text, parent)
        self.setProperty("role", "chip")
        self.setProperty("tone", tone)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        if icon:
            self.setIcon(icon, tone)

    def setTone(self, tone: str) -> None:
        self.setProperty("tone", tone)
        style.restyle(self)

    def setIcon(self, name: str, tone: str = "neutral") -> None:  # noqa: N802
        """Puts an icon before the text. The colour follows the chip's tone."""
        token = {"ok": "ok", "warn": "warn", "error": "error", "accent": "accent"}.get(tone)
        colour = tokens.active_theme().colour(token) if token else tokens.active_theme().text_muted
        pixmap = icons.pixmap(name, colour, tokens.METRICS.font_size_micro + 3)
        self.setPixmap(pixmap)


@dataclass
class Stat:
    """One figure in a stat tile: a value and what it counts."""

    value: str
    label: str
    tone: str = "neutral"


class StatTile(QFrame):
    """A big number over a caption, for the counts a panel wants to shout.

    The value is set in the heading size and the label in the micro size, which
    is the whole trick: a number nobody can read at a glance is a number nobody
    uses.
    """

    def __init__(self, stat: Stat, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "inset")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            tokens.METRICS.pad_sm, tokens.METRICS.gap_sm, tokens.METRICS.pad_sm, tokens.METRICS.gap_sm
        )
        layout.setSpacing(0)

        self._value = QLabel(stat.value, self)
        self._value.setProperty("role", "title")
        self._value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label = QLabel(stat.label, self)
        self._label.setProperty("role", "micro")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(self._value)
        layout.addWidget(self._label)
        self.setTone(stat.tone)

    def setValue(self, value: str) -> None:
        self._value.setText(value)

    def setTone(self, tone: str) -> None:
        """Tints the figure. ok and error are the only two that carry meaning."""
        token = {"ok": "ok", "warn": "warn", "error": "error", "accent": "accent"}.get(tone)
        colour = tokens.active_theme().colour(token) if token else tokens.active_theme().text
        self._value.setStyleSheet(f"color: {colour};")


# -----------------------------------------------------------------------------
#  Structure
# -----------------------------------------------------------------------------


class SectionHeader(QWidget):
    """A heading with an optional caption, icon and trailing action.

    A widget rather than a bare label so the trailing widget (usually an icon
    button) stays vertically centred when the caption wraps to two lines, which
    a hand-built row does not do reliably.
    """

    def __init__(
        self,
        title: str,
        parent: QWidget | None = None,
        *,
        caption: str = "",
        icon: str | None = None,
        accent: bool = False,
    ) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(tokens.METRICS.gap_sm)

        if icon:
            glyph = QLabel(self)
            glyph.setPixmap(
                icons.pixmap(
                    icon,
                    tokens.active_theme().accent if accent else tokens.active_theme().text_muted,
                    tokens.METRICS.icon_size_lg,
                )
            )
            glyph.setFixedWidth(tokens.METRICS.icon_size_lg + 2)
            row.addWidget(glyph, 0, Qt.AlignmentFlag.AlignVCenter)

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(1)
        self._title = QLabel(title, self)
        self._title.setProperty("role", "heading")
        text_column.addWidget(self._title)
        if caption:
            self._caption = QLabel(caption, self)
            self._caption.setProperty("role", "caption")
            self._caption.setWordWrap(True)
            text_column.addWidget(self._caption)
        row.addLayout(text_column, 1)

        self.actions = QHBoxLayout()
        self.actions.setContentsMargins(0, 0, 0, 0)
        self.actions.setSpacing(tokens.METRICS.gap_xs)
        row.addLayout(self.actions)

    def add_action(self, widget: QWidget) -> "SectionHeader":
        """Adds a trailing widget and returns the header, so calls chain.

        Returning the widget instead - the obvious signature - has a failure mode
        that costs an afternoon: `SectionHeader(...).add_action(button)` would
        evaluate to the button, and if the caller then passed that result
        anywhere, the header itself would have no reference, be collected by
        Python, and take the button down with it. The symptom is
        "wrapped C/C++ object has been deleted" pointing at a widget that was
        only ever created. Returning self removes the possibility.
        """
        self.actions.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
        return self


class Card(QFrame):
    """A raised surface with an optional titled header and an optional shadow.

    This is the container everything else goes in, and the reason panels look
    like panels rather than like a flat sheet of labels with lines between them.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str = "",
        caption: str = "",
        icon: str | None = None,
        shadow: bool = False,
        padding: int | None = None,
        spacing: int | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("role", "card")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.header: SectionHeader | None = None
        if title or icon:
            header_frame = QFrame(self)
            header_frame.setProperty("role", "card-header")
            header_layout = QHBoxLayout(header_frame)
            header_layout.setContentsMargins(
                tokens.METRICS.pad, tokens.METRICS.gap_sm, tokens.METRICS.pad, tokens.METRICS.gap_sm
            )
            self.header = SectionHeader(title, header_frame, caption=caption, icon=icon)
            header_layout.addWidget(self.header)
            outer.addWidget(header_frame)

        self.body = QWidget(self)
        self.body_layout = QVBoxLayout(self.body)
        pad = tokens.METRICS.pad if padding is None else padding
        self.body_layout.setContentsMargins(pad, pad, pad, pad)
        self.body_layout.setSpacing(tokens.METRICS.gap if spacing is None else spacing)
        outer.addWidget(self.body, 1)

        if shadow:
            apply_shadow(self, blur=26, y_offset=5, alpha=0.35)

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self.body_layout.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout) -> None:
        self.body_layout.addLayout(layout)


class Callout(QFrame):
    """A note that has to be read, with an accent bar down its left edge.

    Used for the two things a flashing tool must say out loud: what an action
    will destroy, and what went wrong. The severity changes the bar and the
    tint; the icon changes with it, because colour alone is not a signal for
    anyone who cannot see it.
    """

    _ICONS = {"info": "info", "warn": "warning", "error": "error", "ok": "success"}

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        severity: str = "info",
        title: str = "",
    ) -> None:
        super().__init__(parent)
        self.setProperty("role", "callout")
        self.setProperty("severity", severity)

        row = QHBoxLayout(self)
        row.setContentsMargins(tokens.METRICS.gap, tokens.METRICS.gap_sm, tokens.METRICS.gap, tokens.METRICS.gap_sm)
        row.setSpacing(tokens.METRICS.gap_sm)

        self._glyph = QLabel(self)
        row.addWidget(self._glyph, 0, Qt.AlignmentFlag.AlignTop)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        self._title = QLabel(title, self)
        self._title.setProperty("role", "heading")
        self._title.setVisible(bool(title))
        column.addWidget(self._title)
        self._text = QLabel(text, self)
        self._text.setWordWrap(True)
        column.addWidget(self._text)
        row.addLayout(column, 1)

        self.setSeverity(severity)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        self._text.setText(text)

    def setSeverity(self, severity: str) -> None:
        self.setProperty("severity", severity)
        tone = {"ok": "ok", "warn": "warn", "error": "error"}.get(severity, "accent")
        self._glyph.setPixmap(
            icons.pixmap(self._ICONS.get(severity, "info"),
                         tokens.active_theme().colour(tone), tokens.METRICS.icon_size_lg)
        )
        style.restyle(self)


class Divider(QFrame):
    """A one-pixel rule. A widget rather than a stylesheet border so it can be
    added to a layout and spaced like anything else."""

    def __init__(self, parent: QWidget | None = None, *, vertical: bool = False) -> None:
        super().__init__(parent)
        self.setProperty("role", "separator")
        if vertical:
            self.setFixedWidth(1)
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        else:
            self.setFixedHeight(1)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


class EmptyState(QWidget):
    """What a panel shows when it has nothing to show.

    A blank area reads as a bug. This says what would put something there, which
    is the difference between "the tool is broken" and "you have not scanned
    yet".
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        icon: str = "usb",
        title: str = "",
        message: str = "",
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(tokens.METRICS.gap_lg, tokens.METRICS.gap_lg,
                                  tokens.METRICS.gap_lg, tokens.METRICS.gap_lg)
        layout.setSpacing(tokens.METRICS.gap_sm)
        layout.addStretch(1)

        self._glyph = QLabel(self)
        self._glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._glyph)

        self._title = QLabel(title, self)
        self._title.setProperty("role", "heading")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._title)

        self._message = QLabel(message, self)
        self._message.setProperty("role", "caption")
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setWordWrap(True)
        layout.addWidget(self._message)
        layout.addStretch(1)

        self.setIcon(icon)

    def setIcon(self, name: str) -> None:
        self._glyph.setPixmap(
            icons.pixmap(name, tokens.active_theme().text_disabled, 34)
        )

    def setMessages(self, title: str, message: str) -> None:
        self._title.setText(title)
        self._message.setText(message)


# -----------------------------------------------------------------------------
#  Progress
# -----------------------------------------------------------------------------


class StyledProgressBar(QProgressBar):
    """A progress bar that animates to its new value and can show a state.

    Qt's bar jumps straight to the value it is given, which for a flash that
    reports in coarse steps looks like the tool is stuttering. Animating the
    value makes the same information read as smooth, and costs one animation.

    Values are also *smoothed* rather than followed exactly: a device reporting
    37%, 38%, 37% makes a bar that twitches backwards, so a value below the
    current one is only honoured if the drop is large enough to be a real
    restart rather than jitter.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        text_visible: bool = True,
        show_eta: bool = False,
    ) -> None:
        super().__init__(parent)
        self._animation: QPropertyAnimation | None = None
        self._last = 0
        self._show_eta = show_eta
        self.setTextVisible(text_visible)
        self.setRange(0, 100)
        self.setValue(0)

    def setValue(self, value: int) -> None:  # noqa: N802 - Qt naming
        """Sets the value, animating towards it.

        A drop of more than 5 points is treated as a new operation and taken
        directly; anything smaller is jitter and is ignored, because a bar that
        ticks backwards reads as a fault.
        """
        target = max(0, min(self.maximum(), int(value)))
        if target < self._last and (self._last - target) <= 5:
            return
        self._last = target

        if not animations.animations_enabled():
            super().setValue(target)
            return

        start = super().value()
        if start == target:
            return
        if self._animation is not None:
            self._animation.stop()
        animation = QPropertyAnimation(self, b"value", self)
        animation.setDuration(min(220, 60 + abs(target - start) * 3))
        animation.setStartValue(start)
        animation.setEndValue(target)
        animation.setEasingCurve(animations.motion.ease_out)
        self._animation = animation
        animation.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def setState(self, state: str) -> None:
        """ok, warn, error or "" for the accent default."""
        self.setProperty("state", state)
        style.restyle(self)

    def setIndeterminate(self, indeterminate: bool) -> None:
        """Range 0..0 makes Qt draw its own busy animation.

        The honest display for an operation with no known duration - an erase,
        which the chip decides the length of. A bar at 0% would be a lie.
        """
        if indeterminate:
            self.setRange(0, 0)
            self.setTextVisible(False)
        else:
            self.setRange(0, 100)
            self.setTextVisible(True)
            self.setValue(self._last)


class SegmentedProgress(QWidget):
    """One bar per partition, for a multi-partition flash.

    A single bar for a whole package hides the thing an operator actually wants
    to know: which partition it is on, and whether the slow one is the last one
    or a stuck one in the middle. Each segment is drawn from the same tokens as
    everything else, and the labels above them come from the caller.

    The segments are proportional to the sizes given, so a 4 GB super partition
    takes up most of the width and a 64 KB misc does not - which is the whole
    point of showing them rather than a list.
    """

    segment_clicked = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None, *, height: int = 26) -> None:
        super().__init__(parent)
        self._segments: list[tuple[str, int, float]] = []  # name, weight, progress 0..1
        self._active = -1
        self.setMinimumHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self._hover = -1

    def setSegments(self, segments: Sequence[tuple[str, int]]) -> None:
        """Sets the segments as (name, weight) pairs."""
        self._segments = [(name, max(1, int(weight)), 0.0) for name, weight in segments]
        self._active = -1
        self.update()

    def setSegmentProgress(self, index: int, fraction: float) -> None:
        if not 0 <= index < len(self._segments):
            return
        name, weight, previous = self._segments[index]
        self._segments[index] = (name, weight, max(0.0, min(1.0, fraction)))
        self._active = index
        if abs(previous - fraction) > 0.001:
            self.update()

    def setActive(self, index: int) -> None:
        self._active = index
        self.update()

    def overall(self) -> float:
        """Weighted completion across every segment, 0..1."""
        total = sum(weight for _, weight, _ in self._segments)
        if total == 0:
            return 0.0
        done = sum(weight * progress for _, weight, progress in self._segments)
        return done / total

    def hoveredSegment(self) -> int:
        return self._hover

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        index = self._segment_at(event.position().x())
        if index != self._hover:
            self._hover = index
            self.setToolTip(self._segments[index][0] if index >= 0 else "")
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._hover = -1
        self.setToolTip("")
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        index = self._segment_at(event.position().x())
        if index >= 0:
            self.segment_clicked.emit(index)

    def _segment_at(self, x: float) -> int:
        """Which segment a horizontal position falls in, or -1."""
        total = sum(weight for _, weight, _ in self._segments)
        if total == 0:
            return -1
        usable = max(1.0, self.width() - (len(self._segments) - 1) * 2)
        cursor = 0.0
        for index, (_, weight, _) in enumerate(self._segments):
            span = usable * (weight / total)
            if cursor <= x < cursor + span:
                return index
            cursor += span + 2
        return len(self._segments) - 1 if x >= cursor else -1

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self._segments:
            return
        theme = tokens.active_theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)

        total = sum(weight for _, weight, _ in self._segments)
        gap = 2
        usable = max(1.0, self.width() - (len(self._segments) - 1) * gap)
        height = self.height()
        radius = min(4.0, height / 2)

        cursor = 0.0
        for index, (name, weight, progress) in enumerate(self._segments):
            span = usable * (weight / total)
            track = QPainterPath()
            track.addRoundedRect(cursor, 0.0, span, float(height), radius, radius)
            fill = QColor(theme.bg_alt)
            if index == self._hover:
                fill = QColor(theme.surface_hover)
            if index == self._active:
                fill = QColor(theme.accent_soft)
            painter.setBrush(fill)
            painter.drawPath(track)

            if progress > 0:
                done = QPainterPath()
                done.addRoundedRect(cursor, 0.0, max(span * progress, 3.0), float(height),
                                    radius, radius)
                colour = QColor(theme.ok if progress >= 1.0 else theme.accent)
                if index == self._active:
                    colour = QColor(theme.accent_gradient_top)
                painter.setBrush(colour)
                painter.drawPath(done)

            if index != self._active and index == self._hover:
                painter.setPen(QPen(QColor(theme.border_accent), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(track)
                painter.setPen(Qt.PenStyle.NoPen)

            cursor += span + gap
        painter.end()


class CircularProgress(QWidget):
    """A ring that fills as a value approaches its maximum.

    Where a bar is the right control is in a row of other information; where a
    ring is right is in a corner, next to a label, in a space too short for a bar
    to be readable at all. The status bar is exactly that: eight pixels tall, and
    a bar at eight pixels tells nobody anything.

    `setIndeterminate(True)` turns it into a spinner for work with no known
    duration - an erase, which the chip decides the length of. The spinner is a
    quarter arc rotating at a constant speed, which reads as "working" without
    implying a fraction of anything.

    Drawn rather than assembled from pixmaps so it stays sharp at any size and
    takes its colours from the theme, which is what lets one widget look right on
    all three palettes.
    """

    _STATE_TOKENS = {"ok": "ok", "warn": "warn", "error": "error"}

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        size: int = 24,
        thickness: int = 3,
        text_visible: bool = False,
    ) -> None:
        super().__init__(parent)
        self._thickness = thickness
        self._text_visible = text_visible
        self._value = 0.0
        self._indeterminate = False
        self._state = ""
        self._angle = 0.0
        self._spin: QPropertyAnimation | None = None
        self.setFixedSize(QSize(size, size))
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    # -- api ---------------------------------------------------------------

    def setValue(self, value: float) -> None:  # noqa: N802 - Qt naming
        """Sets 0..100. Ignored while indeterminate, which has no value."""
        if self._indeterminate:
            return
        clamped = max(0.0, min(100.0, float(value)))
        if abs(clamped - self._value) < 0.01:
            return
        self._value = clamped
        self.update()

    def value(self) -> float:
        return self._value

    def setIndeterminate(self, indeterminate: bool) -> None:  # noqa: N802
        if indeterminate == self._indeterminate:
            return
        self._indeterminate = bool(indeterminate)
        if self._indeterminate and animations.animations_enabled():
            self._start_spin()
        else:
            self._stop_spin()
        self.update()

    def isIndeterminate(self) -> bool:  # noqa: N802
        return self._indeterminate

    def setState(self, state: str) -> None:
        """ok, warn, error, or "" for the accent default."""
        self._state = state
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return self.size()

    # -- the spinning angle, as an animatable Qt property --------------------

    def _get_angle(self) -> float:
        return self._angle

    def _set_angle(self, value: float) -> None:
        self._angle = float(value)
        self.update()

    angle = pyqtProperty(float, _get_angle, _set_angle)

    # -- internals ---------------------------------------------------------

    def _start_spin(self) -> None:
        # One animation, created once and started once: a spinner that is
        # restarted every time a job reports progress would stutter exactly when
        # the operations are arriving.
        if self._spin is None:
            animation = QPropertyAnimation(self, b"angle", self)
            animation.setDuration(1100)
            animation.setStartValue(0.0)
            animation.setEndValue(360.0)
            animation.setLoopCount(-1)
            # Linear: a spinner that eases is a spinner that looks like it is
            # catching up with something, and it is not catching up with anything.
            animation.setEasingCurve(QEasingCurve.Type.Linear)
            self._spin = animation
        self._spin.start()

    def _stop_spin(self) -> None:
        if self._spin is not None:
            self._spin.stop()
        self._angle = 0.0

    def _colour(self) -> QColor:
        theme = tokens.active_theme()
        if self._state in self._STATE_TOKENS:
            return QColor(theme.colour(self._STATE_TOKENS[self._state]))
        if not self._indeterminate and self._value >= 100.0:
            return QColor(theme.ok)
        return QColor(theme.accent)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        thickness = self._thickness
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        inset = thickness / 2.0 + 0.5
        box = QRectF(inset, inset, self.width() - 2 * inset, self.height() - 2 * inset)
        if box.width() <= 0 or box.height() <= 0:
            painter.end()
            return

        theme = tokens.active_theme()
        track = QPen(QColor(theme.border), thickness)
        track.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(track)
        painter.drawArc(box, 0, 360 * 16)

        pen = QPen(self._colour(), thickness)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        if self._indeterminate:
            # A quarter turn, drawn from the animated angle. Negative sweep so it
            # turns clockwise, which is the direction every platform's spinner
            # turns and therefore the one nobody has to think about.
            start = int((90.0 - self._angle) * 16)
            painter.drawArc(box, start, int(-90 * 16))
        elif self._value > 0:
            painter.drawArc(box, 90 * 16, int(-self._value / 100.0 * 360 * 16))

        if self._text_visible and not self._indeterminate:
            painter.setPen(QColor(theme.text))
            font = self.font()
            font.setPointSize(max(7, int(self.height() / 4)))
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, f"{self._value:.0f}%")
        painter.end()


class PulseRing(QWidget):
    """Rings that expand outwards and fade, drawn around a centre dot.

    The one animation in the interface whose job is to be noticed. It is used
    when something has *arrived* - a device connected, an image finished
    verifying - and then it stops: `start()` runs a fixed number of rings and
    halts.

    That is deliberate. A permanently pulsing indicator stops being a signal
    within about ten seconds and becomes wallpaper, and it also costs a repaint
    forever. Three rings say "this just happened"; an infinite loop says nothing.
    """

    _TONES = {"ok": "ok", "accent": "accent", "warn": "warn", "error": "error"}

    def __init__(self, parent: QWidget | None = None, *, size: int = 26, tone: str = "ok") -> None:
        super().__init__(parent)
        self._tone = tone
        self._phase = 0.0
        self._rings = 2
        self._animation: QPropertyAnimation | None = None
        self.setFixedSize(QSize(size, size))
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setVisible(False)

    def setTone(self, tone: str) -> None:
        self._tone = tone if tone in self._TONES else "ok"
        self.update()

    def start(self, *, rings: int = 3, period_ms: int = 900) -> None:
        """Shows and runs `rings` rings, then hides itself again."""
        if not animations.animations_enabled():
            # With motion off there is nothing to see, so showing an empty circle
            # would be a widget that appears for no reason and then disappears.
            return
        self.setVisible(True)
        self.raise_()
        if self._animation is None:
            animation = QPropertyAnimation(self, b"phase", self)
            animation.setEasingCurve(QEasingCurve.Type.Linear)
            animation.finished.connect(self._finished)
            self._animation = animation
        self._animation.stop()
        self._animation.setDuration(period_ms)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setLoopCount(max(1, rings))
        self._animation.start(QAbstractAnimation.DeletionPolicy.KeepWhenStopped)

    def stop(self) -> None:
        if self._animation is not None:
            self._animation.stop()
        self._phase = 0.0
        self.setVisible(False)

    def _get_phase(self) -> float:
        return self._phase

    def _set_phase(self, value: float) -> None:
        self._phase = float(value)
        self.update()

    phase = pyqtProperty(float, _get_phase, _set_phase)

    def _finished(self) -> None:
        """Called when the last ring has run. Hides the widget."""
        self.setVisible(False)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        colour = QColor(tokens.active_theme().colour(self._TONES[self._tone]))
        centre = self.rect().center()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        dot = 4.0
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        painter.drawEllipse(centre, dot, dot)

        for index in range(self._rings):
            # Each ring is half a period behind the last, so there is always one
            # on the way out rather than a visible gap between them.
            progress = (self._phase + index / self._rings) % 1.0
            radius = dot + (self.width() / 2.0 - dot - 1.0) * progress
            faded = QColor(colour)
            faded.setAlpha(int(150 * (1.0 - progress) ** 1.5))
            pen = QPen(faded, 2.0)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(centre, radius, radius)
        painter.end()


# -----------------------------------------------------------------------------
#  Tables and tabs
# -----------------------------------------------------------------------------


class StyledTable(QTableWidget):
    """A table with the styling already applied and no editing by default.

    Read-only and row-selected, which is what every table in this tool wants: a
    device list is not a spreadsheet, and a cell that can be typed into by
    accident is a cell that will be.
    """

    def __init__(self, rows: int = 0, columns: int = 0, parent: QWidget | None = None) -> None:
        super().__init__(rows, columns, parent)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(30)
        self.horizontalHeader().setHighlightSections(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def setRowMenu(self, builder, *, title: str = "") -> None:
        """Attaches a context menu, built fresh for whichever row is clicked.

        The builder is called with the row index and returns a list of
        (label, callable) pairs; returning an empty list shows no menu. Building
        it on demand rather than up front is what lets the entries depend on the
        row - "copy the partition name" only makes sense over a partition, and a
        disabled entry that never becomes enabled is worse than an absent one.

        Qt's own context-menu policy is used rather than a mouse handler, so the
        menu also opens from the keyboard's context key, which a hand-rolled
        right-click handler silently loses.
        """
        self._menu_builder = builder
        self._menu_title = title
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

    def _show_menu(self, position) -> None:
        builder = getattr(self, "_menu_builder", None)
        if builder is None:
            return
        index = self.indexAt(position)
        row = index.row() if index.isValid() else -1
        entries = builder(row)
        if not entries:
            return

        menu = QMenu(self)
        title = getattr(self, "_menu_title", "")
        if title:
            header = menu.addAction(title)
            header.setEnabled(False)
            menu.addSeparator()
        for label, callback in entries:
            action = menu.addAction(label)
            if callback is None:
                action.setEnabled(False)
            else:
                action.triggered.connect(callback)
        menu.exec(self.viewport().mapToGlobal(position))

    def setHeaders(self, headers: Iterable[str]) -> None:
        labels = list(headers)
        self.setColumnCount(len(labels))
        self.setHorizontalHeaderLabels(labels)

    def stretch_last_column(self, stretch: bool = True) -> None:
        """Gives the last column the slack, so short tables do not trail off."""
        from PyQt6.QtWidgets import QHeaderView

        header = self.horizontalHeader()
        header.setStretchLastSection(stretch)
        for index in range(self.columnCount() - 1):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)


class StyledTabBar(QTabBar):
    """A tab bar that fades the new pane in when the selection changes.

    Qt switches panes instantly, which on a panel full of controls reads as a
    flicker rather than as a change. The fade is short and runs against the new
    pane only - cross-fading two panes needs both alive at once, and the flash
    of the wrong one is worse than no animation.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setExpanding(False)
        self.setDrawBase(False)
        self.setUsesScrollButtons(True)
        self.setElideMode(Qt.TextElideMode.ElideRight)
        self.setMovable(False)
        self.currentChanged.connect(self._on_changed)

    def _on_changed(self, index: int) -> None:
        parent = self.parentWidget()
        while parent is not None and not isinstance(parent, QTabWidget):
            parent = parent.parentWidget()
        if parent is None:
            return
        pane = parent.widget(index)
        if pane is not None:
            animations.fade_in(pane, animations.motion.transition, visible=True)

    def tabSizeHint(self, index: int) -> QSize:  # noqa: N802 - Qt naming
        """Reserves the width the label needs.

        Qt's default measurement ignores the letter-spacing and padding the
        stylesheet applies, so a bold tab ends up elided. Measuring the label
        with the font it will be drawn in is what keeps "Qualcomm" from becoming
        "Qualc…".
        """
        size = super().tabSizeHint(index)
        metrics = self.fontMetrics()
        text = self.tabText(index)
        width = metrics.horizontalAdvance(text) + 46
        return QSize(max(size.width(), width), max(size.height(), 34))


class StyledTabWidget(QTabWidget):
    """A tab widget using the styled bar, with icon support.

    Separate from StyledTabBar so a caller can take the tab widget and get both
    without remembering to set the bar as well.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setTabBar(StyledTabBar(self))
        self.setDocumentMode(True)
        self.tabBar().setObjectName("VendorTabs")

    def addIconTab(self, widget: QWidget, label: str, icon: str) -> int:
        """Adds a tab with an icon from the icon set, coloured for the tab state."""
        index = self.addTab(widget, label)
        self.setTabIcon(index, icons.icon(icon, tokens.active_theme().text_muted,
                                          tokens.METRICS.icon_size))
        return index


# -----------------------------------------------------------------------------
#  Inputs
# -----------------------------------------------------------------------------


class SearchField(QLineEdit):
    """A line edit with a leading magnifier and a clear button.

    The clear button is Qt's own, restyled, rather than a second widget: it
    appears and disappears with the text by itself, and reimplementing that is
    how the two get out of step.
    """

    def __init__(self, parent: QWidget | None = None, *, placeholder: str = "Search") -> None:
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        self.addAction(
            icons.icon("search", tokens.active_theme().text_muted, tokens.METRICS.icon_size),
            QLineEdit.ActionPosition.LeadingPosition,
        )

    def setPlaceholderText(self, text: str) -> None:  # noqa: N802 - Qt naming
        super().setPlaceholderText(text)
        self.setAccessibleName(text)


class StyledComboBox(QComboBox):
    """A combo box with the custom chevron from the stylesheet.

    Nothing to add beyond the base class except a default cursor and an API for
    populating from data, which is what stops every caller writing the same
    `for item: addItem(item.label, item.value)` loop.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        options: Sequence[tuple[str, object]] | None = None,
        current: object = None,
    ) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if options:
            self.setOptions(options, current=current)

    def setOptions(self, options: Sequence[tuple[str, object]], *, current: object = None) -> None:
        """Fills the box from (label, value) pairs, optionally selecting one."""
        self.clear()
        for label, value in options:
            self.addItem(label, value)
        if current is not None:
            self.setCurrentValue(current)

    def setCurrentValue(self, value: object) -> bool:  # noqa: N802
        index = self.findData(value)
        if index < 0:
            return False
        self.setCurrentIndex(index)
        return True

    def currentValue(self) -> object:
        return self.currentData()


class StyledCheckBox(QCheckBox):
    """A checkbox with the custom indicator and a hover cursor."""

    def __init__(self, text: str = "", parent: QWidget | None = None, *, tooltip: str = "") -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if tooltip:
            self.setToolTip(tooltip)
