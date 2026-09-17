"""Vector icons, drawn as SVG and recoloured at render time.

WHY THE ICONS ARE SOURCE STRINGS AND NOT FILES. A file on disk can only be one
colour, so a folder of them means one file per colour per state - and the moment
a theme changes, every one of those files is wrong. Vector source in code can be
recoloured the instant a token changes, which is what makes the icons part of the
design system rather than decoration bolted to it.

WHY THEY ARE DRAWN WITH STROKES. A 1.8px stroke on a 24px grid stays legible
when it is scaled down to 14px in a toolbar and still reads at 32px in a
gallery, where a filled shape turns to mush at the small end. The set is also
deliberately geometric - arcs, chevrons and rectangles - so the icons look like
each other, which matters more to a coherent interface than any one of them being
clever.

TWO CONSUMERS, TWO FORMATS. Qt widgets want a QIcon, so `icon()` renders the SVG
into a pixmap at the right device pixel ratio and caches it. Qt stylesheets
cannot take an inline SVG at all - `image:` wants a file path - so
`icon_path()` writes the recoloured SVG into a cache directory once and hands
back the path, which is how the combo chevron and the checkbox tick are real
icons rather than an approximation made of borders.
"""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QRectF, QSize, Qt
from PyQt6.QtGui import QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer

from huaxin.ui import tokens

__all__ = ["ICON_NAMES", "svg_source", "svg_bytes", "icon", "pixmap", "icon_path", "clear_cache"]

#: Every icon, as the body of a 24x24 viewBox. `{stroke}` is the line colour and
#: `{fill}` the solid colour; both are substituted by `svg_source`.
#:
#: Kept alphabetical: the set is long enough that finding one by eye matters.
_ICONS: dict[str, str] = {
    # --- navigation and window ------------------------------------------
    "check": '<path d="M5 12.5 L10 17.5 L19 7" fill="none" stroke="{stroke}" '
             'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>',
    "chevron-down": '<path d="M6 9.5 L12 15.5 L18 9.5" fill="none" stroke="{stroke}" '
                    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
    "chevron-right": '<path d="M9.5 6 L15.5 12 L9.5 18" fill="none" stroke="{stroke}" '
                     'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
    "close": '<path d="M6 6 L18 18 M18 6 L6 18" fill="none" stroke="{stroke}" '
             'stroke-width="2" stroke-linecap="round"/>',
    "minimise": '<path d="M6 12 H18" fill="none" stroke="{stroke}" stroke-width="2" '
                'stroke-linecap="round"/>',
    "maximise": '<rect x="6" y="6" width="12" height="12" rx="1.5" fill="none" '
                'stroke="{stroke}" stroke-width="2"/>',
    "restore": '<rect x="5" y="8" width="11" height="11" rx="1.5" fill="none" '
               'stroke="{stroke}" stroke-width="2"/>'
               '<path d="M9 8 V5.5 A1.5 1.5 0 0 1 10.5 4 H18.5 A1.5 1.5 0 0 1 20 5.5 '
               'V13.5 A1.5 1.5 0 0 1 18.5 15 H16" fill="none" stroke="{stroke}" '
               'stroke-width="2"/>',
    "menu": '<path d="M4 7 H20 M4 12 H20 M4 17 H20" fill="none" stroke="{stroke}" '
            'stroke-width="2" stroke-linecap="round"/>',
    "search": '<circle cx="11" cy="11" r="6" fill="none" stroke="{stroke}" '
              'stroke-width="2"/><path d="M15.5 15.5 L20 20" fill="none" '
              'stroke="{stroke}" stroke-width="2" stroke-linecap="round"/>',

    # --- panels ----------------------------------------------------------
    "panel-left": '<rect x="3.5" y="5" width="17" height="14" rx="2" fill="none" '
                  'stroke="{stroke}" stroke-width="1.8"/>'
                  '<path d="M10 5 V19" stroke="{stroke}" stroke-width="1.8"/>',
    "panel-bottom": '<rect x="3.5" y="5" width="17" height="14" rx="2" fill="none" '
                    'stroke="{stroke}" stroke-width="1.8"/>'
                    '<path d="M3.5 14.5 H20.5" stroke="{stroke}" stroke-width="1.8"/>',
    "settings": '<circle cx="12" cy="12" r="3" fill="none" stroke="{stroke}" '
                'stroke-width="1.8"/>'
                '<path d="M12 3.5 V6 M12 18 V20.5 M3.5 12 H6 M18 12 H20.5 '
                'M6 6 L7.8 7.8 M16.2 16.2 L18 18 M18 6 L16.2 7.8 '
                'M7.8 16.2 L6 18" fill="none" stroke="{stroke}" stroke-width="1.8" '
                'stroke-linecap="round"/>',
    "filter": '<path d="M4 6 H20 L14 13 V19 L10 17 V13 Z" fill="none" stroke="{stroke}" '
              'stroke-width="1.8" stroke-linejoin="round"/>',

    # --- file and data ---------------------------------------------------
    "folder": '<path d="M3.5 7 A1.5 1.5 0 0 1 5 5.5 H9.5 L11.5 8 H19 A1.5 1.5 0 0 1 '
              '20.5 9.5 V17 A1.5 1.5 0 0 1 19 18.5 H5 A1.5 1.5 0 0 1 3.5 17 Z" '
              'fill="none" stroke="{stroke}" stroke-width="1.8" stroke-linejoin="round"/>',
    "save": '<path d="M5 4.5 H15.5 L19.5 8.5 V18 A1.5 1.5 0 0 1 18 19.5 H6 A1.5 1.5 '
            '0 0 1 4.5 18 V6 A1.5 1.5 0 0 1 6 4.5 Z" fill="none" stroke="{stroke}" '
            'stroke-width="1.8" stroke-linejoin="round"/>'
            '<rect x="8" y="13" width="8" height="6.5" fill="none" stroke="{stroke}" '
            'stroke-width="1.8"/>',
    "file": '<path d="M6 4.5 H14 L18 9 V19.5 H6 Z" fill="none" stroke="{stroke}" '
            'stroke-width="1.8" stroke-linejoin="round"/>'
            '<path d="M14 4.5 V9 H18" fill="none" stroke="{stroke}" stroke-width="1.8"/>',
    "copy": '<rect x="8.5" y="8.5" width="11" height="11" rx="1.5" fill="none" '
            'stroke="{stroke}" stroke-width="1.8"/>'
            '<path d="M15.5 6.5 V5.5 A1.5 1.5 0 0 0 14 4 H5.5 A1.5 1.5 0 0 0 4 5.5 '
            'V14 A1.5 1.5 0 0 0 5.5 15.5 H6.5" fill="none" stroke="{stroke}" '
            'stroke-width="1.8"/>',
    "download": '<path d="M12 4 V15 M7.5 10.5 L12 15 L16.5 10.5" fill="none" '
                'stroke="{stroke}" stroke-width="2" stroke-linecap="round" '
                'stroke-linejoin="round"/>'
                '<path d="M5 18.5 H19" fill="none" stroke="{stroke}" stroke-width="2" '
                'stroke-linecap="round"/>',
    "upload": '<path d="M12 15 V4 M7.5 8.5 L12 4 L16.5 8.5" fill="none" '
              'stroke="{stroke}" stroke-width="2" stroke-linecap="round" '
              'stroke-linejoin="round"/>'
              '<path d="M5 18.5 H19" fill="none" stroke="{stroke}" stroke-width="2" '
              'stroke-linecap="round"/>',

    # --- device operations -----------------------------------------------
    "refresh": '<path d="M20 12 A8 8 0 1 1 17.2 6.1" fill="none" stroke="{stroke}" '
               'stroke-width="2" stroke-linecap="round"/>'
               '<path d="M17.5 2.5 V7 H13" fill="none" stroke="{stroke}" '
               'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
    "flash": '<path d="M13 2.5 L5.5 13.5 H11 L10 21.5 L18.5 10 H12.5 Z" fill="none" '
             'stroke="{stroke}" stroke-width="1.8" stroke-linejoin="round"/>',
    "read": '<path d="M4 6.5 H20 V17.5 H4 Z" fill="none" stroke="{stroke}" '
            'stroke-width="1.8" stroke-linejoin="round"/>'
            '<path d="M7 10 H17 M7 13.5 H13" stroke="{stroke}" stroke-width="1.8" '
            'stroke-linecap="round"/>',
    "erase": '<path d="M8 5 H16 M5 8 H19 M6.5 8 L7.5 19.5 H16.5 L17.5 8" fill="none" '
             'stroke="{stroke}" stroke-width="1.8" stroke-linecap="round" '
             'stroke-linejoin="round"/>'
             '<path d="M10.5 11.5 V16.5 M13.5 11.5 V16.5" stroke="{stroke}" '
             'stroke-width="1.8" stroke-linecap="round"/>',
    "power": '<path d="M12 4 V11" fill="none" stroke="{stroke}" stroke-width="2" '
             'stroke-linecap="round"/>'
             '<path d="M7.5 7 A7 7 0 1 0 16.5 7" fill="none" stroke="{stroke}" '
             'stroke-width="2" stroke-linecap="round"/>',
    "usb": '<path d="M12 20 V7" fill="none" stroke="{stroke}" stroke-width="1.8" '
           'stroke-linecap="round"/>'
           '<circle cx="12" cy="5" r="2" fill="none" stroke="{stroke}" stroke-width="1.8"/>'
           '<path d="M12 13 L8 10 V8 M12 16 L16 13 V10.5" fill="none" stroke="{stroke}" '
           'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    "chip": '<rect x="6.5" y="6.5" width="11" height="11" rx="1.5" fill="none" '
            'stroke="{stroke}" stroke-width="1.8"/>'
            '<path d="M10 3.5 V6.5 M14 3.5 V6.5 M10 17.5 V20.5 M14 17.5 V20.5 '
            'M3.5 10 H6.5 M3.5 14 H6.5 M17.5 10 H20.5 M17.5 14 H20.5" '
            'stroke="{stroke}" stroke-width="1.8" stroke-linecap="round"/>',
    "phone": '<rect x="7" y="3" width="10" height="18" rx="2.2" fill="none" '
             'stroke="{stroke}" stroke-width="1.8"/>'
             '<path d="M10.5 5.5 H13.5" stroke="{stroke}" stroke-width="1.8" '
             'stroke-linecap="round"/>'
             '<circle cx="12" cy="18" r="1" fill="{fill}"/>',

    # --- status ----------------------------------------------------------
    "info": '<circle cx="12" cy="12" r="8.5" fill="none" stroke="{stroke}" '
            'stroke-width="1.8"/>'
            '<path d="M12 11 V16.5" stroke="{stroke}" stroke-width="2" '
            'stroke-linecap="round"/>'
            '<circle cx="12" cy="7.8" r="1.1" fill="{fill}"/>',
    "warning": '<path d="M12 4 L21 19.5 H3 Z" fill="none" stroke="{stroke}" '
               'stroke-width="1.8" stroke-linejoin="round"/>'
               '<path d="M12 9.5 V14" stroke="{stroke}" stroke-width="1.8" '
               'stroke-linecap="round"/>'
               '<circle cx="12" cy="16.8" r="1" fill="{fill}"/>',
    "error": '<circle cx="12" cy="12" r="8.5" fill="none" stroke="{stroke}" '
             'stroke-width="1.8"/>'
             '<path d="M9 9 L15 15 M15 9 L9 15" stroke="{stroke}" stroke-width="1.8" '
             'stroke-linecap="round"/>',
    "success": '<circle cx="12" cy="12" r="8.5" fill="none" stroke="{stroke}" '
               'stroke-width="1.8"/>'
               '<path d="M8.2 12.3 L10.8 15 L15.8 9.3" fill="none" stroke="{stroke}" '
               'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    "dot": '<circle cx="12" cy="12" r="4.5" fill="{fill}"/>',
    "dot-ring": '<circle cx="12" cy="12" r="8" fill="none" stroke="{stroke}" '
                'stroke-width="2"/><circle cx="12" cy="12" r="3" fill="{fill}"/>',

    # --- play / control ---------------------------------------------------
    "play": '<path d="M8 5.5 L18 12 L8 18.5 Z" fill="none" stroke="{stroke}" '
            'stroke-width="1.8" stroke-linejoin="round"/>',
    "stop": '<rect x="6.5" y="6.5" width="11" height="11" rx="1.5" fill="none" '
            'stroke="{stroke}" stroke-width="1.8"/>',
    "external": '<path d="M13 5 H19 V11" fill="none" stroke="{stroke}" '
                'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>'
                '<path d="M19 5 L11 13" fill="none" stroke="{stroke}" stroke-width="1.8" '
                'stroke-linecap="round"/>'
                '<path d="M17 14 V18 A1.5 1.5 0 0 1 15.5 19.5 H6 A1.5 1.5 0 0 1 4.5 18 '
                'V8.5 A1.5 1.5 0 0 1 6 7 H10" fill="none" stroke="{stroke}" '
                'stroke-width="1.8" stroke-linecap="round"/>',
}

ICON_NAMES: tuple[str, ...] = tuple(sorted(_ICONS))

_SVG_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">'
    "{body}</svg>"
)


@dataclass(frozen=True)
class _Key:
    """Cache key. Frozen so it can index a dict, which a tuple would not say."""

    name: str
    stroke: str
    fill: str


def svg_source(name: str, colour: str | None = None) -> str:
    """The SVG source for an icon, with its colours substituted.

    `colour` defaults to the active theme's body text, so an icon drawn without
    a colour follows the theme rather than being stuck at whatever it was first
    rendered as.
    """
    try:
        body = _ICONS[name]
    except KeyError as exc:
        raise KeyError(f"no such icon: {name!r} (have {len(_ICONS)} icons)") from exc
    stroke = colour or tokens.active_theme().text
    return _SVG_TEMPLATE.format(body=body.replace("{stroke}", stroke).replace("{fill}", stroke))


def svg_bytes(name: str, colour: str | None = None) -> bytes:
    """The same source, encoded. QSvgRenderer takes bytes, not str."""
    return svg_source(name, colour).encode("utf-8")


# -----------------------------------------------------------------------------
#  Widget icons
# -----------------------------------------------------------------------------

_pixmap_cache: dict[tuple[_Key, int, int], QPixmap] = {}


def pixmap(name: str, colour: str | None = None, size: int = 16, ratio: int = 2) -> QPixmap:
    """An icon rendered to a pixmap at `ratio` times `size` for a sharp HiDPI result.

    Rendering at 1x and letting Qt scale it up is what makes icons look soft on a
    150% display, which is the single most common way a UI looks cheap on a
    modern laptop.
    """
    stroke = colour or tokens.active_theme().text
    key = (_Key(name, stroke, stroke), size, ratio)
    cached = _pixmap_cache.get(key)
    if cached is not None:
        return cached

    renderer = QSvgRenderer(svg_bytes(name, stroke))
    canvas = QPixmap(size * ratio, size * ratio)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(painter, QRectF(0, 0, size * ratio, size * ratio))
    painter.end()
    canvas.setDevicePixelRatio(ratio)

    _pixmap_cache[key] = canvas
    return canvas


def icon(name: str, colour: str | None = None, size: int = 16) -> QIcon:
    """A QIcon for a widget.

    Both 1x and 2x pixmaps go into the icon, so Qt picks the right one for the
    screen it is drawn on rather than scaling a single size.
    """
    result = QIcon()
    for ratio in (1, 2):
        result.addPixmap(pixmap(name, colour, size, ratio))
    return result


# -----------------------------------------------------------------------------
#  Stylesheet icons
# -----------------------------------------------------------------------------

#: Where `icon_path()` writes its files. A temp directory rather than the source
#: tree: these are derived artifacts, regenerated on demand, and writing them
#: next to the code would leave litter in a checkout.
_cache_dir: Path | None = None


def _cache_root() -> Path:
    global _cache_dir
    if _cache_dir is None:
        _cache_dir = Path(tempfile.gettempdir()) / "huaxin-ui-icons"
        _cache_dir.mkdir(parents=True, exist_ok=True)
    return _cache_dir


def icon_path(name: str, colour: str | None = None) -> str:
    """Writes an icon to a file and returns its path, for use in a stylesheet.

    Qt's stylesheet `image:` property takes a URL, so a recoloured icon has to
    exist as a file before the sheet can reference it. The file name carries a
    hash of its contents, so a theme change writes a new file rather than
    leaving a stale one at the same path - and the old one is simply orphaned in
    a temp directory, which is what temp directories are for.

    The path is returned with forward slashes because Qt parses a stylesheet
    URL with a backslash on Windows as an escape, not a separator.
    """
    stroke = colour or tokens.active_theme().text
    source = svg_bytes(name, stroke)
    digest = hashlib.sha256(source).hexdigest()[:12]
    target = _cache_root() / f"{name}-{digest}.svg"
    if not target.exists():
        target.write_bytes(source)
    return target.as_posix()


def clear_cache() -> None:
    """Empties both caches. Called when the theme changes."""
    _pixmap_cache.clear()
