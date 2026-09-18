"""The settings dialog.

Every control here maps onto exactly one field of `huaxin.core.config.Settings`,
and nothing is stored anywhere else - so a value the operator changes is a value
that survives a restart, and a value the operator does not change is the one the
tool ships with.

Two things this dialog deliberately does that a plain preferences pane does not:

* it says what a setting is *for*, in the field's tooltip, because "Transfer
  timeout" means nothing to someone who has not watched a 4 GB image crawl, and
* it offers to open the settings file, because a support engineer asking an
  operator to "send me your config" should not have to explain where it lives.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from huaxin.core.config import (
    NUMBER_BOUNDS,
    Settings,
    default_log_path,
    save_settings,
    settings_path,
)
from huaxin.ui import components as ui
from huaxin.ui import filedialog, icons, tokens
from huaxin.ui.safe_slot import safe_slot
from huaxin.ui.theme import COLORS

__all__ = ["SettingsDialog", "DriverHelpDialog", "open_in_file_manager"]

#: A sentence under each category's heading. Written as what the category is
#: *for* rather than what it contains, because "Timeouts & retries" already says
#: what it contains.
_PAGE_CAPTIONS = {
    "general": "What the tool does when you start something, and how it builds the device list.",
    "appearance": "Colour and motion. Both apply immediately, without restarting.",
    "timeouts": "How long to wait, and what to do when a device does not answer in time.",
    "logging": "What is recorded, where it goes, and how large it may grow.",
    "tools": "Where adb and fastboot are, and where this tool's own files live.",
    "advanced": "Settings that exist for a specific problem rather than for everyday use.",
}

#: Human labels for the log levels, so the combo reads as English rather than as
#: the identifiers the settings file holds.
_LOG_LEVEL_LABELS = (
    ("debug", "Debug - everything, including per-packet detail"),
    ("info", "Info - the normal record (recommended)"),
    ("warning", "Warning - only what went wrong"),
    ("error", "Error - only failures"),
    ("critical", "Critical - only failures the tool caused itself"),
)


def open_in_file_manager(path: Path) -> bool:
    """Reveals a file or folder in the platform's file manager.

    Returns False when the platform has no way to do it, so a caller can fall
    back to showing the path instead of silently doing nothing.
    """
    target = path if path.exists() else path.parent
    try:
        if sys.platform.startswith("win"):
            # /select, highlights the file itself when it exists.
            if path.exists() and path.is_file():
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                subprocess.Popen(["explorer", str(target)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return True
    except OSError:
        return False


class SettingsDialog(QDialog):
    """Editor for the JSON settings file.

    Laid out as a rail of categories beside a page, rather than as tabs. The
    categories are stable and an operator moves between two or three of them
    repeatedly while working something out - "raise the timeout, now check the
    log level, back to the timeout" - and a vertical rail keeps every category
    visible and one click away, where a row of tabs runs out of room and starts
    scrolling as soon as there are more than four.
    """

    #: (key, label, icon, page builder). The order is the order they appear, and
    #: the first is what the dialog opens on. The icon set is the same one every
    #: other control uses, so nothing here is a special case.
    PAGES = (
        ("general", "General", "settings", "_build_general_page"),
        ("appearance", "Appearance", "flash", "_build_appearance_page"),
        ("timeouts", "Timeouts & retries", "refresh", "_build_timeouts_page"),
        ("logging", "Logging", "file", "_build_logging_page"),
        ("tools", "Tools & folders", "folder", "_build_tools_page"),
        ("advanced", "Advanced", "chip", "_build_advanced_page"),
    )

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(760, 560)

        self._settings = settings
        self._result: Settings | None = None

        self._nav = QListWidget(self)
        self._nav.setObjectName("SettingsNav")
        self._nav.setFixedWidth(190)
        self._nav.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        for key, label, icon_name, _ in self.PAGES:
            item = QListWidgetItem(icons.icon(icon_name, tokens.active_theme().text_muted,
                                              tokens.METRICS.icon_size), label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self._nav.addItem(item)

        self._stack = QStackedWidget(self)
        self._pages: dict[str, QWidget] = {}
        for key, label, _, builder in self.PAGES:
            page = getattr(self, builder)()
            self._pages[key] = page
            self._stack.addWidget(page)
        self._nav.currentRowChanged.connect(self._stack.setCurrentIndex)
        self._nav.setCurrentRow(0)

        heading_column = QVBoxLayout()
        heading_column.setContentsMargins(0, 0, 0, 0)
        heading_column.setSpacing(2)
        self._page_title = QLabel("", self)
        self._page_title.setObjectName("SettingsPageTitle")
        self._page_caption = QLabel("", self)
        self._page_caption.setObjectName("SettingsPageCaption")
        self._page_caption.setWordWrap(True)
        heading_column.addWidget(self._page_title)
        heading_column.addWidget(self._page_caption)

        self._stack.currentChanged.connect(safe_slot(self._on_page_changed))

        detail = QWidget(self)
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(tokens.METRICS.pad, tokens.METRICS.pad,
                                         tokens.METRICS.pad, tokens.METRICS.pad)
        detail_layout.setSpacing(tokens.METRICS.gap_sm)
        detail_layout.addLayout(heading_column)
        detail_layout.addWidget(self._stack, 1)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._nav)
        body.addWidget(detail, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 12, 12)
        layout.setSpacing(tokens.METRICS.gap_sm)
        layout.addLayout(body, 1)

        self._path_label = QLabel(f"Settings file: {settings_path()}", self)
        self._path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._path_label.setStyleSheet(f"color: {COLORS['text_dim']};")
        reveal = QPushButton("Show in folder", self)
        reveal.clicked.connect(self._on_reveal)
        reveal.setToolTip("Open the folder holding settings.json, so it can be copied "
                          "to another machine or sent to support.")

        path_row = QHBoxLayout()
        path_row.setContentsMargins(12, 0, 0, 0)
        path_row.addWidget(self._path_label, 1)
        path_row.addWidget(reveal)
        layout.addLayout(path_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.RestoreDefaults,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        restore = buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults)
        restore.setToolTip(
            "Put every category back to the values the tool ships with. The file is "
            "not written until you press OK."
        )
        restore.clicked.connect(self._on_restore)
        layout.addWidget(buttons)

        self._on_page_changed(0)

    # -----------------------------------------------------------------------
    #  Pages
    # -----------------------------------------------------------------------

    def _on_page_changed(self, index: int) -> None:
        """Fills the heading above the page from the page's own entry."""
        if not 0 <= index < len(self.PAGES):
            return
        key = self.PAGES[index][0]
        self._page_title.setText(self.PAGES[index][1])
        self._page_caption.setText(_PAGE_CAPTIONS.get(key, ""))
        self._caption_key = key

    def show_page(self, key: str) -> bool:
        """Opens the dialog on a named category. False when there is no such page.

        Named rather than indexed on purpose: the caller is saying "the page
        where the adb path lives", and a caller that has to know it is the sixth
        row of a list is a caller that breaks the next time a category is added
        in the middle.
        """
        for row, (page_key, _, _, _) in enumerate(self.PAGES):
            if page_key == key:
                self._nav.setCurrentRow(row)
                return True
        return False

    def current_page(self) -> str:
        """The key of the category on screen."""
        row = self._nav.currentRow()
        return self.PAGES[row][0] if 0 <= row < len(self.PAGES) else ""

    def _build_general_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        behaviour = QGroupBox("When an operation starts", page)
        form = QFormLayout(behaviour)

        self._confirm = QCheckBox("Ask before erasing or writing a partition", behaviour)
        self._confirm.setToolTip(
            "Shows a confirmation naming what will be destroyed, and defaults to No.\n"
            "Turning this off makes batch work faster and a mistake unrecoverable."
        )
        form.addRow(self._confirm)

        self._scan_on_startup = QCheckBox("Scan for devices at startup", behaviour)
        form.addRow(self._scan_on_startup)

        self._auto_scan = self._whole_box(
            behaviour, "auto_scan_seconds",
            "Rescan the USB bus on a timer, so a device that is plugged in appears "
            "without anybody pressing anything.\n"
            "Zero turns it off. The scan is skipped while an operation is running, "
            "because reading a device's string descriptors mid-flash is at best "
            "noise in the log.",
            suffix=" s", zero_text="Never",
        )
        form.addRow("Scan every", self._auto_scan)

        layout.addWidget(behaviour)

        listing = QGroupBox("Device list", page)
        listing_form = QFormLayout(listing)

        self._read_strings = QCheckBox("Read USB serial and model strings", listing)
        self._read_strings.setToolTip(
            "Each string needs the device opened, so this costs time on every scan.\n"
            "Turning it off makes a scan much faster but leaves the list showing "
            "vendor and product IDs only."
        )
        listing_form.addRow(self._read_strings)

        self._show_hubs = QCheckBox("Show USB root hubs", listing)
        self._show_hubs.setToolTip(
            "Host-controller hubs, not devices. Only useful when working out why a "
            "device is not appearing."
        )
        listing_form.addRow(self._show_hubs)

        layout.addWidget(listing)
        layout.addStretch(1)
        return self._wrap_scrollable(page)

    def _build_appearance_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        theme_box = QGroupBox("Colour theme", page)
        theme_form = QFormLayout(theme_box)
        self._theme = ui.StyledComboBox(theme_box)
        for name in tokens.theme_names():
            theme = tokens.THEMES[name]
            self._theme.addItem(theme.label, name)
        self._theme.setToolTip(
            "All three themes pass the same contrast checks, so this is a preference "
            "rather than a readability trade-off. It can also be changed from the "
            "picker in the title bar, which is faster when trying one out."
        )
        theme_form.addRow("Theme", self._theme)
        layout.addWidget(theme_box)

        motion = QGroupBox("Motion", page)
        motion_form = QFormLayout(motion)
        self._animations = QCheckBox("Animate the interface", motion)
        self._animations.setToolTip(
            "Progress bars slide to their new value, panels fade, notifications slide "
            "in.\nTurning this off makes everything appear instantly. Worth doing on a "
            "remote session, where every frame has to travel across the wire, and for "
            "anyone who finds moving interfaces tiring."
        )
        motion_form.addRow(self._animations)
        note = QLabel(
            "Nothing in this application depends on an animation to report something: "
            "text, colour and the log change immediately either way.",
            motion,
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {COLORS['text_dim']};")
        motion_form.addRow(note)
        layout.addWidget(motion)

        layout.addStretch(1)
        return self._wrap_scrollable(page)

    def _build_timeouts_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        timeouts = QGroupBox("Timeouts", page)
        form = QFormLayout(timeouts)

        self._command_timeout = self._seconds_box(
            "default_timeout",
            "Seconds to wait for a device to answer a command.\n"
            "Too short and a slow device looks broken; too long and a "
            "cable that has come out leaves the UI waiting."
        )
        form.addRow("Command timeout", self._command_timeout)

        self._transfer_timeout = self._seconds_box(
            "transfer_timeout",
            "Seconds to wait for a bulk transfer that is moving data.\n"
            "An image of several gigabytes over a slow link can take "
            "fifteen minutes; this is separate from the command timeout "
            "for that reason."
        )
        form.addRow("Transfer timeout", self._transfer_timeout)

        self._connect_timeout = self._seconds_box(
            "connect_timeout",
            "Seconds to wait when opening a device or scanning the bus."
        )
        form.addRow("Connect timeout", self._connect_timeout)
        layout.addWidget(timeouts)

        retries = QGroupBox("Retries", page)
        retry_form = QFormLayout(retries)

        self._attempts = self._whole_box(
            retries, "max_retry_attempts",
            "Total attempts including the first, so 1 disables retrying.\n"
            "Only failures that could plausibly succeed on a second try are "
            "repeated: a dropped link, a stalled handshake. A failed write and a "
            "locked bootloader are never retried automatically."
        )
        retry_form.addRow("Attempts", self._attempts)

        self._initial_delay = self._whole_box(
            retries, "retry_initial_delay_ms",
            "The wait before the second attempt. Each attempt after that waits "
            "twice as long, up to the ceiling below.", suffix=" ms"
        )
        retry_form.addRow("First delay", self._initial_delay)

        self._max_delay = self._whole_box(
            retries, "retry_max_delay_ms",
            "The longest a retry will ever wait.", suffix=" ms"
        )
        retry_form.addRow("Maximum delay", self._max_delay)

        self._budget = self._whole_box(
            retries, "retry_total_budget_s",
            "Give up on an operation entirely after this, however many attempts "
            "are left. Zero means no limit.", suffix=" s", zero_text="No limit"
        )
        retry_form.addRow("Total budget", self._budget)

        layout.addWidget(retries)
        layout.addStretch(1)
        return self._wrap_scrollable(page)

    def _build_logging_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        recording = QGroupBox("What is recorded", page)
        form = QFormLayout(recording)

        self._log_level = ui.StyledComboBox(recording)
        for value, label in _LOG_LEVEL_LABELS:
            self._log_level.addItem(label, value)
        form.addRow("Level", self._log_level)

        self._log_to_file = QCheckBox("Write a log file", recording)
        form.addRow(self._log_to_file)

        self._log_to_console = QCheckBox("Mirror log lines into the console", recording)
        form.addRow(self._log_to_console)

        self._auto_scroll = QCheckBox("Follow the console as lines arrive", recording)
        form.addRow(self._auto_scroll)

        self._save_logs = QCheckBox("Save the log automatically on exit", recording)
        form.addRow(self._save_logs)

        self._log_file = QLineEdit(recording)
        self._log_file.setPlaceholderText("Beside the application (flash_log.txt)")
        self._log_file.setToolTip(
            "Leave empty to write flash_log.txt next to the application."
        )
        browse = QPushButton("Browse…", recording)
        browse.clicked.connect(self._on_pick_log_file)
        row = QHBoxLayout()
        row.addWidget(self._log_file, 1)
        row.addWidget(browse)
        form.addRow("Log file", row)

        self._log_max = QSpinBox(recording)
        self._log_max.setRange(0, 1024)
        self._log_max.setSuffix(" MB")
        self._log_max.setSpecialValueText("No rotation")
        self._log_max.setToolTip(
            "Once the log reaches this size it is renamed to flash_log.1.txt and a "
            "fresh one is started, so there is always a current log and one previous."
        )
        form.addRow("Rotate after", self._log_max)

        layout.addWidget(recording)

        note = QLabel(
            "The log is the first thing to look at after a failure, and the first "
            "thing support will ask for. At debug level it records every packet, "
            "which is what makes a protocol problem diagnosable - and also what makes "
            "it large.",
            page,
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {COLORS['text_dim']};")
        layout.addWidget(note)

        layout.addStretch(1)
        return self._wrap_scrollable(page)

    # -----------------------------------------------------------------------
    #  Helpers
    # -----------------------------------------------------------------------

    def _build_tools_page(self) -> QWidget:
        """Where the external binaries are, and where the files go.

        The paths are preferences rather than overrides, and the page says so:
        an empty box is the normal state and means "look in the usual places".
        The detection line below shows what was actually found, which is the
        question behind the setting - not "have I typed a path" but "will this
        machine's adb be the one that runs".
        """
        page = QWidget(self)
        layout = QVBoxLayout(page)

        android = QGroupBox("Android platform tools", page)
        form = QFormLayout(android)

        self._adb_path = self._path_field(
            android,
            "adb",
            "Leave empty to search PATH, ANDROID_HOME, the usual SDK folders and the "
            "copy shipped beside this tool.\nSet it when several versions are "
            "installed and the one on PATH is not the one you mean.",
        )
        form.addRow("adb", self._adb_path)

        self._fastboot_path = self._path_field(
            android,
            "fastboot",
            "The same search as adb when empty. Fastboot and adb are usually from the "
            "same platform-tools download, so the two fields are normally both empty "
            "or both set to the same folder.",
        )
        form.addRow("fastboot", self._fastboot_path)

        self._tools_status = QLabel("", android)
        self._tools_status.setWordWrap(True)
        self._tools_status.setStyleSheet(f"color: {COLORS['text_dim']};")
        form.addRow(self._tools_status)

        detect = QPushButton("Check what is installed", android)
        detect.setToolTip(
            "Looks for both tools now and reports what it found, without saving."
        )
        detect.clicked.connect(safe_slot(self._on_detect_tools))
        form.addRow("", detect)
        layout.addWidget(android)

        folders = QGroupBox("Files", page)
        folder_form = QFormLayout(folders)
        # Shown, not editable. The log file is set on the Logging page, and a
        # second editor for one setting is how the two end up disagreeing.
        self._log_location = QLabel("", folders)
        self._log_location.setWordWrap(True)
        self._log_location.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._log_location.setStyleSheet(f"color: {COLORS['text_dim']};")
        folder_form.addRow("Log file", self._log_location)

        open_log = QPushButton("Open the log's folder", folders)
        open_log.clicked.connect(safe_slot(self._on_open_log_folder))
        folder_form.addRow("", open_log)

        reveal = QPushButton("Open the folder holding settings.json", folders)
        reveal.clicked.connect(safe_slot(self._on_reveal))
        folder_form.addRow("", reveal)
        layout.addWidget(folders)

        layout.addStretch(1)
        self._refresh_tools_status()
        return self._wrap_scrollable(page)

    def _build_advanced_page(self) -> QWidget:
        """The settings that are only useful when something specific is wrong.

        Everything here has a good default and a reason to exist, which is the
        test for whether it belongs on this page rather than on one of the
        others: an operator arrives here because of a particular problem, not to
        configure the tool.
        """
        page = QWidget(self)
        layout = QVBoxLayout(page)

        throughput = QGroupBox("Transfer rate", page)
        form = QFormLayout(throughput)
        self._speed_limit = self._whole_box(
            throughput,
            "speed_limit",
            "A ceiling on how fast data is written, in kilobytes per second.\n"
            "Zero means no limit. There is one real reason to set this: a device "
            "that is only stable on a long cable or through an unpowered hub, where "
            "flashing faster than the link can carry ends in a checksum failure "
            "rather than a clean error.",
            suffix=" KB/s",
            zero_text="No limit",
        )
        # Stored in bytes; shown in kilobytes, because nobody types 262144.
        form.addRow("Speed limit", self._speed_limit)
        layout.addWidget(throughput)

        diagnostics = QGroupBox("Diagnostics", page)
        diagnostic_form = QFormLayout(diagnostics)

        reset_tools = QPushButton("Re-detect adb and fastboot", diagnostics)
        reset_tools.setToolTip("Forgets the cached toolchain and looks again.")
        reset_tools.clicked.connect(safe_slot(self._on_reset_tools))
        diagnostic_form.addRow("Tool discovery", reset_tools)

        forget = QPushButton("Forget the recent files list", diagnostics)
        forget.setToolTip(
            "Clears the file names the file browser offers on its recent list. No "
            "file is deleted - only the list of names is."
        )
        forget.clicked.connect(safe_slot(self._on_forget_recents))
        diagnostic_form.addRow("File browser", forget)

        self._recent_status = QLabel("", diagnostics)
        self._recent_status.setStyleSheet(f"color: {COLORS['text_dim']};")
        diagnostic_form.addRow("", self._recent_status)
        layout.addWidget(diagnostics)

        note = QLabel(
            "Changing a setting here takes effect on the next operation. Anything "
            "already running keeps the values it started with, because a transfer "
            "that changed its own timeout halfway through would be impossible to "
            "reason about afterwards.",
            page,
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {COLORS['text_dim']};")
        layout.addWidget(note)

        layout.addStretch(1)
        self._refresh_recent_status()
        return self._wrap_scrollable(page)

    # -----------------------------------------------------------------------
    #  Helpers
    # -----------------------------------------------------------------------

    def _path_field(self, parent: QWidget, tool: str, tooltip: str) -> QWidget:
        """A path box with a Browse button, shared by the two tool paths.

        Returns the row container, and the line edit is fetched back by name in
        `load()`/`collected()` - a form row holds one widget, and the pair has to
        be that one widget.
        """
        holder = QWidget(parent)
        line = QLineEdit(holder)
        line.setPlaceholderText(f"Search for {tool} automatically")
        line.setToolTip(tooltip)
        button = QPushButton("Browse…", holder)
        button.clicked.connect(safe_slot(lambda: self._on_pick_binary(line, tool)))
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(line, 1)
        row.addWidget(button)
        setattr(self, f"_{tool}_field", line)
        return holder

    # -- tool discovery, shown live ----------------------------------------

    def _refresh_tools_status(self) -> None:
        """Reports what the toolchain resolves to right now.

        A setting that says "leave this empty" is only usable if the operator can
        see what empty resolves to, so this runs the same discovery the
        application runs rather than describing it.
        """
        label = getattr(self, "_tools_status", None)
        if label is None:
            return
        try:
            from huaxin.core.adb_fastboot_wrapper import AndroidTools

            tools = AndroidTools(
                self._adb_field.text().strip() or None,
                self._fastboot_field.text().strip() or None,
            )
            adb = tools.adb_path or tools._find("adb")
            fastboot = tools.fastboot_path or tools._find("fastboot")
        except Exception as exc:  # a broken import must not break the dialog
            label.setText(f"Could not look for the tools: {exc}")
            return

        found = []
        found.append(f"adb: {adb}" if adb else "adb: not found")
        found.append(f"fastboot: {fastboot}" if fastboot else "fastboot: not found")
        label.setText("\n".join(found))

    def _refresh_recent_status(self) -> None:
        label = getattr(self, "_recent_status", None)
        if label is None:
            return
        try:
            from huaxin.ui import filedialog

            count = len(filedialog.recent_files())
        except Exception:  # pragma: no cover - the module is always importable
            count = 0
        label.setText(f"{count} file name{'s' if count != 1 else ''} remembered")

    def _on_pick_binary(self, line: QLineEdit, tool: str) -> None:
        """Picks a binary. Uses the themed browser rather than the native dialog."""
        start = line.text().strip() or str(Path.home())
        suffix = ".exe" if sys.platform.startswith("win") else ""
        chosen, _ = filedialog.get_open_file_name(
            self,
            title=f"Select the {tool} binary",
            start=start,
            filters=((f"{tool} executable", (f"{tool}{suffix}", f"*{suffix}", "*")),
                     ("All files", ("*",))),
        )
        if chosen:
            line.setText(chosen)
            self._refresh_tools_status()

    def _on_detect_tools(self) -> None:
        self._refresh_tools_status()

    def _on_reset_tools(self) -> None:
        from huaxin.core import adb_fastboot_wrapper

        adb_fastboot_wrapper.reset_default_tools()
        self._refresh_tools_status()
        self._recent_status.setText("Cached toolchain forgotten — it will be looked up again.")

    def _on_forget_recents(self) -> None:
        from huaxin.ui import filedialog

        filedialog.forget_recent_files()
        self._refresh_recent_status()

    def _on_open_log_folder(self) -> None:
        target = Path(self._log_file.text().strip() or str(default_log_path()))
        if not open_in_file_manager(target):
            QMessageBox.information(
                self,
                "Log folder",
                f"This platform has no file manager to open.\n\nThe log is at:\n{target}",
            )

    def _seconds_box(self, field_name: str, tooltip: str) -> QDoubleSpinBox:
        """A seconds field bounded by the same numbers the validator uses.

        Taking the bounds from `NUMBER_BOUNDS` rather than restating them here is
        what stops the dialog accepting a value the validator would then quietly
        change behind the operator's back.
        """
        low, high = NUMBER_BOUNDS[field_name]
        box = QDoubleSpinBox(self)
        box.setRange(low, high)
        box.setDecimals(1)
        box.setSuffix(" s")
        box.setToolTip(tooltip)
        return box

    def _whole_box(
        self,
        parent: QWidget,
        field_name: str,
        tooltip: str,
        *,
        suffix: str = "",
        zero_text: str = "",
    ) -> QSpinBox:
        """A whole-number field, with the zero value optionally named."""
        low, high = NUMBER_BOUNDS[field_name]
        box = QSpinBox(parent)
        box.setRange(int(low), int(high))
        if suffix:
            box.setSuffix(suffix)
        if zero_text and low == 0:
            box.setSpecialValueText(zero_text)
        box.setToolTip(tooltip)
        return box

    @staticmethod
    def _wrap_scrollable(page: QWidget) -> QWidget:
        """Puts a page in a scroll area.

        Without it, a laptop at 1366x768 with the display scaled has its OK
        button pushed off the bottom of the dialog.
        """
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(page)
        return scroll

    # -----------------------------------------------------------------------
    #  Loading and collecting
    # -----------------------------------------------------------------------

    def load(self, settings: Settings) -> None:
        """Fills every control from `settings`."""
        self._settings = settings

        self._confirm.setChecked(settings.confirm_destructive_operations)
        self._scan_on_startup.setChecked(settings.scan_on_startup)
        self._auto_scan.setValue(settings.auto_scan_seconds)
        self._read_strings.setChecked(settings.read_usb_strings)
        self._show_hubs.setChecked(settings.show_root_hubs)

        if not self._theme.setCurrentValue(settings.theme):
            # An unknown theme cannot be displayed honestly, so the dialog says
            # so rather than silently showing the first entry.
            self._theme.setCurrentIndex(0)
        self._animations.setChecked(settings.animations)

        self._command_timeout.setValue(settings.default_timeout)
        self._transfer_timeout.setValue(settings.transfer_timeout)
        self._connect_timeout.setValue(settings.connect_timeout)

        self._attempts.setValue(settings.max_retry_attempts)
        self._initial_delay.setValue(settings.retry_initial_delay_ms)
        self._max_delay.setValue(settings.retry_max_delay_ms)
        self._budget.setValue(settings.retry_total_budget_s)
        # Stored in bytes per second, shown in kilobytes per second.
        self._speed_limit.setValue(int(settings.speed_limit // 1024))

        self._adb_field.setText(settings.adb_path)
        self._fastboot_field.setText(settings.fastboot_path)

        level_index = self._log_level.findData(settings.log_level)
        self._log_level.setCurrentIndex(level_index if level_index >= 0 else 1)
        self._log_to_file.setChecked(settings.log_to_file)
        self._log_to_console.setChecked(settings.log_to_console)
        self._auto_scroll.setChecked(settings.auto_scroll_log)
        self._save_logs.setChecked(settings.save_logs_automatically)
        self._log_file.setText(settings.log_file)
        # Displayed in whole megabytes; the file stores bytes.
        self._log_max.setValue(int(settings.log_max_bytes // (1024 * 1024)))

        self._log_location.setText(
            settings.log_file.strip() or f"{default_log_path()}  (default)"
        )
        self._refresh_tools_status()
        self._refresh_recent_status()

    def collected(self) -> Settings:
        """A Settings built from the controls, validated by `sanitised()`."""
        updated = Settings(**self._settings.to_dict())
        updated.confirm_destructive_operations = self._confirm.isChecked()
        updated.scan_on_startup = self._scan_on_startup.isChecked()
        updated.auto_scan_seconds = self._auto_scan.value()
        updated.read_usb_strings = self._read_strings.isChecked()
        updated.show_root_hubs = self._show_hubs.isChecked()

        updated.theme = str(self._theme.currentData() or self._settings.theme)
        updated.animations = self._animations.isChecked()

        updated.default_timeout = self._command_timeout.value()
        updated.transfer_timeout = self._transfer_timeout.value()
        updated.connect_timeout = self._connect_timeout.value()

        updated.max_retry_attempts = self._attempts.value()
        updated.retry_initial_delay_ms = self._initial_delay.value()
        updated.retry_max_delay_ms = self._max_delay.value()
        updated.retry_total_budget_s = self._budget.value()
        updated.speed_limit = self._speed_limit.value() * 1024

        updated.adb_path = self._adb_field.text().strip()
        updated.fastboot_path = self._fastboot_field.text().strip()

        updated.log_level = str(self._log_level.currentData())
        updated.log_to_file = self._log_to_file.isChecked()
        updated.log_to_console = self._log_to_console.isChecked()
        updated.auto_scroll_log = self._auto_scroll.isChecked()
        updated.save_logs_automatically = self._save_logs.isChecked()
        updated.log_file = self._log_file.text().strip()
        updated.log_max_bytes = self._log_max.value() * 1024 * 1024

        return updated.sanitised()

    @property
    def result_settings(self) -> Settings | None:
        """What was accepted, or None when the dialog was cancelled."""
        return self._result

    # -----------------------------------------------------------------------
    #  Slots
    # -----------------------------------------------------------------------

    def accept(self) -> None:
        """Validates and stores, so a bad combination cannot get past the dialog."""
        collected = self.collected()
        if collected.retry_max_delay_ms < collected.retry_initial_delay_ms:
            QMessageBox.warning(
                self,
                "Retry delays",
                "The maximum delay is shorter than the first delay, which would make "
                "the backoff stop mattering. Raise the maximum or lower the first.",
            )
            return
        missing = [
            f"{name}: {value}"
            for name, value in (("adb", collected.adb_path), ("fastboot", collected.fastboot_path))
            if value and not Path(value).is_file()
        ]
        if missing:
            # A warning rather than a rejection: the drive holding the tools may
            # simply be unplugged right now, and refusing to save would trap the
            # operator in the dialog with a setting they cannot correct.
            proceed = QMessageBox.question(
                self,
                "That file is not there",
                "These paths do not exist:\n\n"
                + "\n".join(missing)
                + "\n\nThe tool will fall back to searching PATH and the usual Android "
                "SDK folders until the path is right.\n\nSave anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if proceed != QMessageBox.StandardButton.Yes:
                return
        if not collected.log_to_file and not collected.log_to_console:
            proceed = QMessageBox.question(
                self,
                "Nothing will be recorded",
                "Both the log file and the console mirror are turned off, so a "
                "failure will leave no record of what happened.\n\nSave anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if proceed != QMessageBox.StandardButton.Yes:
                return
        self._result = collected
        super().accept()

    def _on_pick_log_file(self) -> None:
        start = self._log_file.text().strip() or str(Path.home())
        chosen = filedialog.get_save_file_name(
            self,
            title="Where the log is written",
            start=start,
            name="flash_log.txt",
            filters=(("Log files", ("*.txt", "*.log")), ("All files", ("*",))),
        )
        if chosen:
            self._log_file.setText(chosen)
            self._log_location.setText(chosen)

    def _on_reveal(self) -> None:
        if not open_in_file_manager(settings_path()):
            QMessageBox.information(
                self,
                "Settings file",
                f"This platform has no file manager to open.\n\nThe settings file is at:\n"
                f"{settings_path()}",
            )

    def _on_restore(self) -> None:
        confirm = QMessageBox.question(
            self,
            "Restore defaults",
            "Reset every setting on this page to the value the tool ships with?\n\n"
            "The file is not written until you press OK.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self.load(Settings())


def show_settings_dialog(
    settings: Settings,
    parent: QWidget | None = None,
    *,
    page: str | None = None,
) -> Settings | None:
    """Runs the dialog, saving and returning the settings when accepted.

    Returns None when cancelled. Saving happens here rather than in the caller so
    a caller cannot forget it and leave the operator wondering why their change
    did not survive a restart.

    `page` opens the dialog on a named category - "tools" for a missing adb, so
    the fix is one click from the failure rather than a hunt through six
    categories for the box that takes a path.
    """
    dialog = SettingsDialog(settings, parent)
    dialog.load(settings)
    if page:
        dialog.show_page(page)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    accepted = dialog.result_settings
    if accepted is None:  # pragma: no cover - accept() always sets it
        return None
    save_settings(accepted)
    return accepted


class DriverHelpDialog(QDialog):
    """Where to get the USB driver for each vendor, and what to do when it is wrong.

    The advice is per vendor because the drivers are: a Qualcomm target needs a
    different binding from a MediaTek one, and the folder that ships with this
    tool holds them under one roof.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("USB driver help")
        self.setMinimumWidth(640)

        layout = QVBoxLayout(self)
        heading = QLabel(
            "<b>A device that shows a warning triangle in Device Manager will never "
            "be flashed.</b><br>"
            "Windows binds a different driver to each mode, so a phone that works over "
            "ADB may still need a new binding once it is in flash mode."
        )
        heading.setWordWrap(True)
        layout.addWidget(heading)

        rows = QGroupBox("What each device needs", self)
        form = QFormLayout(rows)
        for vendor, need in (
            ("Qualcomm (9008)", "QDLoader / WinUSB. Install the Qualcomm driver from "
                                "drivers\\qualcomm, or bind WinUSB with drivers\\zadig.exe."),
            ("MediaTek (BROM)", "MediaTek USB VCOM / WinUSB, from drivers\\mediatek. "
                                "The preloader and BROM modes expose different IDs and "
                                "need separate bindings."),
            ("Unisoc (Research Download)", "Spreadtrum/Unisoc USB driver, from "
                                           "drivers\\unisoc."),
            ("Samsung (Download mode)", "Samsung USB driver, from drivers\\samsung."),
            ("ADB / Fastboot", "Google's USB driver, or WinUSB bound with Zadig."),
        ):
            label = QLabel(need)
            label.setWordWrap(True)
            form.addRow(f"{vendor}", label)
        layout.addWidget(rows)

        warning = QLabel(
            "Zadig replaces the driver bound to the selected interface. Bind the "
            "flash-mode interface only, and confirm you have the right device selected: "
            "replacing a keyboard or a mouse driver leaves the machine hard to use until "
            "it is put back."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(f"color: {COLORS['warn']};")
        layout.addWidget(warning)

        detail = QLabel(
            "Full instructions, including how to check which driver is bound and how to "
            "put it back, are in drivers/README.md. The batch script in drivers\\ offers "
            "the same steps one at a time."
        )
        detail.setWordWrap(True)
        detail.setStyleSheet(f"color: {COLORS['text_dim']};")
        layout.addWidget(detail)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
