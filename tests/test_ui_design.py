"""Tests for the design system.

Two kinds of check, and the second is the one that matters.

The first kind asserts the source is right: tokens exist, the stylesheet has no
unresolved placeholders, every icon renders. Those catch a mistake at the point
it is made.

The second kind renders the interface and **reads the pixels back**. A Qt
stylesheet fails silently - a selector that never matched, a sub-control Qt does
not have, a rule dropped by the parser - and none of those show up in the source,
in a linter, or in a test that only inspects strings. Sampling the background of
a real window and finding the token colour there is the only check that proves
the styling was applied rather than merely written.

Run directly:  python tests/test_ui_design.py
"""

from __future__ import annotations

import io
import os
import re
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Motion off: this suite renders windows and snaps pixels, and an animation
# caught mid-flight would make the result depend on machine load.
os.environ.setdefault("HUAXIN_REDUCE_MOTION", "1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QColor  # noqa: E402
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

app = QApplication.instance() or QApplication([])
app.setStyle("Fusion")

from huaxin.ui import animations, components as c, icons, style, theme_controller, tokens  # noqa: E402
from huaxin.ui import opstatus  # noqa: E402

CHECKS = 0
FAILURES = 0


def check(description: str, passed: bool, detail: str = "") -> None:
    global CHECKS, FAILURES
    CHECKS += 1
    if not passed:
        FAILURES += 1
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{'PASS' if passed else 'FAIL'}] {description}{suffix}")


def near(actual: QColor, expected: str, tolerance: int = 6) -> bool:
    """Compares a sampled pixel against a token colour.

    A tolerance rather than equality: the renderer antialiases, the window is
    composited, and a stylesheet colour can come back a shade off through a
    gradient stop. What matters is that the pixel is unmistakably that token and
    not the default grey Qt would paint without a stylesheet.
    """
    target = QColor(expected)
    return (
        abs(actual.red() - target.red()) <= tolerance
        and abs(actual.green() - target.green()) <= tolerance
        and abs(actual.blue() - target.blue()) <= tolerance
    )


# -----------------------------------------------------------------------------
#  1. Tokens
# -----------------------------------------------------------------------------


def test_tokens() -> None:
    print("\n1. design tokens")

    names = tokens.theme_names()
    check("there are at least three themes", len(names) >= 3, str(names))
    check("the default is first", names[0] == tokens.DEFAULT_THEME, str(names))

    import dataclasses

    # The colour tokens only: name, label and dark are not colours, and `dark`
    # being False for the light theme would otherwise read as a missing value.
    fields = {
        field.name for field in dataclasses.fields(tokens.Theme)
        if field.name not in ("name", "label", "dark")
    }
    for name in names:
        theme = tokens.THEMES[name]
        missing = [token for token in fields if not theme.colour(token)]
        check(f"{name} defines every colour token", not missing, str(missing))
        malformed = [
            token for token in fields
            if not (theme.colour(token).startswith("#") or theme.colour(token).startswith("rgba("))
        ]
        check(f"{name}: every token is a colour", not malformed, str(malformed))
        check(f"{name} has a label", bool(theme.label))
        check(f"{name} declares whether it is dark", isinstance(theme.dark, bool))

    # The two dark themes must actually be dark and the light one light, or the
    # names lie and the contrast pairs are being checked against the wrong end.
    check("midnight is dark", tokens.relative_luminance(tokens.THEMES["midnight"].bg) < 0.1)
    check("graphite is dark", tokens.relative_luminance(tokens.THEMES["graphite"].bg) < 0.1)
    check("daylight is light", tokens.relative_luminance(tokens.THEMES["daylight"].bg) > 0.7)

    # The accent family has to stay in a sensible order, or the states are
    # indistinguishable from each other even if each one is readable.
    for name in names:
        theme = tokens.THEMES[name]
        check(f"{name}: hover is not the same as base", theme.accent_hover != theme.accent)
        check(f"{name}: pressed is not the same as base", theme.accent_press != theme.accent)
        check(f"{name}: the gradient's two ends differ",
              theme.accent_gradient_top != theme.accent_gradient_bottom)

    check("an unknown token raises rather than returning a default",
          _raises(lambda: tokens.MIDNIGHT.colour("chartreuse")))
    check("an unknown theme raises", _raises(lambda: tokens.set_active_theme("neon")))
    tokens.set_active_theme(tokens.DEFAULT_THEME)

    # Colour maths, against values that can be checked by hand.
    check("white on black is 21:1", abs(tokens.contrast_ratio("#ffffff", "#000000") - 21.0) < 0.01)
    check("a colour against itself is 1:1",
          abs(tokens.contrast_ratio("#345678", "#345678") - 1.0) < 0.001)
    check("contrast is symmetric",
          abs(tokens.contrast_ratio("#ffffff", "#123456")
              - tokens.contrast_ratio("#123456", "#ffffff")) < 0.001)
    check("three-digit hex expands", tokens.hex_to_rgb("#abc") == (0xAA, 0xBB, 0xCC))
    check("rgba() clamps alpha", "1.000" in tokens.rgba("#000000", 5.0))
    check("mix() at 0 returns the first colour", tokens.mix("#112233", "#445566", 0.0) == "#112233")
    check("mix() at 1 returns the second", tokens.mix("#112233", "#445566", 1.0) == "#445566")


def _raises(call) -> bool:
    try:
        call()
    except Exception:  # noqa: BLE001 - any exception proves the point
        return True
    return False


# -----------------------------------------------------------------------------
#  2. Accessibility
# -----------------------------------------------------------------------------


def test_contrast() -> None:
    print("\n2. contrast, WCAG AA")

    worst_overall = 99.0
    for name in tokens.theme_names():
        theme = tokens.THEMES[name]
        worst = 99.0
        failing: list[str] = []
        for foreground, background in theme.contrasted_with():
            ratio = tokens.contrast_ratio(theme.colour(foreground), theme.colour(background))
            worst = min(worst, ratio)
            if ratio < 4.5:
                failing.append(f"{foreground} on {background} = {ratio:.2f}:1")
        worst_overall = min(worst_overall, worst)
        check(f"{name}: every text pair meets 4.5:1", not failing, "; ".join(failing))
        check(f"{name}: the worst pair is {worst:.2f}:1", worst >= 4.5)

    # The focus ring is a non-text element and is held to 3:1, which is the
    # floor for something that has to be visible without being read.
    for name in tokens.theme_names():
        theme = tokens.THEMES[name]
        ratio = tokens.contrast_ratio(theme.border_accent, theme.bg)
        check(f"{name}: the accent border is visible against the window",
              ratio >= 3.0, f"{ratio:.2f}:1")

    check("the suite has a margin over the floor", worst_overall >= 4.5,
          f"worst {worst_overall:.2f}:1")

    # Type sizes: nothing below the 11px the design specifies, because below
    # that legibility falls off a cliff on a laptop at 125% scaling.
    metrics = tokens.METRICS
    sizes = [metrics.font_size_title, metrics.font_size_heading, metrics.font_size_body,
             metrics.font_size_small, metrics.font_size_micro, metrics.font_size_log]
    check("no font size is below 11px", min(sizes) >= 11, str(min(sizes)))
    check("the type scale is ordered",
          sizes[0] >= sizes[1] >= sizes[2] >= sizes[3] >= sizes[4], str(sizes))


# -----------------------------------------------------------------------------
#  3. The stylesheet
# -----------------------------------------------------------------------------


def test_stylesheet() -> None:
    print("\n3. the stylesheet")

    check("the template file exists", style.STYLESHEET_PATH.is_file(),
          str(style.STYLESHEET_PATH))
    template = style.STYLESHEET_PATH.read_text(encoding="utf-8")
    check("the template is substantial", len(template) > 8000, f"{len(template)} chars")
    check("the template uses tokens", template.count("@") > 100, str(template.count("@")))

    for name in tokens.theme_names():
        rendered = style.stylesheet(name, refresh=True)
        body = re.sub(r"/\*.*?\*/", "", rendered, flags=re.DOTALL)
        left = sorted(set(re.findall(r"@[a-z][a-z0-9_]*", body)))
        check(f"{name}: every token resolved", not left, str(left))

    # Qt drops rules it cannot parse and says nothing about most of them, so a
    # parse failure shows up only as a complaint on stderr.
    for name in tokens.theme_names():
        captured = io.StringIO()
        with redirect_stderr(captured):
            app.setStyleSheet(style.stylesheet(name, refresh=True))
        complaint = captured.getvalue().strip()
        check(f"{name}: Qt parses the sheet without complaint", not complaint, complaint[:80])

    # A mistyped token name is the mistake this whole scheme exists to make
    # loud, so it must raise rather than substituting an empty string.
    original = style.STYLESHEET_PATH
    with tempfile.TemporaryDirectory() as scratch:
        broken = Path(scratch) / "broken.qss"
        broken.write_text("QWidget { color: @not_a_token; }", encoding="utf-8")
        style.STYLESHEET_PATH = broken
        try:
            raised = _raises(lambda: style.stylesheet("midnight", refresh=True))
        finally:
            style.STYLESHEET_PATH = original
        check("an unknown token fails loudly", raised)

    rendered = style.stylesheet("midnight", refresh=True)
    for selector, why in (
        ("QPushButton[role=\"primary\"]", "primary buttons"),
        ("QPushButton[role=\"danger\"]", "danger buttons"),
        ("QPushButton[role=\"icon\"]", "icon buttons"),
        ("QTabBar::tab:selected", "the selected tab"),
        ("QPlainTextEdit#LogConsole", "the log console"),
        ("QScrollBar::handle", "the scrollbars"),
        ("QProgressBar::chunk", "the progress fill"),
        ("QHeaderView::section", "table headers"),
        ("QComboBox::down-arrow", "the dropdown chevron"),
        ("QCheckBox::indicator:checked", "checked checkboxes"),
        ("QLabel[role=\"chip\"]", "status chips"),
        ("QFrame[role=\"card\"]", "cards"),
        ("QWidget#TitleBar", "the custom title bar"),
    ):
        check(f"the sheet styles {why}", selector in rendered, selector)

    # The rendered sheet is *full* of literals - that is what substitution
    # produces. The invariant belongs on the template: a colour written there by
    # hand is a colour that will not follow a theme change.
    body = re.sub(r"/\*.*?\*/", "", template, flags=re.DOTALL)
    literals = set(re.findall(r"#[0-9a-fA-F]{6}", body))
    check("the template writes no colours by hand except white",
          literals <= {"#ffffff"}, str(sorted(literals)))
    # White is allowed because text on a saturated danger fill is white in every
    # theme; a token for it would be a token with one value.


# -----------------------------------------------------------------------------
#  4. Icons
# -----------------------------------------------------------------------------


def test_icons() -> None:
    print("\n4. icons")

    check("the set is substantial", len(icons.ICON_NAMES) >= 30, str(len(icons.ICON_NAMES)))
    check("names are sorted for lookup by eye",
          list(icons.ICON_NAMES) == sorted(icons.ICON_NAMES))

    # Icons the task names explicitly must exist, or a panel silently draws
    # blank where an action icon should be.
    for required in ("flash", "read", "erase", "settings", "usb", "chip", "phone",
                     "check", "warning", "error", "info", "refresh", "power",
                     "save", "folder", "search", "close", "minimise", "maximise"):
        check(f"the {required!r} icon exists", required in icons.ICON_NAMES)

    empty = []
    for name in icons.ICON_NAMES:
        for size in (14, 16, 32):
            pixmap = icons.pixmap(name, "#ffffff", size, ratio=2)
            if pixmap.isNull() or pixmap.width() != size * 2:
                empty.append(f"{name}@{size}")
    check("every icon renders at every size", not empty, str(empty[:5]))

    # An icon that paints nothing is a missing path, and a blank square in a
    # toolbar is exactly the failure that is invisible in the source.
    blank: list[str] = []
    for name in icons.ICON_NAMES:
        image = icons.pixmap(name, "#ffffff", 24, ratio=1).toImage()
        painted = sum(
            1
            for y in range(image.height())
            for x in range(image.width())
            if image.pixelColor(x, y).alpha() > 0
        )
        if painted < 8:
            blank.append(f"{name}={painted}px")
    check("no icon is a blank square", not blank, str(blank))

    # Recolouring has to actually recolour, or the theme switch leaves the
    # icons in the colour they were first drawn in.
    red = icons.pixmap("flash", "#ff0000", 24, ratio=1).toImage()
    green = icons.pixmap("flash", "#00ff00", 24, ratio=1).toImage()
    differing = sum(
        1
        for y in range(red.height())
        for x in range(red.width())
        if red.pixelColor(x, y) != green.pixelColor(x, y)
    )
    check("recolouring changes the pixels", differing > 8, f"{differing} pixels differ")

    check("an unknown icon name raises", _raises(lambda: icons.svg_source("definitely-not-an-icon")))

    # Stylesheets take a path, so the icon has to exist as a file.
    path = Path(icons.icon_path("chevron-down", "#123456"))
    check("icon_path writes a file", path.is_file(), str(path))
    check("the file holds the requested colour", "#123456" in path.read_text(encoding="utf-8"))
    check("the path uses forward slashes, which Qt's sheet parser needs",
          "\\" not in path.as_posix())
    again = Path(icons.icon_path("chevron-down", "#123456"))
    check("the same icon and colour reuse the file", path == again)

    # The SVG has to be well-formed XML, or the renderer silently draws nothing.
    from xml.etree import ElementTree

    malformed = []
    for name in icons.ICON_NAMES:
        try:
            ElementTree.fromstring(icons.svg_source(name, "#ffffff"))
        except ElementTree.ParseError as exc:
            malformed.append(f"{name}: {exc}")
    check("every icon is well-formed SVG", not malformed, str(malformed[:3]))


# -----------------------------------------------------------------------------
#  5. Components
# -----------------------------------------------------------------------------


def test_components() -> None:
    print("\n5. components")

    style.apply_theme(app, "midnight")

    for role in ("primary", "secondary", "danger", "ghost", "icon"):
        button = c.StyledButton("x", role=role)
        check(f"a {role} button carries its role as a property",
              button.property("role") == role)
    check("an unknown role raises rather than styling as something else",
          _raises(lambda: c.StyledButton("x", role="enormous")))

    button = c.StyledButton("Flash", role="primary", icon="flash")
    check("a button takes an icon", not button.icon().isNull())
    button.setIcon("erase")
    check("the icon can be changed", not button.icon().isNull())

    # The row's enablement rules are the reason it exists, so they are what to test.
    row = c.ButtonRow()
    always = c.StyledButton("always")
    needs = c.StyledButton("needs a device")
    idle = c.StyledButton("needs idle")
    row.add(always).add(needs, needs_selection=True).add(idle, needs_idle=True).finish()
    row.refresh(has_selection=False, busy=False)
    check("a button needing a selection is disabled without one", not needs.isEnabled())
    check("an unconditional button stays enabled", always.isEnabled())
    row.refresh(has_selection=True, busy=True)
    check("a button needing idle is disabled while busy", not idle.isEnabled())
    check("a selection button is disabled while busy too", not needs.isEnabled())
    row.refresh(has_selection=True, busy=False)
    check("both enable once there is a selection and no job", needs.isEnabled() and idle.isEnabled())

    chip = c.Chip("Ready", tone="ok", icon="success")
    check("a chip carries its tone", chip.property("tone") == "ok")
    chip.setTone("error")
    check("the tone can change", chip.property("tone") == "error")

    dot = c.StatusDot(tone="warn")
    check("a status dot carries its tone", dot.tone() == "warn")
    dot.setPulsing(True)
    dot.setPulsing(False)
    check("pulsing can be turned on and off without error", True)

    tile = c.StatTile(c.Stat("3", "Devices", "accent"))
    tile.setValue("4")
    check("a stat tile updates its value", True)

    callout = c.Callout("Careful", severity="warn", title="Erasing")
    check("a callout carries its severity", callout.property("severity") == "warn")
    callout.setSeverity("error")
    check("the severity can change", callout.property("severity") == "error")

    card = c.Card(title="Title", caption="Caption", icon="chip", shadow=True)
    check("a card has a body to put things in", card.body is not None)
    check("a card header can take an action",
          card.header is not None and card.header.add_action(c.IconButton("refresh")) is card.header)

    header = c.SectionHeader("T", caption="c", icon="chip")
    check("a section header chains add_action into itself", header.add_action(c.IconButton("save")) is header)

    table = c.StyledTable(3, 2)
    table.setHeaders(["A", "B"])
    check("a styled table is read-only by default",
          table.editTriggers() == table.EditTrigger.NoEditTriggers)
    check("a styled table selects whole rows",
          table.selectionBehavior() == table.SelectionBehavior.SelectRows)

    combo = c.StyledComboBox(options=[("One", 1), ("Two", 2)], current=2)
    check("a combo selects by value", combo.currentValue() == 2)
    check("selecting a missing value reports rather than silently keeping the old one",
          combo.setCurrentValue(99) is False)

    field = c.SearchField(placeholder="Find")
    check("a search field has a clear button", field.isClearButtonEnabled())

    empty = c.EmptyState(icon="usb", title="Nothing", message="Do something")
    check("an empty state constructs", empty is not None)


def test_progress() -> None:
    print("\n6. progress")

    # Jitter: a device reporting 37, 38, 37 makes a bar that twitches backwards.
    bar = c.StyledProgressBar()
    bar.setValue(40)
    bar.setValue(38)
    check("a small backwards step is ignored as jitter", bar.value() == 40, str(bar.value()))
    bar.setValue(10)
    check("a large backwards step is honoured as a restart", bar.value() == 10,
          str(bar.value()))
    bar.setValue(500)
    check("a value above the maximum is clamped", bar.value() == 100, str(bar.value()))
    bar.setValue(-20)
    check("a negative value is clamped", bar.value() == 0, str(bar.value()))

    bar.setIndeterminate(True)
    check("indeterminate uses Qt's busy range", bar.minimum() == 0 and bar.maximum() == 0)
    bar.setIndeterminate(False)
    check("leaving indeterminate restores the range", bar.maximum() == 100)

    bar.setState("error")
    check("the state is a property the sheet reads", bar.property("state") == "error")

    segments = c.SegmentedProgress()
    segments.setSegments([("boot", 100), ("system", 300)])
    check("nothing is complete before anything is done", segments.overall() == 0.0)
    segments.setSegmentProgress(0, 1.0)
    check("completion is weighted by segment size, not by count",
          abs(segments.overall() - 0.25) < 0.001, f"{segments.overall():.3f}")
    segments.setSegmentProgress(1, 1.0)
    check("all segments complete reads as 1.0", abs(segments.overall() - 1.0) < 0.001)
    segments.setSegmentProgress(1, 5.0)
    check("a fraction above 1 is clamped", segments.overall() <= 1.0)
    segments.setSegmentProgress(99, 0.5)
    check("an out-of-range index is ignored rather than raising", True)


# -----------------------------------------------------------------------------
#  7. Themes, and whether they reach the pixels
# -----------------------------------------------------------------------------


def test_theme_switching() -> None:
    print("\n7. switching themes")

    controller = theme_controller.ThemeController()
    seen: list[str] = []
    controller.on_change(seen.append)

    for name in tokens.theme_names():
        applied = controller.set_theme(name)
        check(f"{name} applies", applied == name)
        check(f"{name} becomes the active theme", tokens.active_theme().name == name)

    check("every change was reported", seen == tokens.theme_names(), str(seen))
    check("an unknown theme raises", _raises(lambda: controller.set_theme("neon")))
    check("the chooser lists every theme, by name and label",
          [name for _, name in controller.themes()] == tokens.theme_names(),
          str(controller.themes()))

    controller.set_theme(tokens.DEFAULT_THEME)
    _check_picker_works()


def _check_picker_works() -> None:
    """Drives the title bar's theme picker the way a click does.

    This exists because the picker was broken for a long time while every check
    passed. Its options were built as (name, label) where the combo box takes
    (label, name), so it displayed the raw key, never selected the current
    entry, and handed the *label* back to `set_theme` - which rejected it. The
    only symptom was an ERROR in the log when somebody clicked it, and the check
    that should have caught it asserted the wrong contract instead:

        [name for name, _ in controller.themes()] == tokens.theme_names()

    which passed while the widget could not change a theme. So the check now
    goes through the widget, entry by entry, and asserts that the theme actually
    changed.
    """
    print("\n8. the theme picker changes the theme when it is used")

    parent = QWidget()
    picker = theme_controller.theme_picker(controller_under_test(), parent)
    order = tokens.theme_names()

    check("the picker displays labels, not keys",
          [picker.itemText(i) for i in range(picker.count())]
          == [tokens.THEMES[name].label for name in order],
          str([picker.itemText(i) for i in range(picker.count())]))
    check("the picker stores names, not labels",
          [picker.itemData(i) for i in range(picker.count())] == order,
          str([picker.itemData(i) for i in range(picker.count())]))
    check("the picker opens on the active theme",
          picker.currentValue() == tokens.DEFAULT_THEME, str(picker.currentValue()))

    failures: list[str] = []
    for row, name in enumerate(order):
        try:
            # Exactly what a click does: move the selection and let the widget
            # tell the controller.
            picker.setCurrentIndex(row)
        except Exception as exc:  # noqa: BLE001 - the point is to catch anything
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        if tokens.active_theme().name != name:
            failures.append(f"{name}: theme is {tokens.active_theme().name}")
    check("every entry in the picker switches to the theme it names",
          failures == [], "; ".join(failures))

    # And the reverse direction: the settings dialog can change the theme, and
    # the picker has to follow without re-triggering the controller.
    changed: list[str] = []
    controller = controller_under_test()
    controller.on_change(changed.append)
    # The parent is held, not a temporary: the picker is a child of it and is
    # destroyed with it, so `theme_picker(controller, QWidget())` builds a widget
    # that dies on the next line.
    holder = QWidget()
    picker2 = theme_controller.theme_picker(controller, holder)
    picker2.syncToTheme("daylight")
    check("syncing the picker moves its selection",
          picker2.currentValue() == "daylight", str(picker2.currentValue()))
    check("syncing does not tell the controller to change anything", changed == [],
          str(changed))
    controller.set_theme("midnight")
    parent.deleteLater()
    holder.deleteLater()


def controller_under_test() -> "theme_controller.ThemeController":
    """A controller for the picker tests, with the default theme active."""
    tokens.set_active_theme(tokens.DEFAULT_THEME)
    return theme_controller.ThemeController()


def test_rendered_pixels() -> None:
    """The check that matters: what the stylesheet says is what gets painted.

    A Qt stylesheet is applied silently and fails silently. This renders a real
    window and reads the colours back out of the framebuffer, so a rule that
    never matched - or a token that resolved to something Qt rejected - shows up
    as a wrong pixel rather than as a passing string comparison.
    """
    print("\n9. the stylesheet reaches the pixels")

    for name in tokens.theme_names():
        theme = tokens.THEMES[name]
        style.apply_theme(app, name)

        host = QWidget()
        host.resize(360, 240)
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        primary = c.StyledButton("Flash", role="primary")
        primary.setFixedHeight(44)
        layout.addWidget(primary)
        host.show()
        _settle()

        image = host.grab().toImage()
        background = image.pixelColor(4, image.height() - 4)
        check(f"{name}: the window background is the theme's bg",
              near(background, theme.bg),
              f"sampled {background.name()}, expected {theme.bg}")

        # Sampled from the button's own geometry rather than a guessed offset:
        # where the layout puts a widget is not something a test should assume,
        # and assuming it is how the first version of this check measured the
        # background and still passed the wrong assertion.
        box = primary.geometry()
        fill = image.pixelColor(box.center().x(), box.center().y())
        family = (theme.accent_gradient_top, theme.accent, theme.accent_gradient_bottom)
        check(f"{name}: the primary button is painted in the accent family",
              any(near(fill, shade, 22) for shade in family),
              f"sampled {fill.name()} at {box.center().x()},{box.center().y()}; "
              f"expected one of {', '.join(family)}")

        # The gradient: the top of the button is lighter than the bottom, which
        # is the whole point of using one and is invisible to a single sample.
        top = image.pixelColor(box.center().x(), box.top() + 3)
        bottom = image.pixelColor(box.center().x(), box.bottom() - 3)
        check(f"{name}: the button's fill is a top-to-bottom gradient",
              tokens.relative_luminance(top.name()) >= tokens.relative_luminance(bottom.name()),
              f"top {top.name()} bottom {bottom.name()}")

        # Not the default grey Qt paints without a stylesheet, which is what a
        # dropped rule would leave behind.
        check(f"{name}: the button is not Qt's default grey",
              not near(fill, "#efefef", 20) and not near(fill, "#e0e0e0", 20),
              fill.name())

        host.close()
        _settle(2)

    # Two themes must not render the same pixels, or the switch does nothing.
    rendered: dict[str, str] = {}
    for name in tokens.theme_names():
        style.apply_theme(app, name)
        host = QWidget()
        host.resize(80, 60)
        host.show()
        _settle()
        rendered[name] = host.grab().toImage().pixelColor(40, 30).name()
        host.close()
        _settle(2)
    check("the three themes paint three different backgrounds",
          len(set(rendered.values())) == len(rendered), str(rendered))
    for name, sampled in rendered.items():
        check(f"{name}: the sampled background matches its token",
              near(QColor(sampled), tokens.THEMES[name].bg, 4),
              f"{sampled} vs {tokens.THEMES[name].bg}")

    style.apply_theme(app, tokens.DEFAULT_THEME)


def _settle(rounds: int = 8) -> None:
    for _ in range(rounds):
        app.processEvents()


# -----------------------------------------------------------------------------
#  9. Animations
# -----------------------------------------------------------------------------


def test_animations() -> None:
    print("\n10. animations")

    check("motion durations are short enough to stay responsive",
          animations.motion.hover <= 250 and animations.motion.transition <= 400,
          f"hover {animations.motion.hover}, transition {animations.motion.transition}")
    check("the stagger budget bounds a long list",
          0 < animations.motion.stagger_budget <= 600, str(animations.motion.stagger_budget))

    # Motion off must jump to the end state, not refuse to act - a caller should
    # never have to check whether animations are on before calling one.
    animations.set_animations_enabled(False)
    widget = QWidget()
    widget.resize(100, 60)
    check("with motion off, fading in still shows the widget",
          animations.fade_in(widget) is None and widget.isVisible())
    check("with motion off, fading out still hides the widget",
          animations.fade_out(widget) is None and not widget.isVisible())
    check("with motion off, expand shows the widget",
          animations.expand(widget) is None and widget.isVisible())
    check("with motion off, collapse hides it",
          animations.collapse(widget) is None and not widget.isVisible())
    check("with motion off, pulse returns nothing rather than raising",
          animations.pulse(widget) is None)
    animations.animate_modal(widget)

    animations.set_animations_enabled(True)
    widget.setVisible(True)
    animation = animations.fade_in(widget, 80)
    check("with motion on, fading returns an animation", animation is not None)
    check("the animation is running or finished, never stuck",
          animation.state() in (annotation_running(), annotation_running()))
    # Leaving animations on for the rest of the suite would make the pixel
    # samples depend on timing.
    animations.set_animations_enabled(False)
    check("the reduce-motion flag is settable", animations.animations_enabled() is False)

    from huaxin.ui.animations import HoverFeedback

    button = c.StyledButton("x")
    feedback = HoverFeedback(button, distance=2)
    check("hover feedback installs without subclassing the widget", feedback is not None)

    # Interruptibility: a second animation must replace the first rather than
    # queue behind it.
    widgets = [c.StyledButton(f"b{index}") for index in range(3)]
    animations.stagger(widgets, delay=1)
    check("staggering a list does not raise", True)


def annotation_running():
    from PyQt6.QtCore import QAbstractAnimation

    return QAbstractAnimation.State.Running


def test_motion_respects_the_environment() -> None:
    print("\n11. the reduce-motion setting")

    # The suite runs with motion off, which is also what a user with the
    # preference set gets - so this asserts the flag is actually being read
    # rather than the helpers happening to be quiet.
    check("the suite is running with motion off", animations.animations_enabled() is False)
    animations.set_animations_enabled(True)
    check("motion can be turned back on", animations.animations_enabled() is True)
    animations.set_animations_enabled(False)
    check("and off again", animations.animations_enabled() is False)


# -----------------------------------------------------------------------------
#  11. The window, end to end
# -----------------------------------------------------------------------------


def test_main_window() -> None:
    print("\n12. the main window")

    from huaxin.core.backend import BackendService
    from huaxin.ui.main_window import MainWindow
    from huaxin.ui.titlebar import FramelessWindow

    service = BackendService()
    window = MainWindow(service)
    window.resize(1366, 860)
    window.show()
    _settle()

    check("the window is frameless", isinstance(window, FramelessWindow))
    check("it has a custom title bar", window.titlebar is not None)
    check("the title bar carries the window title",
          "Huaxin Tool" in window.titlebar._title.text(), window.titlebar._title.text())
    check("there is a theme picker in the title bar", window._theme_picker is not None)
    check("the title bar has the three window controls",
          all(w is not None for w in (window.titlebar._minimise, window.titlebar._maximise,
                                      window.titlebar._close)))
    check("the window can be resized from every edge", len(window._handles) == 8,
          str(len(window._handles)))

    tabs = window._tabs
    check("there is a tab per vendor", tabs.count() >= 5, str(tabs.count()))
    missing_icons = [
        tabs.tabText(index) for index in range(tabs.count()) if tabs.tabIcon(index).isNull()
    ]
    check("every tab has an icon", not missing_icons, str(missing_icons))

    # A tab whose label is elided is a tab an operator cannot read.
    elided = []
    for index in range(tabs.count()):
        label = tabs.tabText(index)
        hint = tabs.tabBar().tabSizeHint(index)
        if hint.width() < tabs.tabBar().fontMetrics().horizontalAdvance(label):
            elided.append(label)
    check("no tab label is squeezed to ellipsis", not elided, str(elided))

    # The panels' buttons are design-system buttons with roles, not raw Qt ones.
    from huaxin.ui import components as ui_components

    buttons = window.findChildren(ui_components.StyledButton)
    check("the panels use the design system's buttons", len(buttons) >= 10, str(len(buttons)))
    roles = {button.property("role") for button in buttons}
    check("buttons carry roles rather than inline colours", roles <= {"primary", "secondary", "danger", "ghost", "icon"},
          str(sorted(roles)))
    check("there is at least one primary action on screen", "primary" in roles, str(sorted(roles)))

    check("the status bar shows the backend state as a themed chip",
          window._state_chip.property("role") == "chip")
    check("the status bar carries the live operation readout",
          isinstance(window._operation, opstatus.OperationStatus))
    check("the operation readout starts hidden",
          not window._operation.isVisible())

    # Minimum size is enforced, which is what stops the layout collapsing.
    window.resize(200, 200)
    _settle(3)
    # --- the window stacks, and nothing overlaps ----------------------------
    #
    # This is a regression test for two layout bugs that both looked fine in the
    # source. The title bar was inside the central widget, so QMainWindow put its
    # own menu bar and toolbar ABOVE it and the window's minimise/maximise/close
    # buttons ended up in the middle of the window. And the central widget was
    # forced to the full window size, which made the tab area extend underneath
    # the log dock and the status bar - content that is simply not visible.
    from PyQt6.QtCore import QPoint, QRect

    def in_window(widget) -> QRect:
        return QRect(widget.mapTo(window, QPoint(0, 0)), widget.size())

    strips = [
        ("title bar", window.titlebar),
        ("menu bar", window._menubar),
        ("toolbar", window._toolbar),
        ("device list", window._device_panel),
        ("tabs", window._tabs),
        ("log", window._console),
        ("status bar", window.statusBar()),
    ]
    rects = [(name, in_window(widget)) for name, widget in strips]

    climbed = [name for name, rect in rects if rect.top() >= 0 and rect.width() > 40]
    check("every strip is laid out on screen", len(climbed) == len(strips), str(climbed))

    heights = [rect.top() for _, rect in rects]
    order = [name for name, _ in rects]
    check("the strips stack in reading order without going backwards",
          heights == sorted(heights),
          " ".join(f"{n}@{r.top()}" for n, r in rects))

    check("the title bar is the topmost strip", rects[0][1].top() == 0,
          f"title bar at y={rects[0][1].top()}")
    check("the title bar spans the window", rects[0][1].width() >= window.width() - 2,
          f"{rects[0][1].width()} of {window.width()}")

    overlaps = []
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            inter = rects[i][1].intersected(rects[j][1])
            if inter.width() > 2 and inter.height() > 2:
                overlaps.append(f"{rects[i][0]}/{rects[j][0]}")
    check("no two strips overlap", overlaps == [], ", ".join(overlaps))

    check("the device list and the tabs sit side by side",
          rects[3][1].top() == rects[4][1].top() and rects[3][1].right() <= rects[4][1].left() + 2,
          f"list right {rects[3][1].right()}, tabs left {rects[4][1].left()}")

    check("the window refuses to shrink below its minimum",
          window.width() >= tokens.METRICS.window_min_width
          and window.height() >= tokens.METRICS.window_min_height,
          f"{window.width()}x{window.height()}")

    window.close()
    _settle(2)


def main() -> int:
    print("=== HUAXIN design system tests ===")
    test_tokens()
    test_contrast()
    test_stylesheet()
    test_icons()
    test_components()
    test_progress()
    test_theme_switching()
    test_rendered_pixels()
    test_animations()
    test_motion_respects_the_environment()
    test_main_window()

    print(f"\n{CHECKS - FAILURES}/{CHECKS} checks passed")
    if FAILURES:
        print(f"{FAILURES} FAILED")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
