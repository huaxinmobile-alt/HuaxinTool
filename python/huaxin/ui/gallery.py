"""The component gallery: every piece of the design system on one screen.

This is the deliverable that answers "what do I have, and what does it look
like". It is also the tool's own regression fixture - if a component stops
rendering correctly, this window shows it in one screenshot rather than after a
hunt through five panels.

    python -m huaxin.ui.gallery

It runs against the real stylesheet and the real token set, not a mock-up, so
what it shows is what the application shows. The theme picker in its title bar
switches all three themes live, which is the fastest way to see whether a colour
was chosen for the dark theme and merely inherited by the light one.

NOT a user-facing window. It is registered in the Help menu for anyone who wants
to see the building blocks, and it exists mainly so a designer or a new
contributor can add a component without reading 1500 lines of panel code first.
"""

from __future__ import annotations

import sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from huaxin.ui import components as c
from huaxin.ui import icons, style, theme_controller, tokens, animations
from huaxin.ui.titlebar import FramelessWindow

__all__ = ["Gallery", "main"]

#: Every icon, so a broken one is visible rather than waiting to be used.
_THEME_TOKENS = (
    ("bg", "Window"), ("surface", "Panel"), ("surface_alt", "Panel alt"),
    ("surface_raised", "Raised"), ("border", "Border"), ("border_strong", "Border strong"),
    ("text", "Text"), ("text_muted", "Muted"), ("text_disabled", "Disabled"),
    ("accent", "Accent"), ("ok", "Success"), ("warn", "Warning"), ("error", "Error"),
)


def _swatch(name: str, label: str, theme: tokens.Theme) -> QWidget:
    """A colour token shown as a chip of the colour with its name under it."""
    value = theme.colour(name)
    holder = QWidget()
    column = QVBoxLayout(holder)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(3)

    chip = QLabel(holder)
    chip.setFixedSize(72, 40)
    chip.setStyleSheet(
        f"background: {value};"
        f"border: 1px solid {theme.border};"
        f"border-radius: {tokens.METRICS.radius_small}px;"
    )
    chip.setToolTip(f"{name}: {value}")
    column.addWidget(chip)

    caption = QLabel(label, holder)
    caption.setProperty("role", "micro")
    caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    column.addWidget(caption)
    return holder


class Gallery(FramelessWindow):
    """The gallery window: a scrolling page of every component."""

    def __init__(self) -> None:
        super().__init__(title="Huaxin Tool", subtitle="— component gallery")

        self.themes = theme_controller.ThemeController()
        self._picker = theme_controller.theme_picker(self.themes, self.titlebar)
        self.titlebar.add_widget(self._picker)

        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.set_content_layout(outer)

        page = QWidget()
        self._page = QVBoxLayout(page)
        self._page.setContentsMargins(18, 18, 18, 18)
        self._page.setSpacing(16)

        self._build_buttons()
        self._build_inputs()
        self._build_progress()
        self._build_status()
        self._build_structure()
        self._build_table()
        self._build_icons()
        self._build_typography()
        self._build_palette()

        self._page.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(page)
        outer.addWidget(scroll)

        self.resize(1120, 820)
        # Subscribed rather than registered: the controller calls apply_theme()
        # on registered widgets and then the change callbacks, so doing both
        # would rebuild this page twice for every switch.
        self.themes.on_change(lambda _name: self._rebuild())

    # -- sections ----------------------------------------------------------

    def _build_buttons(self) -> None:
        card = c.Card(title="Buttons", caption="Five roles, each with a hover, pressed and disabled state.",
                      icon="flash")
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(c.StyledButton("Flash firmware", role="primary", icon="flash"))
        row.addWidget(c.StyledButton("Read back", role="secondary", icon="read"))
        row.addWidget(c.StyledButton("Erase", role="danger", icon="erase"))
        row.addWidget(c.StyledButton("Advanced", role="ghost", icon="settings"))
        row.addWidget(c.IconButton("refresh", tooltip="Rescan"))
        row.addWidget(c.IconButton("save", tooltip="Save the log"))
        row.addStretch(1)
        card.add_layout(row)

        row2 = QHBoxLayout()
        row2.setSpacing(10)
        row2.addWidget(c.StyledButton("Large primary", role="primary", size="large", icon="download"))
        row2.addWidget(c.StyledButton("Small secondary", role="secondary", size="small"))
        disabled = c.StyledButton("Disabled", role="primary")
        disabled.setEnabled(False)
        row2.addWidget(disabled)
        checked = c.StyledButton("Toggle", role="secondary", checkable=True)
        checked.setChecked(True)
        row2.addWidget(checked)
        row2.addStretch(1)
        card.add_layout(row2)
        self._page.addWidget(card)

    def _build_inputs(self) -> None:
        card = c.Card(title="Inputs", caption="Custom indicators, focus states and a real dropdown chevron.")
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)

        grid.addWidget(QLabel("Search"), 0, 0)
        grid.addWidget(c.SearchField(placeholder="Search devices, models or USB IDs"), 0, 1)

        grid.addWidget(QLabel("Storage"), 1, 0)
        grid.addWidget(
            c.StyledComboBox(options=[("Auto-detect", "auto"), ("UFS", "ufs"), ("eMMC", "emmc")],
                             current="auto"), 1, 1)

        text = QLabel("Select the programmer that shipped with the firmware package.")
        text.setProperty("role", "mono")
        grid.addWidget(QLabel("Command"), 2, 0)
        grid.addWidget(text, 2, 1)

        grid.setColumnStretch(1, 1)
        card.add_layout(grid)

        checks = QHBoxLayout()
        checks.setSpacing(20)
        checks.addWidget(c.StyledCheckBox("Read USB strings", tooltip="Slower, but names the device"))
        checks.addWidget(c.StyledCheckBox("Show root hubs"))
        from PyQt6.QtWidgets import QRadioButton

        for label, checked in (("Fastboot", False), ("EDL", True)):
            radio = QRadioButton(label)
            radio.setChecked(checked)
            checks.addWidget(radio)
        checks.addStretch(1)
        card.add_layout(checks)
        self._page.addWidget(card)

    def _build_progress(self) -> None:
        card = c.Card(title="Progress", caption="Animated fill, semantic states, and a segment per partition.")
        self._bar = c.StyledProgressBar()
        self._bar.setValue(46)
        card.add(self._bar)

        row = QHBoxLayout()
        row.setSpacing(10)
        for state, label in (("", "Running"), ("ok", "Done"), ("warn", "Slow"), ("error", "Failed")):
            bar = c.StyledProgressBar()
            bar.setValue({"": 46, "ok": 100, "warn": 72, "error": 31}[state])
            bar.setState(state)
            holder = QVBoxLayout()
            holder.setSpacing(4)
            holder.addWidget(bar)
            caption = QLabel(label)
            caption.setProperty("role", "micro")
            holder.addWidget(caption)
            row.addLayout(holder)
        card.add_layout(row)

        self._segments = c.SegmentedProgress()
        self._segments.setSegments([("preloader", 256), ("boot", 512), ("system", 4096),
                                    ("vendor", 1024), ("userdata", 2048)])
        self._segments.setSegmentProgress(0, 1.0)
        self._segments.setSegmentProgress(1, 1.0)
        self._segments.setSegmentProgress(2, 0.42)
        card.add(self._segments)

        indeterminate = c.StyledProgressBar()
        indeterminate.setIndeterminate(True)
        card.add(indeterminate)

        ring_row = QHBoxLayout()
        ring_row.setSpacing(18)
        for value, state, label in ((0, "", "Starting"), (35, "", "Running"), (100, "ok", "Done"),
                                    (62, "error", "Failed")):
            ring = c.CircularProgress(size=34, thickness=4)
            ring.setValue(value)
            ring.setState(state)
            holder = QVBoxLayout()
            holder.setSpacing(4)
            holder.addWidget(ring, 0, Qt.AlignmentFlag.AlignHCenter)
            caption = QLabel(label)
            caption.setProperty("role", "micro")
            caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            holder.addWidget(caption)
            ring_row.addLayout(holder)

        spinner = c.CircularProgress(size=34, thickness=4)
        spinner.setIndeterminate(True)
        spinner_holder = QVBoxLayout()
        spinner_holder.setSpacing(4)
        spinner_holder.addWidget(spinner, 0, Qt.AlignmentFlag.AlignHCenter)
        spinner_caption = QLabel("Unknown duration")
        spinner_caption.setProperty("role", "micro")
        spinner_caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        spinner_holder.addWidget(spinner_caption)
        ring_row.addLayout(spinner_holder)

        pulse = c.PulseRing(size=34, tone="ok")
        pulse.start(rings=1000)
        pulse_holder = QVBoxLayout()
        pulse_holder.setSpacing(4)
        pulse_holder.addWidget(pulse, 0, Qt.AlignmentFlag.AlignHCenter)
        pulse_caption = QLabel("Device arrived")
        pulse_caption.setProperty("role", "micro")
        pulse_caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        pulse_holder.addWidget(pulse_caption)
        ring_row.addLayout(pulse_holder)
        ring_row.addStretch(1)
        card.add_layout(ring_row)
        self._page.addWidget(card)

        self._build_operation_status()
        self._build_toasts()

    def _build_operation_status(self) -> None:
        """The live readout, with figures that look like a real transfer."""
        from huaxin.core.progress import JobProgress
        from huaxin.ui.opstatus import OperationStatus

        card = c.Card(
            title="Operation status",
            caption="What is running, how fast, for how long, and what is left. "
                    "The readout in the status bar while a job is in progress.",
        )
        readout = OperationStatus()
        readout.begin("flash_partition")
        readout.report(JobProgress(
            operation_type="flashing", current_partition="super",
            current_partition_index=1, total_partitions=5,
            bytes_written=1_500_000_000, total_bytes=4_000_000_000,
            percentage=37.5, speed_mbps=21.75, eta_seconds=88.0,
        ))
        card.add(readout)

        estimated = OperationStatus()
        estimated.begin("read_partition")
        estimated.report(JobProgress(operation_type="reading", percentage=64.0,
                                     status_message="reading userdata"))
        card.add(estimated)

        note = c.Callout(
            "The first line shows an ETA the device reported. The second shows one this "
            "tool calculated from the rate of progress so far, which is why it is marked "
            "with a tilde — an estimate presented as a fact is how somebody ends up "
            "watching \"2 minutes left\" for twenty.",
            severity="info",
        )
        card.add(note)
        self._page.addWidget(card)

    def _build_toasts(self) -> None:
        """The four toast tones, shown as static cards.

        Rendered inline rather than fired into the corner of this window: the
        gallery is a reference sheet, and a notification that dismisses itself
        while somebody is reading it is exactly the wrong thing to put on one.
        """
        from huaxin.ui.toasts import Toast

        card = c.Card(
            title="Notifications",
            caption="Transient messages: they never take focus, they pause while the "
                    "pointer is over them, and everything they say also reaches the log.",
        )
        holder = c.Card()
        column = QVBoxLayout(holder)
        column.setSpacing(8)
        for level, title, message in (
            ("info", "Scan finished", "4 devices found, 2 recognised targets."),
            ("ok", "Flash complete", "boot, system and vendor written in 3m 12s."),
            ("warn", "Device disconnected", "05c6:9008 is no longer on the bus."),
            ("error", "Write failed", "The device stopped responding at 43%."),
        ):
            toast = Toast(holder, level=level, title=title, message=message, timeout_ms=0)
            # Never, rather than after twelve seconds: a timeout here would empty
            # the sheet while it is being looked at.
            toast.setFixedWidth(Toast.WIDTH)
            column.addWidget(toast)
        card.add(holder)
        self._page.addWidget(card)

    def _build_status(self) -> None:
        card = c.Card(title="Status", caption="Dots, chips and stat tiles.")
        row = QHBoxLayout()
        row.setSpacing(16)
        for tone, label in (("ok", "Backend ready"), ("warn", "Initialising"), ("error", "Error"),
                            ("muted", "Idle")):
            holder = QHBoxLayout()
            holder.setSpacing(6)
            holder.addWidget(c.StatusDot(tone=tone))
            caption = QLabel(label)
            holder.addWidget(caption)
            row.addLayout(holder)
        row.addStretch(1)
        card.add_layout(row)

        chips = QHBoxLayout()
        chips.setSpacing(8)
        chips.addWidget(c.Chip("Qualcomm", tone="accent", icon="chip"))
        chips.addWidget(c.Chip("Connected", tone="ok", icon="success"))
        chips.addWidget(c.Chip("Unverified", tone="warn", icon="warning"))
        chips.addWidget(c.Chip("Locked", tone="error", icon="error"))
        chips.addWidget(c.Chip("3 devices"))
        chips.addStretch(1)
        card.add_layout(chips)

        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        for stat in (c.Stat("3", "USB devices", "accent"), c.Stat("1", "Flashing targets", "ok"),
                     c.Stat("0", "Errors", "neutral")):
            tile = c.StatTile(stat)
            tile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            tiles.addWidget(tile)
        card.add_layout(tiles)
        self._page.addWidget(card)

    def _build_structure(self) -> None:
        card = c.Card(title="Surfaces", caption="Cards, callouts, headers and the empty state.")
        card.add(c.SectionHeader("Section header", caption="With a caption and a trailing action",
                                 icon="folder", accent=True).add_action(
            c.IconButton("external", tooltip="Open")))
        card.add(c.Divider())
        for severity, title, text in (
            ("info", "Note", "The programmer is uploaded before configuration begins."),
            ("ok", "Verified", "The .tar.md5 digest matches the archive."),
            ("warn", "Careful", "Erasing userdata destroys everything on the device."),
            ("error", "Failed", "The device disconnected while writing to SYSTEM."),
        ):
            card.add(c.Callout(text, severity=severity, title=title))

        empty = c.EmptyState(icon="usb", title="No device selected",
                             message="Pick a device from the list, or press Scan Devices.")
        empty.setMinimumHeight(170)
        card.add(empty)
        self._page.addWidget(card)

    def _build_table(self) -> None:
        card = c.Card(title="Table", caption="Alternating rows, hover, selection and a sticky header.")
        table = c.StyledTable(0, 4)
        table.setHeaders(["Vendor", "USB ID", "Mode", "Device"])
        rows = [
            ("Qualcomm", "05c6:9008", "EDL (Emergency Download)", "Generic MSM8998"),
            ("MediaTek", "0e8d:0003", "BROM", "Helio P22 (MT6765)"),
            ("Samsung", "04e8:685d", "Download mode (Odin)", "Galaxy S9"),
            ("Qualcomm", "05c6:900e", "Diagnostic", "—"),
        ]
        for vendor, usb, mode, name in rows:
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(vendor))
            table.setItem(row, 1, QTableWidgetItem(usb))
            table.setItem(row, 2, QTableWidgetItem(mode))
            table.setItem(row, 3, QTableWidgetItem(name))
        table.selectRow(0)
        table.setFixedHeight(150)
        card.add(table)
        self._page.addWidget(card)

    def _build_icons(self) -> None:
        card = c.Card(title="Icons", caption=f"{len(icons.ICON_NAMES)} vector icons, recoloured per theme.")
        grid = QGridLayout()
        grid.setSpacing(10)
        for index, name in enumerate(icons.ICON_NAMES):
            holder = QVBoxLayout()
            holder.setSpacing(2)
            glyph = QLabel()
            glyph.setPixmap(icons.pixmap(name, tokens.active_theme().text, 22))
            glyph.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            caption = QLabel(name)
            caption.setProperty("role", "micro")
            caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            holder.addWidget(glyph)
            holder.addWidget(caption)
            grid.addLayout(holder, index // 10, index % 10)
        card.add_layout(grid)
        self._page.addWidget(card)

    def _build_typography(self) -> None:
        card = c.Card(title="Typography", caption="Four sizes and a monospace, all from the token set.")
        for role, text in (
            ("title", f"Title — {tokens.METRICS.font_size_title}px"),
            ("heading", f"Heading — {tokens.METRICS.font_size_heading}px"),
            ("", f"Body — {tokens.METRICS.font_size_body}px · the default for everything that is read"),
            ("caption", f"Caption — {tokens.METRICS.font_size_small}px · supporting text"),
            ("micro", f"Micro — {tokens.METRICS.font_size_micro}px · status bar and chips"),
        ):
            label = QLabel(text)
            if role:
                label.setProperty("role", role)
            card.add(label)
        mono = QLabel("12:04:31.482  [INFO ]  loaded native module huaxin_core 0.5.0")
        mono.setProperty("role", "mono")
        card.add(mono)
        self._page.addWidget(card)

    def _build_palette(self) -> None:
        card = c.Card(title="Palette", caption="Every semantic colour, at the ratio it is checked against.")
        theme = tokens.active_theme()
        grid = QGridLayout()
        grid.setSpacing(10)
        for index, (name, label) in enumerate(_THEME_TOKENS):
            grid.addWidget(_swatch(name, label, theme), index // 7, index % 7)
        card.add_layout(grid)

        note = QLabel(
            "Every foreground/background pair the interface uses is checked against WCAG AA "
            "(4.5:1 for body text). The worst pair in this theme is "
            f"{min(tokens.contrast_ratio(theme.colour(f), theme.colour(b)) for f, b in theme.contrasted_with()):.2f}:1."
        )
        note.setWordWrap(True)
        note.setProperty("role", "caption")
        card.add(note)
        self._page.addWidget(card)

    # -- lifecycle ---------------------------------------------------------

    def apply_theme(self) -> None:
        """Called by the controller after a switch.

        The whole page is rebuilt rather than repainted, because the swatches and
        icon glyphs are pixmaps and stylesheet strings baked at construction. A
        gallery of forty components is cheap to rebuild and expensive to get
        wrong.
        """
        self._rebuild()

    def _rebuild(self) -> None:
        while self._page.count() > 1:
            item = self._page.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())

        self._build_buttons()
        self._build_inputs()
        self._build_progress()
        self._build_status()
        self._build_structure()
        self._build_table()
        self._build_icons()
        self._build_typography()
        self._build_palette()

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())


def main(argv: list[str] | None = None) -> int:
    """Runs the gallery on its own, for looking at the design system in isolation."""
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Huaxin Tool — components")
    # Fusion renders identically everywhere, so the stylesheet is the only thing
    # deciding how anything looks - and a theme that only looks right on one
    # platform is a theme that has not been checked.
    app.setStyle("Fusion")
    style.apply_theme(app)

    gallery = Gallery()
    gallery.show()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
