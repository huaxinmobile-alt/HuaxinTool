"""Backwards-compatible façade over the design system.

This module used to hold the only stylesheet, as an f-string with the colours
written into it. The design system that replaced it lives in three files:

    tokens.py       the palettes and the metrics, one place per value
    styles/*.qss    the stylesheet, as a stylesheet
    style.py        the two joined, plus the Qt palette and icon handling

Everything here is kept so that existing imports keep working, and is defined in
terms of the new system rather than duplicating any of it. `COLORS` is a live
view of the active theme rather than a copy, so code that reads
`COLORS["accent"]` gets the current theme's accent and not the one that happened
to be active when this module was imported - which is the bug a copied dict
would have introduced the first time somebody switched theme.

New code should import from `tokens` and `style` directly.
"""

from __future__ import annotations

from typing import Iterator

from huaxin.ui import style, tokens

__all__ = ["COLORS", "LOG_COLORS", "build_stylesheet", "apply_theme", "theme_names"]


class _LiveColors(dict):
    """The active theme's colours, read at the moment of access.

    A plain dict snapshot would be correct until the theme changed and silently
    wrong afterwards, in the one place - a colour looked up at runtime - where it
    would be hardest to spot.
    """

    def __init__(self) -> None:
        super().__init__()

    def _current(self) -> dict[str, str]:
        theme = tokens.active_theme()
        return {
            "bg": theme.bg,
            "surface": theme.surface,
            "surface_alt": theme.surface_alt,
            "surface_hi": theme.surface_hover,
            "border": theme.border,
            "border_hi": theme.border_strong,
            "text": theme.text,
            "text_dim": theme.text_muted,
            "accent": theme.accent,
            "accent_hover": theme.accent_hover,
            "accent_press": theme.accent_press,
            "ok": theme.ok,
            "warn": theme.warn,
            "error": theme.error,
        }

    def __getitem__(self, key: str) -> str:
        return self._current()[key]

    def get(self, key: str, default=None):  # type: ignore[override]
        return self._current().get(key, default)

    def keys(self):  # type: ignore[override]
        return self._current().keys()

    def items(self):  # type: ignore[override]
        return self._current().items()

    def values(self):  # type: ignore[override]
        return self._current().values()

    def __iter__(self) -> Iterator[str]:
        return iter(self._current())

    def __len__(self) -> int:
        return len(self._current())

    def __contains__(self, key: object) -> bool:
        return key in self._current()

    def __repr__(self) -> str:
        return f"COLORS({tokens.active_theme().name}, {len(self)} entries)"


#: The active theme's colours under their historical names.
COLORS = _LiveColors()

#: Log level -> hex colour, used by the console widget.
LOG_COLORS = {
    "debug": "#7f8894",
    "info": "#c8d0d9",
    "output": "#86c5d8",
    "ok": "#4ec26a",
    "warn": "#e0b341",
    "error": "#ff6b6b",
}


def _refresh_log_colors() -> None:
    """Repoints the log colours at the active theme.

    Called on every access of LOG_COLORS through the function below rather than
    at import, because the console re-colours the lines already on screen after
    a theme switch and reads this dict to do it.
    """
    theme = tokens.active_theme()
    LOG_COLORS.update(
        {
            "debug": theme.log_debug,
            "info": theme.log_info,
            "output": theme.log_output,
            "ok": theme.ok,
            "warn": theme.warn,
            "error": theme.error,
        }
    )


def build_stylesheet() -> str:
    """The stylesheet for the active theme.

    Kept for the call sites that still ask for a sheet to hand to
    `setStyleSheet`; `style.apply_theme()` is the better entry point because it
    also sets the palette and re-renders the icons.
    """
    _refresh_log_colors()
    return style.stylesheet()


def apply_theme(app=None, name: str | None = None) -> str:
    """Applies a theme to the application."""
    applied = style.apply_theme(app, name)
    _refresh_log_colors()
    return applied


def theme_names() -> list[str]:
    """Every available theme, default first."""
    return tokens.theme_names()
