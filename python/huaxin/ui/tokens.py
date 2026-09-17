"""Design tokens: the single source of truth for how the tool looks.

WHY TOKENS AND NOT A STYLESHEET FULL OF HEX CODES. A colour appears in a dozen
places - a button's gradient, its hover state, the focus ring on the input next
to it, the accent bar on the panel above, the log line that reports what it did.
Change the accent in one place and every one of those has to move together, or
the interface drifts into a patchwork the first time somebody edits a single
rule.

So no colour is written down twice. The stylesheet is a template full of
`@token` placeholders, the tokens live here, and `style.py` joins them. A test
asserts that no placeholder survives into the rendered sheet and that every
foreground/background pair the interface actually uses meets a contrast floor -
which is how the palette below got its values, rather than by eye.

WHY THREE THEMES. `midnight` is the default and what the tool ships with.
`graphite` is the same layout in neutral greys, for operators who find a blue
interface tiring over a long shift. `daylight` is a light theme, and it exists
because a dark room is not the only place this runs: a workshop bench with a
window behind it is a bright environment, and a dark UI there is genuinely hard
to read. All three are checked for contrast, so none of them is the "good" theme
with two afterthoughts beside it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Iterator

__all__ = [
    "Theme",
    "Metrics",
    "THEMES",
    "METRICS",
    "DEFAULT_THEME",
    "active_theme",
    "set_active_theme",
    "theme_names",
    "tokens",
    "hex_to_rgb",
    "relative_luminance",
    "contrast_ratio",
    "rgba",
    "mix",
]


@dataclass(frozen=True)
class Theme:
    """One colour scheme.

    Token names are semantic rather than literal: `surface_raised` survives a
    change of palette, `grey_700` does not. The two exceptions are `accent_soft`
    and the log colours, which are named for what they are used for because that
    is the only thing that matters about them.
    """

    name: str
    label: str
    dark: bool

    # --- surfaces, from furthest back to furthest forward --------------------
    #: The window behind everything.
    bg: str
    #: A region inside the window: a dock, a toolbar, a status bar.
    bg_alt: str
    #: A panel or card.
    surface: str
    #: A panel inside a panel - a header strip, a table's alternate row.
    surface_alt: str
    #: Something raised off the surface: a button, a chip, a hovered row.
    surface_raised: str
    #: A raised surface under the pointer.
    surface_hover: str

    # --- lines ---------------------------------------------------------------
    border: str
    #: A border that needs to be noticed: a focused input, a header rule.
    border_strong: str
    #: A border tinted with the accent, without being a full accent fill.
    border_accent: str

    # --- text ----------------------------------------------------------------
    #: Body text. The highest-contrast colour in the scheme.
    text: str
    #: Labels, captions, anything supporting the text above it.
    text_muted: str
    #: Text on a disabled control. Deliberately low contrast: it must read as
    #: unavailable, not as faintly available.
    text_disabled: str
    #: Text drawn on top of an accent fill.
    text_on_accent: str

    # --- the accent, and its states -----------------------------------------
    accent: str
    #: The top and bottom of the gradient used on filled accent surfaces. A
    #: gradient rather than a flat fill: a large flat block of saturated colour
    #: looks painted on, and a 6% vertical shift reads as a physical surface
    #: without anyone noticing why.
    accent_gradient_top: str
    accent_gradient_bottom: str
    accent_hover: str
    accent_press: str
    #: An accent-tinted translucent fill, for selection and focus rings.
    accent_soft: str

    # --- semantic states -----------------------------------------------------
    ok: str
    ok_soft: str
    warn: str
    warn_soft: str
    error: str
    error_soft: str

    # --- log levels ----------------------------------------------------------
    #: Our own informational lines.
    log_info: str
    #: Trace-level detail.
    log_debug: str
    #: Raw stdout/stderr of a tool we ran. A different hue from our own messages
    #: on purpose: the operator needs to see at a glance which lines came from
    #: adb or fastboot and which came from the application.
    log_output: str

    # --- effects -------------------------------------------------------------
    #: The colour of the drop shadow under raised surfaces.
    shadow: str
    #: The dim laid over the window behind a modal dialog.
    scrim: str

    def colour(self, token: str) -> str:
        """One token by name. Raises on an unknown name rather than returning black."""
        try:
            return getattr(self, token)
        except AttributeError as exc:
            raise KeyError(f"no such theme token: {token!r}") from exc

    def contrasted_with(self) -> Iterator[tuple[str, str]]:
        """The (foreground, background) pairs this theme is expected to satisfy.

        Used by the contrast test. Listing them here rather than in the test
        means a new surface is one line away from being checked, and the list
        documents which combinations the interface actually puts together.
        """
        yield ("text", "bg")
        yield ("text", "surface")
        yield ("text", "surface_alt")
        yield ("text", "surface_raised")
        yield ("text_muted", "bg")
        yield ("text_muted", "surface")
        yield ("text_on_accent", "accent")
        yield ("text_on_accent", "accent_press")
        yield ("ok", "surface")
        yield ("warn", "surface")
        yield ("error", "surface")
        yield ("log_info", "bg")
        yield ("log_debug", "bg")
        yield ("log_output", "bg")


@dataclass(frozen=True)
class Metrics:
    """Every dimension the interface uses, in logical pixels.

    Separate from the colours because geometry does not change when the palette
    does. Kept as tokens for the same reason the colours are: a corner radius
    that is 5px in one widget and 6px in the next is exactly the kind of detail
    that makes an interface look unfinished without anyone being able to say why.
    """

    # --- corners -------------------------------------------------------------
    radius_small: int = 4
    radius: int = 6
    radius_large: int = 10
    #: Fully rounded, for status pills. Large enough to round any control it is
    #: applied to, since Qt clamps the radius to half the height.
    radius_pill: int = 999

    # --- spacing -------------------------------------------------------------
    gap_xs: int = 4
    gap_sm: int = 8
    gap: int = 12
    gap_lg: int = 18
    gap_xl: int = 26

    #: Padding inside a panel or card.
    pad: int = 14
    pad_sm: int = 9

    # --- type ----------------------------------------------------------------
    font_family: str = "'Segoe UI', 'Inter', 'Noto Sans', 'DejaVu Sans', sans-serif"
    font_mono: str = "'Cascadia Mono', 'Consolas', 'JetBrains Mono', 'DejaVu Sans Mono', monospace"
    font_size_title: int = 15
    font_size_heading: int = 14
    font_size_body: int = 13
    font_size_small: int = 12
    font_size_micro: int = 11
    font_size_log: int = 12

    # --- controls ------------------------------------------------------------
    control_height: int = 32
    control_height_sm: int = 26
    control_height_lg: int = 38
    icon_size: int = 16
    icon_size_lg: int = 20
    scrollbar: int = 11
    titlebar_height: int = 38
    progress_height: int = 8

    # --- window --------------------------------------------------------------
    window_min_width: int = 1024
    window_min_height: int = 640
    sidebar_min_width: int = 380
    console_min_height: int = 140


# -----------------------------------------------------------------------------
#  The palettes
#
#  Every value here was chosen against the contrast floor the test enforces,
#  not by eye. Where a colour had to move to pass, it moved - see
#  tests/test_ui_design.py, which reports the measured ratio for each pair.
# -----------------------------------------------------------------------------

MIDNIGHT = Theme(
    name="midnight",
    label="Midnight",
    dark=True,
    bg="#1e1e2e",
    bg_alt="#181825",
    surface="#252536",
    surface_alt="#2d2d40",
    surface_raised="#33334a",
    surface_hover="#3d3d57",
    border="#3a3a52",
    border_strong="#4c4c69",
    border_accent="#5b9bf8",
    text="#e6e8f0",
    text_muted="#a6adc8",
    text_disabled="#5c6180",
    text_on_accent="#ffffff",
    accent="#2563eb",
    accent_gradient_top="#2f6cea",
    accent_gradient_bottom="#1f56c9",
    # The hover fill is only slightly lighter than the base, and that is a
    # deliberate compromise: on a dark theme, a fill light enough to be a
    # "brightening" is also light enough to drop white text below 4.5:1. What
    # actually reads as hover is the brighter border and the glow, so the fill
    # moves a little and the edges do the work.
    accent_hover="#2f6ee8",
    accent_press="#1d4ed8",
    accent_soft="rgba(37, 99, 235, 0.20)",
    ok="#5fd68a",
    ok_soft="rgba(95, 214, 138, 0.15)",
    warn="#f0b849",
    warn_soft="rgba(240, 184, 73, 0.15)",
    error="#ff6b6b",
    error_soft="rgba(255, 107, 107, 0.15)",
    log_info="#d5d9e6",
    log_debug="#8fa0c4",
    log_output="#7fd3e8",
    shadow="rgba(0, 0, 0, 0.45)",
    scrim="rgba(10, 10, 18, 0.62)",
)

GRAPHITE = Theme(
    name="graphite",
    label="Graphite",
    dark=True,
    bg="#1f1f1f",
    bg_alt="#181818",
    surface="#262626",
    surface_alt="#303030",
    surface_raised="#383838",
    surface_hover="#454545",
    border="#3d3d3d",
    border_strong="#505050",
    border_accent="#8ab4f8",
    text="#e8e8e8",
    text_muted="#b0b0b0",
    text_disabled="#636363",
    text_on_accent="#111111",
    accent="#8ab4f8",
    accent_gradient_top="#98bdf9",
    accent_gradient_bottom="#78a6f2",
    accent_hover="#a4c6fa",
    accent_press="#6b96dd",
    accent_soft="rgba(138, 180, 248, 0.16)",
    ok="#7bd88f",
    ok_soft="rgba(123, 216, 143, 0.15)",
    warn="#ffb86b",
    warn_soft="rgba(255, 184, 107, 0.15)",
    error="#ff7b72",
    error_soft="rgba(255, 123, 114, 0.15)",
    log_info="#dcdcdc",
    log_debug="#9aa0a6",
    log_output="#8ab4f8",
    shadow="rgba(0, 0, 0, 0.5)",
    scrim="rgba(8, 8, 8, 0.66)",
)

DAYLIGHT = Theme(
    name="daylight",
    label="Daylight",
    dark=False,
    bg="#f4f5f7",
    bg_alt="#e9ebef",
    surface="#ffffff",
    surface_alt="#f7f8fa",
    surface_raised="#eef0f4",
    surface_hover="#e3e6ec",
    border="#d3d7de",
    border_strong="#b4bac4",
    border_accent="#1a6fd4",
    text="#1a1d23",
    text_muted="#5a616e",
    text_disabled="#a2a8b3",
    text_on_accent="#ffffff",
    accent="#1a6fd4",
    accent_gradient_top="#2b7de0",
    accent_gradient_bottom="#1663c0",
    accent_hover="#2b7de0",
    accent_press="#12539f",
    accent_soft="rgba(26, 111, 212, 0.12)",
    ok="#1c853a",
    ok_soft="rgba(30, 142, 62, 0.12)",
    warn="#9a6200",
    warn_soft="rgba(154, 98, 0, 0.12)",
    error="#c5221f",
    error_soft="rgba(197, 34, 31, 0.12)",
    log_info="#2b3038",
    log_debug="#676e7c",
    log_output="#0b6e8f",
    shadow="rgba(15, 23, 42, 0.16)",
    scrim="rgba(24, 28, 36, 0.32)",
)

METRICS = Metrics()

THEMES: dict[str, Theme] = {theme.name: theme for theme in (MIDNIGHT, GRAPHITE, DAYLIGHT)}

DEFAULT_THEME = MIDNIGHT.name

_active: Theme = MIDNIGHT


def theme_names() -> list[str]:
    """Every theme, default first, so a chooser lists them in a stable order."""
    return [DEFAULT_THEME] + [name for name in THEMES if name != DEFAULT_THEME]


def active_theme() -> Theme:
    return _active


def set_active_theme(name: str) -> Theme:
    """Switches the active theme. Raises on an unknown name.

    A silent fall back to the default would be worse than an error here: the
    only callers are the theme chooser and the settings loader, and both would
    rather know they asked for something that does not exist.
    """
    global _active
    if name not in THEMES:
        raise KeyError(f"no such theme: {name!r} (have {sorted(THEMES)})")
    _active = THEMES[name]
    return _active


def tokens(theme: Theme | None = None) -> dict[str, str]:
    """Every token as a flat string map, for stylesheet substitution.

    Only strings: the substitution is textual, and a stray int would silently
    render as one - `border-radius: 6` without a unit is accepted by Qt, which
    makes the mistake hard to see.
    """
    chosen = theme or _active
    merged: dict[str, str] = {}
    for key, value in asdict(chosen).items():
        if key in ("name", "label", "dark"):
            continue
        merged[key] = str(value)
    # Metrics are all lengths, and Qt wants a unit on anything that is not an
    # integer count. Appending it here rather than in the stylesheet means the
    # sheet reads as `border-radius: @radius` instead of `@radius px`, and a
    # length token can never be substituted somewhere a bare number belongs.
    for key, value in asdict(METRICS).items():
        merged[key] = f"{value}px"
    return merged


# -----------------------------------------------------------------------------
#  Colour maths
#
#  Used by the contrast check and by `rgba`/`mix` below. Small enough to keep in
#  the token module rather than a utility file of its own.
# -----------------------------------------------------------------------------


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    """Parses #rgb or #rrggbb. Raises on anything else."""
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError(f"not a hex colour: {value!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _channel_luminance(component: int) -> float:
    """One sRGB channel, linearised. The WCAG definition, not a gamma guess."""
    value = component / 255.0
    if value <= 0.03928:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def relative_luminance(colour: str) -> float:
    """WCAG relative luminance of a hex colour."""
    red, green, blue = hex_to_rgb(colour)
    return (
        0.2126 * _channel_luminance(red)
        + 0.7152 * _channel_luminance(green)
        + 0.0722 * _channel_luminance(blue)
    )


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG contrast ratio between two hex colours, from 1.0 to 21.0.

    The floor the interface is held to is 4.5:1 for body text (WCAG AA) and 3:1
    for large or non-text elements. The test reports the measured value for
    every pair so a colour that only just passes is visible as such.
    """
    first = relative_luminance(foreground)
    second = relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def rgba(colour: str, alpha: float) -> str:
    """A hex colour as a Qt rgba() string. Clamps alpha to 0..1."""
    red, green, blue = hex_to_rgb(colour)
    clamped = min(max(alpha, 0.0), 1.0)
    return f"rgba({red}, {green}, {blue}, {clamped:.3f})"


def mix(first: str, second: str, weight: float = 0.5) -> str:
    """Blends two hex colours. `weight` is how much of `second` to use."""
    ratio = min(max(weight, 0.0), 1.0)
    one = hex_to_rgb(first)
    two = hex_to_rgb(second)
    blended = tuple(
        int(round(one[index] * (1.0 - ratio) + two[index] * ratio)) for index in range(3)
    )
    return "#{:02x}{:02x}{:02x}".format(*blended)
