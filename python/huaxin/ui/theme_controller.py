"""Theme switching at runtime.

Three things have to move together when the theme changes, and getting any one
of them out of step is what makes a live theme switch look broken:

  1. The stylesheet, rebuilt from the template with the new tokens.
  2. The palette, for everything the stylesheet does not name.
  3. Every icon that was rendered in the old theme's colours - window controls,
     combo arrows, chips, empty-state glyphs - because an SVG recoloured at
     render time keeps whatever colour it was first drawn with until it is
     rendered again.

So a switch is not "set the stylesheet"; it is a broadcast that every widget
which draws a theme-coloured pixmap has to answer. Widgets that own such pixmaps
implement `apply_theme()` and are registered here. That is a small protocol, and
the alternative - reaching into the widget tree looking for anything that might
hold a pixmap - is how a theme switch leaves three icons behind.

The chooser in the title bar calls `ThemeController.set_theme`, which applies the
change and records it in the settings file so it survives a restart.
"""

from __future__ import annotations

from typing import Callable, Protocol

from PyQt6.QtWidgets import QApplication, QWidget

from huaxin.ui import style, tokens

__all__ = ["ThemeAware", "ThemeController", "theme_picker"]


class ThemeAware(Protocol):
    """A widget that renders theme-coloured pixmaps and can re-render them.

    Structural, not inherited: a widget only has to *have* the method. Requiring
    a base class would mean every panel in the application inheriting from a
    theme class it otherwise has no use for.
    """

    def apply_theme(self) -> None:  # pragma: no cover - protocol definition
        ...


class ThemeController:
    """Applies themes and keeps the widgets that need re-rendering in step.

    Deliberately not a QObject: it emits nothing. The widgets that care are
    registered, and a signal would only add a hop through the event loop before
    the same list of calls happened.
    """

    def __init__(self) -> None:
        self._aware: list[ThemeAware] = []
        self._on_change: list[Callable[[str], None]] = []
        self._current = tokens.active_theme().name

    # -- registry ----------------------------------------------------------

    def register(self, *widgets: ThemeAware) -> None:
        """Registers widgets to be re-rendered on a theme change.

        A weak reference would be tidier and is not used on purpose: a widget
        that has been destroyed is worse than a leak here, because calling
        `apply_theme` on it raises from inside a paint cycle. Callers unregister
        on destruction instead, via the usual Qt parent-child lifetime.
        """
        for widget in widgets:
            if widget not in self._aware:
                self._aware.append(widget)

    def unregister(self, widget: ThemeAware) -> None:
        try:
            self._aware.remove(widget)
        except ValueError:
            pass

    def on_change(self, callback: Callable[[str], None]) -> None:
        """Called after a theme is applied, with the new name.

        Used by the main window to persist the choice, and by the log console to
        re-colour the lines already in it - a log full of the old theme's greens
        is the most visible thing a switch can leave behind.
        """
        self._on_change.append(callback)

    # -- api ---------------------------------------------------------------

    @property
    def current(self) -> str:
        return self._current

    def set_theme(self, name: str) -> str:
        """Applies a theme and re-renders everything that depends on it."""
        if name not in tokens.THEMES:
            raise KeyError(f"no such theme: {name!r}")
        self._current = style.apply_theme(QApplication.instance(), name)

        # A copy: a widget's apply_theme may register or unregister something,
        # and mutating the list mid-iteration would skip the next entry.
        for widget in list(self._aware):
            try:
                widget.apply_theme()
            except RuntimeError:
                # The widget's C++ side is gone. Dropping it is right: it can
                # never be re-rendered and would raise on every future switch.
                self.unregister(widget)

        for callback in self._on_change:
            callback(self._current)
        return self._current

    def themes(self) -> list[tuple[str, str]]:
        """(label, name) pairs, default first, ready for a chooser.

        The order is `(label, name)` and not `(name, label)` because that is the
        order `StyledComboBox(options=...)` takes - and getting it backwards is
        not a cosmetic mistake. The picker ends up displaying the raw key, never
        selecting the current entry, and handing the *label* back to
        `set_theme`, which rejects it: the chooser looks fine and changes
        nothing. Which is exactly what it did.
        """
        return [(tokens.THEMES[name].label, name) for name in tokens.theme_names()]


def theme_picker(controller: ThemeController, parent: QWidget | None = None):
    """A small combo box for the title bar that switches the theme.

    Returns the widget; the caller owns it. Placed in the title bar rather than
    in Settings because a theme is something an operator tries, not something
    they configure once - and having to open a dialog and press OK to see what
    Daylight looks like means nobody ever will.
    """
    from huaxin.ui.components import StyledComboBox

    picker = StyledComboBox(
        parent,
        options=controller.themes(),
        current=controller.current,
    )
    picker.setToolTip("Colour theme")
    picker.setFixedWidth(112)
    picker.setFixedHeight(tokens.METRICS.control_height_sm)

    def changed() -> None:
        controller.set_theme(str(picker.currentValue()))

    picker.currentIndexChanged.connect(lambda _index: changed())

    def sync(name: str) -> None:
        """Moves the picker without telling the controller - it asked."""
        picker.blockSignals(True)
        picker.setCurrentValue(name)
        picker.blockSignals(False)

    # Exposed so a caller that has already changed the theme - the settings
    # dialog can - can put the picker back in step. `hasattr` guards at the call
    # site instead of this would hide exactly the bug above.
    picker.syncToTheme = sync  # type: ignore[attr-defined]
    return picker
