"""The furniture every vendor tab is built from.

A tab is the same six things five times over: a heading, a sentence explaining
what the mode is for, a badge saying whether it works, a box showing which device
the buttons will act on, a grid of actions, and a line of help at the bottom.
Writing that out five times is how five tabs end up looking almost-but-not-quite
the same, and "almost" is what makes an interface look unfinished.

So it is written once, here, and each tab supplies its own words and actions.

THE TWO-COLUMN GRID, AND WHY THE ORDER IS THE API. The columns are filled row by
row from a single ordered list: index 0 goes top-left, 1 top-right, 2 second row
left, and so on. That means the *order of the list is the layout*, which is worth
stating plainly because it is not obvious from looking at a list of actions that
they will be arranged in two columns. `left_column()` and `right_column()` exist
so a test can assert that arrangement rather than a reader having to work it out,
and so a future edit that reorders the list is caught as a layout change rather
than admired as a tidy-up.

The alternative - separate `left=` and `right=` arguments - reads better but
breaks down the moment a row has to span both columns or a tab wants three
columns. One ordered list and a documented rule covers all of it.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from huaxin.ui import components as ui
from huaxin.ui import icons, style, tokens

__all__ = [
    "StatusBadge",
    "DeviceBox",
    "ActionGrid",
    "InfoNote",
    "TabHeader",
    "left_column",
    "right_column",
]


class _IconText(QWidget):
    """An icon and a line of text, side by side.

    A QWidget rather than a QLabel because a QLabel holds *either* a pixmap or
    text: `setPixmap` replaces the text rather than sitting beside it. Two widgets
    inside one is the only way to have both, and this exists so the two below do
    not each rediscover that.
    """

    def __init__(self, parent: QWidget | None = None, *, spacing: int | None = None) -> None:
        super().__init__(parent)
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(tokens.METRICS.gap_sm if spacing is None else spacing)

        self.icon = QLabel(self)
        self.icon.setFixedWidth(tokens.METRICS.icon_size + 4)
        self._row.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)

        self._text = QLabel(self)
        self._text.setWordWrap(True)
        self._row.addWidget(self._text, 1)

    def setIcon(self, name: str, colour: str) -> None:
        self.icon.setPixmap(icons.pixmap(name, colour, tokens.METRICS.icon_size))

    def setText(self, value: str) -> None:
        self._text.setText(value)

    def text_value(self) -> str:
        return self._text.text()


class StatusBadge(_IconText):
    """Whether this tab's actions work, as an icon *and* a sentence.

    Both, not one or the other: the icon is what an operator sees at a glance,
    and the sentence is what tells them what specifically does and does not work.
    Colour alone would be no signal at all for anyone who cannot see it, and this
    is the one label on the tab that says whether pressing the buttons will do
    anything.
    """

    #: state -> (icon, tone). The tones are the ones the stylesheet defines.
    _STATES = {
        "ready": ("success", "ok"),
        "partial": ("warning", "warn"),
        "pending": ("info", "accent"),
        "unavailable": ("error", "error"),
    }

    def __init__(self, text: str = "", parent: QWidget | None = None, *, state: str = "pending") -> None:
        super().__init__(parent, spacing=tokens.METRICS.gap_sm)
        self.setProperty("role", "chip")
        self._state = "pending"
        self.setText(text)
        self.setState(state)

    def setState(self, state: str) -> None:
        self._state = state if state in self._STATES else "pending"
        icon_name, tone = self._STATES[self._state]
        self.setProperty("tone", tone)
        token = {"ok": "ok", "warn": "warn", "error": "error", "accent": "accent"}[tone]
        self.setIcon(icon_name, tokens.active_theme().colour(token))
        self._text.setStyleSheet(
            f"color: {tokens.active_theme().colour(token)}; font-weight: 600;"
        )
        style.restyle(self)

    def state(self) -> str:
        return self._state

    # The widget is used where a label was, so it answers the two calls that
    # matter with the same names a QLabel would have.
    def text(self) -> str:  # noqa: A003 - Qt naming
        return self.text_value()

    def setText(self, value: str) -> None:  # noqa: N802 - Qt naming
        self._text.setText(value)


class DeviceBox(QWidget):
    """The box naming the device the actions will act on.

    Two states, and the difference matters: nothing selected is a neutral box
    saying what to do about it, and something selected is an accent-marked box
    saying exactly which device. An operator about to erase a partition needs to
    be certain which one they are pointing at, and a box that looks the same
    either way does not give them that.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            tokens.METRICS.pad_sm, tokens.METRICS.gap_sm,
            tokens.METRICS.pad_sm, tokens.METRICS.gap_sm,
        )
        layout.setSpacing(tokens.METRICS.gap_sm)

        self._dot = ui.StatusDot(tone="muted", size=8)
        layout.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignVCenter)

        self._text = QLabel(self)
        self._text.setWordWrap(True)
        layout.addWidget(self._text, 1)

        self.setDevice(None)

    def setDevice(self, device: object | None) -> None:
        if device is None:
            self._dot.setTone("muted")
            self._text.setText(
                "No device selected — run <b>Scan Devices</b> (F5) and pick one from the list."
            )
            self._text.setStyleSheet(f"color: {tokens.active_theme().text_muted};")
        else:
            self._dot.setTone("ok")
            self._text.setText(
                f"<b>{device.usb_id}</b> · {device.vendor} · {device.description} "
                f"<span style='color:{tokens.active_theme().text_muted}'>"
                f"(bus {device.location})</span>"
            )
            self._text.setStyleSheet(f"color: {tokens.active_theme().text};")
        self.setProperty("selected", device is not None)
        style.restyle(self)


class ActionGrid(QWidget):
    """The tab's buttons, in two columns.

    Filled row by row from one ordered list, so the order of the list is the
    layout - see the module docstring. `left_column()` and `right_column()` report
    what that produces, which is what the tests assert against.
    """

    def __init__(
        self,
        actions: Sequence[object],
        parent: QWidget | None = None,
        *,
        columns: int = 2,
        on_action=None,
    ) -> None:
        super().__init__(parent)
        self._columns = max(1, columns)
        self._buttons: dict[str, ui.StyledButton] = {}
        self._actions = list(actions)
        self._busy = False

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(tokens.METRICS.gap_sm)
        grid.setVerticalSpacing(tokens.METRICS.gap_sm)

        first_implemented: int | None = None
        for index, spec in enumerate(self._actions):
            if spec.handler is not None and first_implemented is None:
                first_implemented = index
        if first_implemented is None:
            first_implemented = -1

        for index, spec in enumerate(self._actions):
            button = ui.StyledButton(
                spec.label,
                self,
                role=self._role_for(spec, index, first_implemented),
                icon=getattr(spec, "icon", None),
                icon_colour_token="error" if spec.danger else None,
            )
            button.setToolTip(self._tooltip_for(spec))
            # Every button is the same width, so the two columns line up and a
            # long label does not make one column wider than the other.
            button.setSizePolicy(button.sizePolicy().horizontalPolicy(), button.sizePolicy().verticalPolicy())
            if on_action is not None:
                from functools import partial

                button.clicked.connect(partial(on_action, spec))
            if not getattr(spec, "implemented", True):
                # Set once, here, as well as in set_enabled_states: a panel that
                # has not been refreshed yet must not show a dead button as live.
                button.setEnabled(False)
                button.setToolTip(f"{spec.help}")
            self._buttons[spec.key] = button
            grid.addWidget(button, index // self._columns, index % self._columns)

        # Equal stretch on both columns: an operator reads the two columns as a
        # pair, and a grid where the right one is wider looks like a mistake.
        for column in range(self._columns):
            grid.setColumnStretch(column, 1)

    # -- layout ------------------------------------------------------------

    def _role_for(self, spec: object, index: int, primary: int) -> str:
        """Which button role an action gets.

        The first implemented, non-destructive action on the tab is its primary
        one - the button an operator came to that tab to press. Everything else
        is secondary, and anything destructive is a danger button, which the
        stylesheet draws in red so it cannot be mistaken for the rest.
        """
        if spec.danger:
            return "danger"
        if index == primary and spec.handler is not None:
            return "primary"
        return "secondary"

    def _tooltip_for(self, spec: object) -> str:
        if spec.handler is not None:
            return spec.help
        phase = spec.phase or getattr(self.parent(), "phase", "a later phase")
        return f"{spec.help}\n\nNot implemented yet — lands in {phase}."

    def buttons(self) -> dict[str, ui.StyledButton]:
        return dict(self._buttons)

    def button(self, key: str) -> ui.StyledButton | None:
        return self._buttons.get(key)

    # -- state -------------------------------------------------------------

    def set_busy(self, busy: bool) -> None:
        """Disables everything while a job runs.

        Disabling is the whole of it: a control that would race a running flash
        must not be pressable, and marking them merely as "busy" while leaving
        them live would be worse than doing nothing. The `busy` property is set
        as well so the stylesheet can soften them, which is what tells an
        operator the interface is working rather than broken.
        """
        if busy == self._busy:
            return
        self._busy = busy
        for button in self._buttons.values():
            button.setProperty("busy", busy)
        style.restyle(*self._buttons.values())

    def set_enabled_states(self, *, has_device: bool, busy: bool) -> None:
        """Applies the three rules that decide what can be pressed.

        Device requirement, whether the build implements the action at all, and
        whether something is running. The middle one is not a state that changes:
        an action this build cannot do stays disabled, and its tooltip is what
        explains it.
        """
        for spec in self._actions:
            button = self._buttons[spec.key]
            enabled = spec.requires_device is False or has_device
            if not getattr(spec, "implemented", True):
                enabled = False
            button.setEnabled(enabled and not busy)

    def requires_device(self, key: str) -> bool:
        """Whether this action needs a selected device."""
        for spec in self._actions:
            if spec.key == key:
                return bool(spec.requires_device)
        raise KeyError(key)


def left_column(actions: Sequence[object], columns: int = 2) -> list[str]:
    """The labels that land in the left column of a row-major grid."""
    return [spec.label for spec in actions[::columns]]


def right_column(actions: Sequence[object], columns: int = 2) -> list[str]:
    """The labels that land in the right column."""
    return [spec.label for spec in actions[1::columns]]


class InfoNote(_IconText):
    """The line of help at the bottom of a tab.

    Small and muted: it is there for the person who has not used this tab before,
    and its job is done the moment they have. A note set at body size would
    compete with the controls above it, which is not what a note is for.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None, *, icon: str | None = None) -> None:
        super().__init__(parent, spacing=tokens.METRICS.gap_sm)
        self.setProperty("role", "caption")
        self._text.setProperty("role", "caption")
        if icon:
            self.setIcon(icon, tokens.active_theme().text_disabled)
        else:
            self.icon.setVisible(False)
        self.setText(text)

    def text(self) -> str:  # noqa: A003 - Qt naming
        return self.text_value()

    def setText(self, value: str) -> None:  # noqa: N802 - Qt naming
        self._text.setText(value)
        self.setToolTip(value)


class TabHeader(QWidget):
    """Everything above the buttons: title, description, status and device box."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str,
        description: str,
        status: str,
        state: str = "pending",
        icon: str | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(tokens.METRICS.gap_sm)

        heading = QHBoxLayout()
        heading.setSpacing(tokens.METRICS.gap_sm)
        if icon:
            glyph = QLabel(self)
            glyph.setPixmap(icons.pixmap(icon, tokens.active_theme().accent, 20))
            glyph.setFixedWidth(24)
            heading.addWidget(glyph, 0, Qt.AlignmentFlag.AlignVCenter)

        title_label = QLabel(title, self)
        title_label.setProperty("role", "title")
        heading.addWidget(title_label, 0, Qt.AlignmentFlag.AlignVCenter)
        heading.addStretch(1)
        layout.addLayout(heading)

        self.description = QLabel(description, self)
        self.description.setProperty("role", "caption")
        self.description.setWordWrap(True)
        layout.addWidget(self.description)

        self.badge = StatusBadge(status, self, state=state)
        layout.addWidget(self.badge)

        self.device_box = DeviceBox(self)
        self.device_box.setObjectName("DeviceBox")
        layout.addWidget(self.device_box)
