# The design system

How the interface is built, and the rules that keep it consistent.

Read this before adding a widget. Adding a control that styles itself with inline
colours is a five-minute job that leaves one element that does not follow a theme
change, and finding it later takes an afternoon.

---

## The four files

| File | What it owns |
|---|---|
| `ui/tokens.py` | Every colour, radius, spacing, font size and duration. Three themes. |
| `ui/styles/huaxin.qss` | The stylesheet, as a stylesheet, with `@token` placeholders. |
| `ui/style.py` | Joins the two, sets the Qt palette, and handles the icon paths. |
| `ui/icons.py` | The SVG icon set, recoloured at render time. |
| `ui/components.py` | The widgets the interface is assembled from. |
| `ui/animations.py` | Motion, and the rules that keep it from being a nuisance. |
| `ui/theme_controller.py` | Switching themes at runtime. |
| `ui/titlebar.py` | The frameless window and its custom chrome. |
| `ui/toasts.py` | Transient notifications, stacked in the corner of a window. |
| `ui/opstatus.py` | The live operation readout for the status bar. |
| `ui/filedialog.py` | The themed file browser, and the recent-files list. |
| `ui/about.py` | The About dialog and the build facts behind it. |
| `ui/gallery.py` | Every component on one page — `python -m huaxin.ui.gallery`. |

**No colour is written down twice.** A colour appears in a button's gradient, its
hover state, the focus ring on the input beside it, the accent bar on the panel
above, and the log line reporting what it did. The stylesheet is a template of
`@token` placeholders; the values live in `tokens.py`; `style.py` joins them.

---

## Using it

```python
from huaxin.ui import components as ui

# A primary action, a destructive one, and a toolbar button.
flash = ui.StyledButton("Flash firmware", role="primary", icon="flash")
erase = ui.StyledButton("Erase", role="danger", icon="erase")
rescan = ui.IconButton("refresh", tooltip="Scan for devices")

# A container with a titled header, a callout, and a progress bar.
card = ui.Card(title="Flash", caption="Package: build/system.img", icon="flash", shadow=True)
card.add(ui.Callout("This erases userdata.", severity="warn", title="Careful"))
card.add(ui.StyledProgressBar())
```

The role is a **property**, not a colour. `QPushButton[role="primary"]` in the
stylesheet decides how it looks, so changing the palette changes every button and
switching to the light theme cannot leave one behind.

### The button roles

| Role | Use for | Colour |
|---|---|---|
| `primary` | The one action a panel exists for | Filled accent, gradient |
| `secondary` | Everything else | Outlined, accent border |
| `danger` | Anything destructive | Outlined red, fills red on hover |
| `ghost` | Quiet actions inside a panel | Transparent until hovered |
| `icon` | Toolbar and header buttons | Square, icon only |

A panel picks its own primary automatically: the first implemented, non-destructive
action is primary, everything else is secondary, and anything marked `danger` in
its `ActionSpec` is a danger button.

### Adding a tab

Start from `ui/tabkit.py`. A tab is a `VendorPanel` with a list of `ActionSpec`s;
the base class builds the header, the badge, the device box, the two-column grid
and the help note from them, so a new tab is a list of actions and some prose.

The grid fills row by row from one ordered list, which means **the order of the
list is the layout**: index 0 is top-left, 1 top-right, 2 second row left. That
is not obvious from looking at a list of actions, so `left_column()` and
`right_column()` exist to make it assertable in a test. Separate `left=` and
`right=` arguments would read better and break the moment a tab wants three
columns.

A tab that shows a package should put the display in `ui/tabviews.py` and read
the file through `huaxin.core.parsers`, not by importing the extension directly -
the panels are UI code, and giving them the native module means the next person
to add a button has to decide for themselves whether bypassing the worker thread
is acceptable. `parsers.py` has no Qt in it, so the answer is structural.

### Adding a colour or a size

Add it to `Theme` (or `Metrics`) in `tokens.py`, give it a value in all three
themes, and use `@name` in the stylesheet. A token used in the sheet but missing
from a theme is a hard error at load time with the line number — Qt would
otherwise drop the rule silently, which is how a `border-radius` goes missing and
nobody can say why.

### Adding an icon

Add its path to `_ICONS` in `icons.py` as the body of a 24x24 viewBox, using
`{stroke}` for line colour. Keep it geometric: a 1.8px stroke that reads at 14px
in a toolbar also reads at 32px in the gallery, where a filled shape turns to
mush at the small end. `tests/test_ui_design.py` renders every icon and fails if
one paints fewer than eight pixels.

---

## The rules

**A colour that is not a token is a bug.** The tests assert that the stylesheet
template writes no literal colour other than `#ffffff`, which is allowed because
text on a saturated danger fill is white in every theme.

**Contrast is checked, not judged.** Every foreground/background pair the
interface puts together is listed in `Theme.contrasted_with()` and held to WCAG
AA — 4.5:1 for body text, 3:1 for non-text. The palettes were tuned until they
passed rather than the other way round; the accent family is a saturated blue with
white text specifically because a light blue with dark text could not darken for
its pressed state without breaking the ratio. The worst pair across all three
themes is 4.70:1 and the test reports it.

**Motion is optional and short.** Every animation checks `animations_enabled()`
and jumps to its end state when motion is off, so no caller has to ask. 160 ms
for a hover, 240 ms for a transition, 300 ms for a panel resizing. Past about
400 ms an interface stops feeling responsive, and no easing curve fixes that.

**Nothing that matters is communicated by motion or by colour alone.** A status
dot is accompanied by its text; a failing log line is worded as well as coloured.
An animation is never the only sign that something finished.

**Widgets implement `apply_theme()` if they hold a themed pixmap.** A recoloured
SVG keeps whatever colour it was first drawn with, so the theme controller calls
`apply_theme()` on registered widgets after a switch. Reaching into the widget
tree looking for anything that might hold a pixmap is how a switch leaves three
icons behind.

---

## The window

`FramelessWindow` draws its own title bar. That costs the native frame, and each
thing the native frame did has to be paid back correctly or the window feels
broken: dragging with a movement threshold so a click is not a drag, double-click
to maximise, eight invisible resize handles, `startSystemMove` so Windows Snap and
multi-monitor dragging keep working, and the normal window flags so the taskbar
and Alt+Tab are unaffected.

The window is left square rather than faking Windows 11's rounded corners: a
square window with a shadow looks intentional, and a rounded one with a visible
square behind it looks like a bug.

---

## The pieces that need a rule of their own

Everything below is an addition to the design system that has behaviour attached,
not just appearance. The rules are the part worth reading before changing them.

### Notifications

`Toast` and `ToastManager`. Four rules, each of which exists because breaking it
makes the interface worse rather than merely different:

* **Never takes focus.** No `activateWindow`, no `setFocus`, `WA_ShowWithoutActivating`
  set. A flash may be running and the operator's caret must stay where it was.
* **Never stacks without limit.** Four cards, and a message that repeats under the
  same `key` updates the card already on screen and counts itself (`×3`) instead
  of adding a fifth. A cable being jiggled must not bury the interface.
* **Never disappears while it is being read.** The countdown pauses under the
  pointer and restarts when it leaves.
* **Never the only copy.** Everything a toast says also reaches the log, because
  four seconds is not a record.

A toast reports. A dialog *asks*. Anything that destroys something still gets a
dialog with a default of No.

### The operation readout

`OperationStatus` shows four things, and the fourth is the point: what is running,
how fast, how long it has been going, and **whether it has stalled**. A bar at 43%
cannot distinguish a device writing steadily from one that stopped responding
ninety seconds ago; a percentage that has not moved for 45 seconds can.

The rule about the `~`: an ETA from the device's own tracker is a fact and is
shown plain. An ETA this widget calculated from the rate of progress is an
estimate and is marked `~`. Presenting the second as the first is the most common
way a flashing tool loses somebody's trust.

### Progress that is not a bar

`CircularProgress` for a corner where a bar would be unreadable, with an
indeterminate mode (a quarter arc, linear easing) for work with no known duration.
`PulseRing` for something that has *just* happened: it plays a fixed number of
rings and stops. An indicator that pulses forever stops being a signal within ten
seconds and costs a repaint forever.

### Dialogs that are lists

The settings dialog is a rail of categories beside a page, not a row of tabs: the
categories are stable, an operator moves between two or three repeatedly, and a
rail keeps every one visible and one click away. The file browser is the same
shape with a preview pane, and the preview reads the *file's own header* rather
than guessing from the extension — and says what it did not check.

---

## Checking a change

```bash
python tests/test_ui_design.py                 # 198 checks
python tests/test_polish.py                    # 140 checks: the behaviour above
python python/tools/screenshot_ui.py --all     # render to docs/screenshots/
python python/tools/screenshot_ui.py --features  # a job running, with notifications
python -m huaxin.ui.gallery                    # look at it, switch themes live
```

The test suite is not the whole check. A Qt stylesheet fails *silently* — a
selector that never matched, a sub-control Qt does not have, a rule the parser
dropped — and none of that shows up in the source or in a linter. So the suite
renders real windows and reads the pixels back: the window background must be the
theme's `bg`, the primary button must be painted in the accent family, its fill
must be lighter at the top than the bottom, and the three themes must paint three
different backgrounds. That is the check that proves the styling was *applied*
rather than merely written.

The gallery is the other half. It is the fastest way to see whether a colour was
chosen for the dark theme and merely inherited by the light one.
