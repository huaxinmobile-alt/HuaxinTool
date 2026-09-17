"""Loads the stylesheet template and fills in the theme's tokens.

The sheet itself lives in `styles/huaxin.qss` as a template full of `@token`
placeholders; this module is what turns it into something Qt can use. Keeping the
two apart means the styling can be read and edited as a stylesheet rather than as
a Python string, which is the difference between a designer being able to work on
it and not.

WHAT IS SUBSTITUTED

* Every `@token` from `tokens.tokens()` - colours, radii, spacing, font sizes.
  A token name that does not exist is a hard error, not a blank: Qt drops a rule
  it cannot parse without saying so, and a silently missing `border-radius` is
  exactly the kind of fault nobody notices until the interface looks wrong and
  nobody can say why.
* Three icon paths, because Qt's stylesheet `image:` takes a URL and cannot take
  an inline SVG. `icons.icon_path()` writes the recoloured file and this module
  splices in where it landed.

WHY THE RESULT IS CACHED PER THEME. Building the sheet parses ~1200 lines and
writes three files. Doing that on every repaint would be absurd, and doing it on
every widget construction is what makes a Qt app feel slow to start.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication, QWidget

from huaxin.ui import icons, tokens

__all__ = [
    "STYLESHEET_PATH",
    "stylesheet",
    "apply_theme",
    "restyle",
    "placeholder_colour",
    "colour",
]

def _locate_stylesheet() -> Path:
    """Finds the stylesheet template.

    More than one candidate, because "next to this module" is not the same place
    in a source checkout and in a frozen build, and getting it wrong is fatal at
    startup. The first candidate is the package-relative one, which is what the
    PyInstaller spec preserves; the rest cover a bundle that laid the tree out
    differently, and the working directory covers a build that did not ship it at
    all but has the source beside it.
    """
    here = Path(__file__).resolve().parent
    candidates = [
        here / "styles" / "huaxin.qss",
        here.parent / "styles" / "huaxin.qss",
        Path(getattr(sys, "_MEIPASS", here)) / "huaxin" / "ui" / "styles" / "huaxin.qss",
        Path.cwd() / "styles" / "huaxin.qss",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    # Nothing found: report every place that was looked, because the fix depends
    # on which one it should have been.
    return candidates[0]


STYLESHEET_PATH = _locate_stylesheet()

#: A token reference, e.g. `@accent`. Anchored to word boundaries so it cannot
#: eat the `@` in an at-rule or a URL.
_TOKEN = re.compile(r"@([a-z][a-z0-9_]*)")

#: A stylesheet comment, including its delimiters.
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

#: Token name -> the icon it points at, for the three the stylesheet needs.
_ICON_TOKENS = {
    "icon_chevron_path": "chevron-down",
    "icon_check_path": "check",
    "icon_close_path": "close",
}

_cache: dict[str, str] = {}


def _read_template() -> str:
    try:
        return STYLESHEET_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        # Without this the application would start with no styling at all and
        # look broken rather than failing, which is harder to diagnose.
        raise RuntimeError(
            f"the stylesheet template is missing: {STYLESHEET_PATH} ({exc})"
        ) from exc


def _resolve(name: str, theme_tokens: dict[str, str]) -> str:
    if name in _ICON_TOKENS:
        return icons.icon_path(_ICON_TOKENS[name])
    try:
        return theme_tokens[name]
    except KeyError as exc:
        line = _line_of(name)
        raise KeyError(
            f"the stylesheet uses @{name}, which is not a known token"
            + (f" (first seen on line {line})" if line else "")
            + f".\nKnown tokens: {len(theme_tokens)}; icons: {sorted(_ICON_TOKENS)}"
        ) from exc


_template_lines: list[str] | None = None


def _line_of(name: str) -> int | None:
    """Line number of the first use of a token, for the error message."""
    global _template_lines
    if _template_lines is None:
        _template_lines = _read_template().splitlines()
    needle = f"@{name}"
    for number, line in enumerate(_template_lines, 1):
        if needle in line:
            return number
    return None


def stylesheet(theme_name: str | None = None, *, refresh: bool = False) -> str:
    """The application stylesheet for a theme, with every token resolved.

    Cached by theme name. `refresh=True` rebuilds it, which is what a theme
    switch does after the icon cache has been cleared.
    """
    theme = tokens.active_theme() if theme_name is None else tokens.THEMES[theme_name]
    key = theme.name
    if not refresh:
        cached = _cache.get(key)
        if cached is not None:
            return cached

    template = _read_template()
    resolved = tokens.tokens(theme)
    rendered = _substitute(template, resolved)
    _cache[key] = rendered
    return rendered


def _substitute(template: str, resolved: dict[str, str]) -> str:
    """Fills in tokens everywhere except inside comments.

    Comments are left alone deliberately. The template's own header explains
    what a token is, so it contains the word `@token` - and without this, that
    prose would be treated as a reference to a token called "token" and fail the
    build with a confusing error about line 4. Comments are also the one place a
    reader might write `@something` as an example.
    """
    parts: list[str] = []
    cursor = 0
    for comment in _COMMENT.finditer(template):
        chunk = template[cursor:comment.start()]
        parts.append(_TOKEN.sub(lambda match: _resolve(match.group(1), resolved), chunk))
        parts.append(comment.group(0))
        cursor = comment.end()
    tail = template[cursor:]
    parts.append(_TOKEN.sub(lambda match: _resolve(match.group(1), resolved), tail))
    return "".join(parts)


def colour(token: str, theme_name: str | None = None) -> str:
    """A token's value from a theme, for the places that need a colour in code.

    Qt's stylesheet cannot express everything - a drop shadow's colour and a
    gradient stop in a QPainter are both set from Python - so code needs the
    same tokens the sheet uses, and needs them to be the same values.
    """
    theme = tokens.active_theme() if theme_name is None else tokens.THEMES[theme_name]
    value = theme.colour(token)
    if value.startswith("rgba"):
        return value
    return value


def placeholder_colour() -> QColor:
    """The colour of placeholder text, as a QColor.

    Qt takes this from the palette rather than the stylesheet - there is no
    `QLineEdit::placeholder` sub-control, and a rule that looks like one is
    silently ignored.
    """
    return QColor(tokens.active_theme().text_disabled)


def apply_theme(app: QApplication | None = None, theme_name: str | None = None) -> str:
    """Applies a theme to the running application and returns its name.

    Also sets the parts of the appearance Qt will not take from a stylesheet:
    the palette roles that show through where no rule matches (a tooltip's
    border, the text colour inside a native dialog), and the placeholder colour.
    Without the palette, switching to the light theme leaves dark grey text on
    white in every control the sheet does not reach.
    """
    target = app or QApplication.instance()
    if target is None:
        raise RuntimeError("apply_theme needs a running QApplication")

    if theme_name is not None:
        tokens.set_active_theme(theme_name)
    theme = tokens.active_theme()

    # The icons are coloured with the old theme's text colour, so they have to
    # go before the sheet is rebuilt - otherwise the combo chevron keeps the
    # colour it was first drawn in.
    icons.clear_cache()
    global _template_lines
    _template_lines = None

    target.setStyleSheet(stylesheet(theme.name, refresh=True))
    target.setPalette(_palette_for(theme))
    return theme.name


def _palette_for(theme: tokens.Theme) -> QPalette:
    """A palette matching the theme.

    A stylesheet does not replace the palette; it layers on top of it. Anything
    the sheet does not name falls back to these roles, so they have to agree with
    it or the two disagree visibly - a white tooltip on the light theme and a
    dark grey one on the dark theme, from the same application.
    """
    palette = QPalette()
    background = QColor(theme.bg)
    surface = QColor(theme.surface)
    text = QColor(theme.text)
    muted = QColor(theme.text_muted)
    accent = QColor(theme.accent)
    on_accent = QColor(theme.text_on_accent)

    palette.setColor(QPalette.ColorRole.Window, background)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, QColor(theme.bg_alt))
    palette.setColor(QPalette.ColorRole.AlternateBase, surface)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(theme.text_disabled))
    palette.setColor(QPalette.ColorRole.Button, QColor(theme.surface_raised))
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, QColor(theme.error))
    palette.setColor(QPalette.ColorRole.Highlight, accent)
    palette.setColor(QPalette.ColorRole.HighlightedText, on_accent)
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(theme.surface_alt))
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Link, accent)
    palette.setColor(QPalette.ColorRole.Mid, QColor(theme.border))
    palette.setColor(QPalette.ColorRole.Dark, QColor(theme.bg_alt))
    palette.setColor(QPalette.ColorRole.Light, QColor(theme.surface_hover))

    # Disabled roles are separate in Qt and are not derived from the enabled
    # ones, so leaving them unset gives disabled text the platform default - on
    # Windows that is a mid grey that is invisible on a dark background.
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, muted)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight,
                     QColor(theme.surface_raised))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.HighlightedText, muted)
    return palette


def restyle(*widgets: QWidget) -> None:
    """Re-evaluates stylesheet rules that depend on a Qt property.

    Qt evaluates a property selector such as `QPushButton[role="primary"]` when a
    widget is polished and does not re-evaluate it when the property changes -
    so setting `role` after construction has no effect until the widget is
    unpolished and polished again. This is that dance, in one place, because
    forgetting it is the reason a button keeps the old style and nobody can tell
    why.
    """
    for widget in widgets:
        if widget is None:
            continue
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.update()
