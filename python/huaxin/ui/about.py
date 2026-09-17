"""The About dialog, and the build facts behind it.

WHY THIS IS NOT A MESSAGE BOX. The version of a flashing tool matters in a way
the version of a text editor does not: an operator reporting a failed flash is
asked "which build?", and the answer is not one number. It is the interface
version, the native backend, the libusb the backend linked against, and the Qt
the interface runs on - and four of those five are invisible from the menu.

So the dialog states all of them, and offers to copy them as text, because the
actual use of this window is pasting its contents into a bug report.

THE ONE THING THIS DIALOG DOES NOT PRETEND. There is no update channel. The tool
cannot check for a newer version because there is no server to ask, so it says so
rather than showing a button that produces an error. What it offers instead is
the thing that is actually useful: the exact build details, ready to copy.

Licence and attribution are shown because they have to be - this application links
Qt and libusb, both under the LGPL, and shipping their notices in a file nobody
opens is not how that obligation is met.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from huaxin import __version__
from huaxin.ui import components as ui
from huaxin.ui import icons, tokens
from huaxin.ui.safe_slot import safe_slot

__all__ = ["AboutDialog", "AppLogo", "build_info", "diagnostics_text"]


# -----------------------------------------------------------------------------
#  The logo
# -----------------------------------------------------------------------------


class AppLogo(QWidget):
    """The application's mark: a chip glyph on a rounded accent tile.

    Painted rather than loaded from a file, for the same reason the icons are:
    a bitmap would need one copy per scale factor, and this is sharp at every
    size by construction. It also cannot go missing from a build.
    """

    def __init__(self, parent: QWidget | None = None, *, size: int = 72) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        theme = tokens.active_theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        box = QRectF(0, 0, self.width(), self.height())
        gradient = QLinearGradient(box.topLeft(), box.bottomRight())
        gradient.setColorAt(0.0, QColor(theme.accent_gradient_top))
        gradient.setColorAt(1.0, QColor(theme.accent_gradient_bottom))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        radius = self.width() * 0.22
        painter.drawRoundedRect(box, radius, radius)

        # The glyph itself, drawn from the icon set in the on-accent colour so it
        # reads on all three themes - the accent is light in Graphite, where a
        # white glyph would disappear.
        glyph = icons.pixmap("chip", theme.text_on_accent, int(self.width() * 0.54))
        painter.drawPixmap(
            (self.width() - glyph.width()) // 2,
            (self.height() - glyph.height()) // 2,
            glyph,
        )
        painter.end()


# -----------------------------------------------------------------------------
#  Build facts
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Fact:
    """One labelled line in the build table."""

    label: str
    value: str
    tooltip: str = ""


def _native_facts() -> list[Fact]:
    """The native module's own version, or why it is not there."""
    try:
        import huaxin_core
    except ImportError as exc:
        return [Fact("Native backend", f"not available ({exc})",
                     "The C++ extension is missing or failed to import. Build it "
                     "with the CMake project in ../src/cpp.")]
    facts = [Fact("Native backend", getattr(huaxin_core, "__version__", "unknown"))]
    try:
        bridge = huaxin_core.HardwareBridge()
        facts.append(Fact("libusb", bridge.libusb_version() or "unknown"))
        facts.append(Fact("Backend build", bridge.backend_version() or "unknown"))
    except Exception:  # a bridge that will not construct is a fact worth stating
        facts.append(Fact("libusb", "not started"))
    return facts


def _build_date() -> str:
    """When this build was made.

    Read from the executable's own timestamp when frozen, and reported as a
    development tree otherwise - rather than stamping a date at import time,
    which would say the build is from whenever it was last run.
    """
    if not getattr(sys, "frozen", False):
        return "development tree"
    try:
        stamp = Path(sys.executable).stat().st_mtime
    except OSError:
        return "unknown"
    return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")


def build_info() -> list[Fact]:
    """Every version this application is made of.

    Gathered from the running process rather than from a file, so it describes
    what is actually loaded - which is the question being asked when somebody
    reads this dialog.
    """
    from PyQt6.QtCore import QT_VERSION_STR, PYQT_VERSION_STR

    facts = [
        Fact("Huaxin Tool", __version__),
        Fact("Build", _build_date()),
    ]
    facts.extend(_native_facts())
    facts.extend(
        [
            Fact("Python", platform.python_version()),
            Fact("Qt", QT_VERSION_STR),
            Fact("PyQt", PYQT_VERSION_STR),
            Fact("System", f"{platform.system()} {platform.release()} ({platform.machine()})"),
        ]
    )
    return facts


def diagnostics_text() -> str:
    """The build facts as plain text, for a bug report.

    Plain text and key: value pairs, because this gets pasted into an issue
    tracker or an email and a table drawn with box characters survives neither.
    """
    lines = ["Huaxin Tool — build details", ""]
    lines.extend(f"{fact.label}: {fact.value}" for fact in build_info())
    lines.append(f"Frozen: {'yes' if getattr(sys, 'frozen', False) else 'no'}")
    lines.append("")
    lines.append("Supported targets:")
    lines.extend(f"  {name}: {state}" for name, state in SUPPORT_STATUS)
    lines.append("")
    lines.append("Generated " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    return "\n".join(lines)


#: What each vendor's support actually amounts to, in the tool's own words.
#:
#: Stated in the About box on purpose. Every tool in this market claims every
#: protocol; the difference between "implemented and tested against recorded
#: traffic" and "implemented from the specification, waiting for hardware" is the
#: difference between a feature and a promise, and an operator deciding whether to
#: trust this with a device is entitled to know which one they are looking at.
SUPPORT_STATUS: tuple[tuple[str, str], ...] = (
    ("ADB / Fastboot", "implemented"),
    ("Qualcomm EDL (Sahara, Firehose)", "implemented"),
    ("MediaTek BROM / Preloader", "implemented"),
    ("Unisoc Research Download", "read-only — packages can be inspected; flashing is not implemented"),
    ("Samsung Odin", "read-only — PIT and packages can be inspected; flashing is not implemented"),
)

#: Third-party components and the licence each is under. Kept here rather than in
#: a text file because the dialog is where somebody looks, and a notice file that
#: ships unread does not discharge the obligation to display it.
THIRD_PARTY = (
    ("Qt 6", "LGPL-3.0", "https://www.qt.io/licensing/open-source-lgpl-obligations"),
    ("PyQt6", "GPL-3.0", "https://www.riverbankcomputing.com/software/pyqt/"),
    ("libusb", "LGPL-2.1", "https://libusb.info/"),
    ("pybind11", "BSD-3-Clause", "https://github.com/pybind/pybind11"),
)

_DOC_FILES = (
    ("Architecture", "architecture.md"),
    ("Design system", "design-system.md"),
    ("Error handling", "errors.md"),
    ("Firehose commands", "firehose-commands.md"),
    ("MediaTek notes", "mediatek-notes.md"),
    ("SPD PAC format", "spd-pac-format.md"),
    ("Contributing", "contributing.md"),
)


def docs_dir() -> Path | None:
    """Where the documentation lives, frozen or not. None when it is not shipped."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidate = base / "docs"
    else:
        candidate = Path(__file__).resolve().parents[3] / "docs"
    return candidate if candidate.is_dir() else None


# -----------------------------------------------------------------------------
#  The dialog
# -----------------------------------------------------------------------------


class AboutDialog(QDialog):
    """Version, build facts, support status, attribution and the licence."""

    def __init__(self, parent: QWidget | None = None, *, log_path: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("About Huaxin Tool")
        self.setMinimumWidth(640)
        self._log_path = log_path

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            tokens.METRICS.gap_lg, tokens.METRICS.gap_lg, tokens.METRICS.gap_lg, tokens.METRICS.pad
        )
        layout.setSpacing(tokens.METRICS.gap)
        layout.addWidget(self._build_header())
        layout.addWidget(self._build_facts())
        layout.addWidget(self._build_support())
        layout.addWidget(self._build_attribution())
        layout.addLayout(self._build_buttons())

    # -- sections ----------------------------------------------------------

    def _build_header(self) -> QWidget:
        holder = QWidget(self)
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(tokens.METRICS.gap)

        row.addWidget(AppLogo(holder), 0, Qt.AlignmentFlag.AlignTop)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        name = QLabel("Huaxin Tool", holder)
        name.setProperty("role", "title")
        tagline = QLabel(
            "Multi-vendor Android firmware flashing and software repair.", holder
        )
        tagline.setWordWrap(True)
        version = QLabel(f"Version {__version__}  ·  {_build_date()}", holder)
        version.setProperty("role", "caption")
        column.addWidget(name)
        column.addWidget(tagline)
        column.addWidget(version)
        column.addStretch(1)
        row.addLayout(column, 1)
        return holder

    def _build_facts(self) -> QWidget:
        frame = QFrame(self)
        frame.setProperty("role", "inset")
        grid = QGridLayout(frame)
        grid.setContentsMargins(
            tokens.METRICS.pad_sm, tokens.METRICS.pad_sm, tokens.METRICS.pad_sm, tokens.METRICS.pad_sm
        )
        grid.setHorizontalSpacing(tokens.METRICS.gap)
        grid.setVerticalSpacing(3)
        for row, fact in enumerate(build_info()):
            label = QLabel(fact.label, frame)
            label.setProperty("role", "caption")
            value = QLabel(fact.value, frame)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            mono = QFont(value.font())
            mono.setFamilies([tokens.METRICS.font_mono.split(",")[0].strip("' ")])
            value.setFont(mono)
            if fact.tooltip:
                label.setToolTip(fact.tooltip)
            grid.addWidget(label, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(value, row, 1)
        grid.setColumnStretch(1, 1)
        return frame

    def _build_support(self) -> QWidget:
        box = QFrame(self)
        box.setProperty("role", "card")
        column = QVBoxLayout(box)
        column.setContentsMargins(
            tokens.METRICS.pad_sm, tokens.METRICS.pad_sm, tokens.METRICS.pad_sm, tokens.METRICS.pad_sm
        )
        column.setSpacing(tokens.METRICS.gap_xs)

        heading = QLabel("Supported targets", box)
        heading.setProperty("role", "heading")
        column.addWidget(heading)

        for name, state in SUPPORT_STATUS:
            line = QLabel(box)
            read_only = state.startswith("read-only")
            tone = "warn" if read_only else "ok"
            line.setText(
                f'<span style="color:{tokens.active_theme().colour(tone)}">'
                f'{"◐" if read_only else "●"}</span>  '
                f"<b>{name}</b> — <span>{state}</span>"
            )
            line.setWordWrap(True)
            column.addWidget(line)

        note = QLabel(
            "A read-only target can be inspected but not written: this build does not "
            "implement that vendor's download protocol, and offering a button that "
            "cannot work is worse than saying so. Nothing in this application has been "
            "run against real hardware — every protocol is verified against recorded "
            "traffic and the vendor's own specification.",
            box,
        )
        note.setWordWrap(True)
        note.setProperty("role", "caption")
        column.addWidget(note)
        return box

    def _build_attribution(self) -> QWidget:
        box = QFrame(self)
        box.setProperty("role", "card")
        column = QVBoxLayout(box)
        column.setContentsMargins(
            tokens.METRICS.pad_sm, tokens.METRICS.pad_sm, tokens.METRICS.pad_sm, tokens.METRICS.pad_sm
        )
        column.setSpacing(tokens.METRICS.gap_xs)

        heading = QLabel("Licence and third-party components", box)
        heading.setProperty("role", "heading")
        column.addWidget(heading)

        licence = QLabel(
            "Huaxin Tool is released under the MIT licence. See LICENSE beside the "
            "application; the components above and the product names are covered by "
            "THIRD_PARTY_NOTICES.md.",
            box,
        )
        licence.setWordWrap(True)
        column.addWidget(licence)

        for name, licence_name, url in THIRD_PARTY:
            line = QLabel(box)
            line.setText(f"<b>{name}</b> — {licence_name} — <a href='{url}'>{url}</a>")
            line.setOpenExternalLinks(True)
            line.setWordWrap(True)
            column.addWidget(line)

        return box

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(tokens.METRICS.gap_sm)

        copy = ui.StyledButton("Copy build details", self, role="primary", icon="copy",
                               tooltip="Puts the table above on the clipboard as plain text, "
                                       "ready to paste into a report.")
        copy.clicked.connect(safe_slot(self._on_copy))
        row.addWidget(copy)

        docs = ui.StyledButton("Documentation", self, role="secondary", icon="file",
                               tooltip="Open the folder holding the technical notes.")
        docs.clicked.connect(safe_slot(self._on_docs))
        row.addWidget(docs)

        if self._log_path:
            log = ui.StyledButton("Open log", self, role="secondary", icon="folder",
                                  tooltip=self._log_path)
            log.clicked.connect(safe_slot(self._on_log))
            row.addWidget(log)

        row.addStretch(1)
        close = ui.StyledButton("Close", self, role="secondary")
        close.clicked.connect(safe_slot(self.accept))
        row.addWidget(close)

        self._status = QLabel("", self)
        self._status.setProperty("role", "caption")
        row.addWidget(self._status)
        return row

    # -- actions -----------------------------------------------------------

    def _on_copy(self) -> None:
        from PyQt6.QtWidgets import QApplication

        QApplication.clipboard().setText(diagnostics_text())
        self._status.setText("Copied.")

    def _on_docs(self) -> None:
        """Opens one of the technical notes, or the folder holding them.

        A menu rather than a folder because the notes are the reason somebody
        pressed the button: "which document explains the PAC layout" is answered
        here, and having to find it in a folder listing is not.
        """
        from PyQt6.QtWidgets import QMenu

        target = docs_dir()
        if target is None:
            self._status.setText("No documentation is bundled with this build.")
            return

        menu = QMenu(self)
        for label, filename in _DOC_FILES:
            path = target / filename
            if path.is_file():
                menu.addAction(label, safe_slot(self._open_document, path))
        menu.addSeparator()
        menu.addAction("Open the documentation folder",
                       safe_slot(self._open_document, target))
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))
        del menu

    def _open_document(self, path: Path) -> None:
        import os
        import subprocess

        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # noqa: S606 - opening a bundled document
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError:
            self._status.setText(f"The document is at {path}")

    def _on_log(self) -> None:
        from huaxin.ui.settings_dialog import open_in_file_manager

        if not self._log_path:
            return
        if not open_in_file_manager(Path(self._log_path)):
            self._status.setText(f"The log is at {self._log_path}")
