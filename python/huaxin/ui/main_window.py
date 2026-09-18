"""Application main window: toolbar, vendor tabs, device dock and log console.

This is where the pieces the task calls "advanced" meet: the toast stack, the
live operation readout in the status bar, drag-and-drop onto the tabs, the
lazily-built vendor panels, and the keyboard. They are all in this file because
they are all about the *window* rather than about any one tab - each has an
interface that a panel can use without knowing this file exists.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from PyQt6.QtCore import QSettings, QSize, Qt
from PyQt6.QtGui import QAction, QCloseEvent, QDragEnterEvent, QDropEvent, QKeySequence
from PyQt6.QtWidgets import (
    QDockWidget,
    QLabel,
    QMessageBox,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from huaxin.core import progress as progress_module
from huaxin.core.backend import BackendService, Device
from huaxin.core.config import (
    Settings,
    default_log_path,
    load_settings,
    save_settings,
    set_active_settings,
    settings_path,
)
from huaxin.ui import animations, components as ui
from huaxin.ui import filedialog, icons, style, theme_controller, tokens, toasts
from huaxin.ui.about import AboutDialog
from huaxin.ui.opstatus import OperationStatus
from huaxin.ui.panels import (
    AndroidPanel,
    MediaTekPanel,
    QualcommPanel,
    SamsungPanel,
    SpdPanel,
    VendorPanel,
)
from huaxin.ui.safe_slot import safe_slot
from huaxin.ui.settings_dialog import (
    DriverHelpDialog,
    open_in_file_manager,
    show_settings_dialog,
)
from huaxin.ui.theme import COLORS
from huaxin.ui.titlebar import FramelessWindow
from huaxin.ui.widgets import DevicePanel, LogConsole

__all__ = ["MainWindow"]

#: Backend state -> (chip tone, text). Tones are the ones the stylesheet
#: defines, so a new state cannot invent a colour of its own.
_STATE_CHIP = {
    "stopped": ("neutral", "● Backend stopped"),
    "starting": ("warn", "● Backend starting…"),
    "ready": ("ok", "● Backend ready"),
    "unavailable": ("error", "● Backend unavailable"),
}

_DEFAULT_SIZE = (1360, 860)
_DEFAULT_DEVICE_DOCK_WIDTH = 420
_DEFAULT_LOG_DOCK_HEIGHT = 220

#: The shortest the log dock may ever be dragged to. A log strip two lines tall is
#: the first thing an operator complains about and the last thing they think to
#: resize, so it has a floor rather than only a default.
_LOG_DOCK_MIN_HEIGHT = 260

#: (key, panel class). The order is the tab order, and the class is built on
#: first visit - see `_ensure_panel`. The tab's label and icon are read from the
#: class's own `tab_name` and `tab_icon` rather than repeated here, so a panel
#: cannot end up labelled one thing in the tab bar and another in its header.
_PANEL_TYPES: tuple[tuple[str, type[VendorPanel]], ...] = (
    ("android", AndroidPanel),
    ("qualcomm", QualcommPanel),
    ("mediatek", MediaTekPanel),
    ("spd", SpdPanel),
    ("samsung", SamsungPanel),
)

#: Which panel takes which dropped file, by suffix. Longest suffix first, because
#: `file.tar.md5` ends with both `.md5` and `.tar.md5` and the longer one is the
#: one that identifies the format.
_DROP_ROUTES: tuple[tuple[tuple[str, ...], str], ...] = (
    ((".tar.md5", ".tar"), "samsung"),
    ((".pit",), "samsung"),
    ((".pac",), "spd"),
)

#: Panel key -> tab label, for the messages that name where a file is going.
_PANEL_LABELS = {key: cls.tab_name for key, cls in _PANEL_TYPES}


def _tab_label(index: int) -> str:
    """The label for a tab, from the panel class's own `tab_name`."""
    return _PANEL_TYPES[index][1].tab_name


def _tab_icon(index: int) -> str:
    """The icon *name* for a tab, from the panel class's own `tab_icon`."""
    return getattr(_PANEL_TYPES[index][1], "tab_icon", "chip")


class MainWindow(FramelessWindow):
    """Top level window.

    The window never touches the native bridge: it asks `BackendService` for
    work and reacts to its signals. That is what keeps the UI thread free while
    a device operation is in flight.

    Every slot connected to a signal is wrapped in `safe_slot`, because an
    exception escaping a Qt slot aborts the process in PyQt6.
    """

    def __init__(self, service: BackendService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = service
        #: Built panels, by key. Populated on first visit to each tab.
        self._panels: dict[str, VendorPanel] = {}
        #: Tab index -> panel key, and the reverse.
        self._tab_keys: list[str] = []
        #: Whether a panel built later should start with a device selected.
        self._selected_device: Device | None = None
        self._settings = QSettings()
        # Created on first use; see _on_gallery.
        self._gallery = None
        # Tracks which devices were present at the last scan, so arrival and
        # departure can be announced rather than re-announced on every scan.
        self._known_devices: set[str] = set()
        self._scanned_once = False

        # The tool's own settings, read once at startup. A malformed file is
        # reported through the console rather than stopping the window from
        # opening - see huaxin.core.config.
        self.settings, self._settings_notes = load_settings()
        set_active_settings(self.settings)
        # A scan that has not been asked for yet. Cleared once one has been
        # started, so a settings change that re-triggers "ready" does not queue a
        # second scan.
        self._startup_scan_pending = bool(self.settings.scan_on_startup)

        self.setWindowTitle("Huaxin Tool — Multi-Vendor Android Firmware Suite")
        self.titlebar.setTitle("Huaxin Tool")
        self.titlebar.setSubtitle("— Multi-Vendor Android Firmware Suite")
        self.resize(*_DEFAULT_SIZE)
        self.setMinimumSize(tokens.METRICS.window_min_width, tokens.METRICS.window_min_height)
        # Firmware is dragged onto this window from a file manager, which is how
        # anybody who has just downloaded a package actually opens it.
        self.setAcceptDrops(True)

        # The theme picker sits in the title bar rather than in Settings: a theme
        # is something an operator tries, and having to open a dialog and press
        # OK to see what Daylight looks like means nobody ever does.
        self.themes = theme_controller.ThemeController()
        self._theme_picker = theme_controller.theme_picker(self.themes, self.titlebar)
        self.titlebar.add_widget(self._theme_picker)
        self.themes.on_change(self._on_theme_changed)

        self._build_toolbar()
        self._build_menu()
        self._build_central()
        self._build_docks()
        self._build_status_bar()
        self._build_drop_hint()
        self._wire()
        self._restore_layout()

        # Created last: the toast stack anchors itself to the bottom-right corner
        # and needs the status bar to know how much room that is.
        self._toasts = toasts.ToastManager(self, bottom_offset=self.statusBar().height())

        for note in self._settings_notes:
            self._console.append("warning", note)
        self._console.set_auto_scroll(self.settings.auto_scroll_log)

    # -- construction ------------------------------------------------------

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main", self)
        toolbar.setObjectName("MainToolBar")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(16, 16))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(toolbar)

        self._action_scan = QAction("⟳  Scan Devices", self)
        self._action_scan.setShortcut(QKeySequence("F5"))
        self._action_scan.setStatusTip("Enumerate attached devices through the native backend")
        self._action_scan.triggered.connect(safe_slot(self._on_scan))

        self._action_cancel = QAction("✕  Cancel Operation", self)
        # Esc is the key everybody presses to stop something, and Ctrl+. is the
        # one Qt has used for the same job for twenty years. Both work; Esc is
        # listed because it is the one nobody has to be told.
        self._action_cancel.setShortcuts(
            [QKeySequence("Esc"), QKeySequence("Ctrl+.")]
        )
        self._action_cancel.setEnabled(False)
        self._action_cancel.setStatusTip("Ask the running operation to stop")
        self._action_cancel.triggered.connect(safe_slot(self._on_cancel))

        self._action_shutdown = QAction("⏻  Shut Down Backend", self)
        self._action_shutdown.setStatusTip("Release the USB context and stop the worker thread")
        self._action_shutdown.triggered.connect(safe_slot(self._on_shutdown_backend))

        self._action_save_log = QAction("⤓  Save Log…", self)
        self._action_save_log.setShortcut(QKeySequence("Ctrl+S"))
        self._action_save_log.setStatusTip("Write the console's contents to a text file")
        self._action_save_log.triggered.connect(safe_slot(self._on_save_log))

        self._action_clear_log = QAction("🗑  Clear Log", self)
        self._action_clear_log.setShortcut(QKeySequence("Ctrl+L"))
        self._action_clear_log.setStatusTip("Clear the log console")
        self._action_clear_log.triggered.connect(safe_slot(self._on_clear_log))

        self._action_reset_layout = QAction("⤢  Reset Layout", self)
        self._action_reset_layout.setStatusTip("Restore the default window and dock layout")
        self._action_reset_layout.triggered.connect(safe_slot(self._on_reset_layout))

        toolbar.addAction(self._action_scan)
        toolbar.addAction(self._action_cancel)
        toolbar.addSeparator()
        toolbar.addAction(self._action_shutdown)
        toolbar.addSeparator()
        toolbar.addAction(self._action_save_log)
        toolbar.addAction(self._action_clear_log)
        toolbar.addAction(self._action_reset_layout)

        self._toolbar = toolbar

    def _build_menu(self) -> None:
        # The menus reference the same QAction objects as the toolbar, so
        # shortcuts, enablement and text stay in sync automatically - including
        # the shortcut text Qt prints beside each entry, which is why every
        # shortcut is documented here rather than in a help page nobody opens.
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(self._action_scan)
        file_menu.addSeparator()
        file_menu.addAction(self._action_save_log)
        file_menu.addAction(self._action_clear_log)
        file_menu.addSeparator()
        settings_action = QAction("&Settings…", self)
        settings_action.setShortcut(QKeySequence("Ctrl+,"))
        settings_action.setStatusTip("Timeouts, retries, logging and behaviour.")
        # Wrapped so the action's bool never reaches `page`: `triggered` always
        # sends one, and a `False` arriving where a category key belongs is the
        # kind of thing that works until somebody renames a page.
        settings_action.triggered.connect(safe_slot(lambda: self._on_settings()))
        file_menu.addAction(settings_action)
        file_menu.addSeparator()
        quit_action = QAction("E&xit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.setStatusTip("Close the tool. A running operation is cancelled first.")
        quit_action.triggered.connect(safe_slot(self.close))
        file_menu.addAction(quit_action)

        self._view_menu = self.menuBar().addMenu("&View")

        # One entry per vendor tab, with the shortcut that jumps to it. Alt+1..5
        # is what makes the tab bar usable without a mouse, and it costs one
        # line here rather than a key handler per panel.
        self._view_menu.addSeparator()
        for index, (key, _) in enumerate(_PANEL_TYPES):
            action = QAction(f"&{index + 1} {_tab_label(index)}", self)
            action.setShortcut(QKeySequence(f"Alt+{index + 1}"))
            action.setStatusTip(f"Switch to the {_tab_label(index)} tab")
            action.triggered.connect(safe_slot(partial(self._on_show_tab, index)))
            self._view_menu.addAction(action)

        help_menu = self.menuBar().addMenu("&Help")
        driver_action = QAction("USB &driver help", self)
        driver_action.setStatusTip("Which driver each vendor's flash mode needs.")
        driver_action.triggered.connect(safe_slot(self._on_driver_help))
        help_menu.addAction(driver_action)
        gallery_action = QAction("&Component gallery", self)
        gallery_action.setStatusTip("Every button, input and indicator in one window.")
        gallery_action.triggered.connect(safe_slot(self._on_gallery))
        help_menu.addAction(gallery_action)
        log_action = QAction("Open log &folder", self)
        log_action.setStatusTip("Reveal the folder holding the flash log.")
        log_action.triggered.connect(safe_slot(self._on_open_log_folder))
        help_menu.addAction(log_action)
        help_menu.addSeparator()
        about_action = QAction("&About", self)
        about_action.setShortcut(QKeySequence("F1"))
        about_action.setStatusTip("Version, build details and what each vendor's support amounts to.")
        about_action.triggered.connect(safe_slot(self._on_about))
        help_menu.addAction(about_action)

    def _build_central(self) -> None:
        """Builds the tab bar with placeholder tabs.

        The panels are *not* built here. Each one is a few hundred widgets and a
        handful of tables, and building all five before the window is shown costs
        the time it takes to construct four screens the operator may never look
        at. A placeholder goes in instead and is swapped on the first visit - see
        `_ensure_panel`, which is also what makes the drop and the Alt+N shortcuts
        work on a tab that has never been opened.
        """
        self._tabs = ui.StyledTabWidget(self)

        for index, (key, _) in enumerate(_PANEL_TYPES):
            placeholder = QWidget(self)
            self._tabs.addIconTab(placeholder, _tab_label(index), _tab_icon(index))
            self._tab_keys.append(key)
            # The first tab is what the window opens on, so it is built now:
            # deferring it would trade a real cost for one that lands on the
            # first paint instead.
            if len(self._tab_keys) == 1:
                self._ensure_panel(0)

        self._tabs.currentChanged.connect(safe_slot(self._on_tab_changed))

        # Inside the frameless window's content area, below the title bar -
        # `setCentralWidget` would put the tabs behind the custom chrome.
        content = QVBoxLayout()
        content.setContentsMargins(8, 8, 8, 4)
        content.setSpacing(0)
        content.addWidget(self._tabs)
        self.set_content_layout(content)

    def _build_drop_hint(self) -> None:
        """The overlay shown while a file is dragged over the window.

        A child of the window rather than a dialog: it has to appear the moment
        the drag crosses the edge, and it must not take the drag events itself -
        which is why it is transparent to the mouse.
        """
        self._drop_hint = QLabel("", self)
        self._drop_hint.setObjectName("DropHint")
        self._drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._drop_hint.setVisible(False)

    def _build_docks(self) -> None:
        """Builds the two docks, fixed in place.

        The docks are deliberately not movable and not floatable. A panel that
        can be torn off and left floating over the tabs is a panel somebody drags
        away by accident and then cannot find again, and every tool in this market
        keeps its device list and its log where they are. The cost is stated
        rather than hidden: Qt *disables* a dock's View-menu toggle when the dock
        cannot be closed, so those two menu entries are not added at all - a menu
        item that looks clickable and does nothing is worse than no menu item.
        **Reset Layout** is the way back if a dock ends up hidden.
        """
        self._device_panel = DevicePanel(self)
        devices_dock = QDockWidget("Devices", self)
        devices_dock.setObjectName("DevicesDock")
        devices_dock.setWidget(self._device_panel)
        devices_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, devices_dock)

        self._console = LogConsole(self)
        log_dock = QDockWidget("Log", self)
        log_dock.setObjectName("LogDock")
        log_dock.setWidget(self._console)
        log_dock.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.TopDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, log_dock)

        # Both docks, once, after both exist - each is locked against dragging,
        # floating and closing. The log also gets a floor on its height: a log
        # strip two lines tall is the first thing an operator complains about and
        # the last thing they think to resize.
        for dock in (devices_dock, log_dock):
            dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
            dock.setFloating(False)
        log_dock.setMinimumHeight(_LOG_DOCK_MIN_HEIGHT)

        self._devices_dock = devices_dock
        self._log_dock = log_dock
        self._view_menu.addSeparator()
        self._view_menu.addAction(self._action_reset_layout)

    def _build_status_bar(self) -> None:
        bar = self.statusBar()

        self._state_chip = ui.Chip("● Stopped", tone="neutral")

        self._device_count = QLabel("0 devices", self)
        self._device_count.setToolTip("Attached USB devices. F5 scans again.")

        # The live readout replaces the old indeterminate bar: it says what is
        # running, how fast, for how long and how much is left, where a bar could
        # only say "something is happening".
        self._operation = OperationStatus(self)

        bar.addPermanentWidget(self._operation)
        bar.addPermanentWidget(self._device_count)
        bar.addPermanentWidget(self._state_chip)
        bar.showMessage("Ready. Run Scan Devices (F5) to enumerate attached hardware.")

        self._apply_chip_style("stopped")

    # -- wiring ------------------------------------------------------------

    def _wire(self) -> None:
        service = self._service

        service.log.connect(safe_slot(self._console.append))
        service.backend_state_changed.connect(safe_slot(self._on_state_changed))
        service.busy_changed.connect(safe_slot(self._on_busy_changed))
        service.devices_changed.connect(safe_slot(self._on_devices_changed))
        service.scan_started.connect(safe_slot(self._on_scan_started))
        service.scan_finished.connect(safe_slot(self._on_scan_finished))
        service.job_started.connect(safe_slot(self._on_job_started))
        service.job_progress_changed.connect(safe_slot(self._on_job_progress))
        service.job_progress_detail.connect(safe_slot(self._on_job_progress_detail))
        service.job_finished.connect(safe_slot(self._on_job_finished))
        service.job_failed.connect(safe_slot(self._on_job_failed))

        self._device_panel.selection_changed.connect(safe_slot(self._on_device_selected))
        self._device_panel.scan_requested.connect(safe_slot(self._on_scan))

    # -- panels ------------------------------------------------------------

    def _ensure_panel(self, index: int) -> VendorPanel | None:
        """Builds the panel for a tab on first use, and returns it.

        Called from the tab handler, the drop handler and the Alt+N shortcuts, so
        every route into a tab goes through one place - a lazily-built panel that
        only one of those routes built would be a panel that is missing state
        depending on how it was reached.
        """
        if not 0 <= index < len(self._tab_keys):
            return None
        key = self._tab_keys[index]
        panel = self._panels.get(key)
        if panel is not None:
            return panel

        panel_type = _PANEL_TYPES[index][1]
        panel = panel_type(self._service, self._tabs)
        self._panels[key] = panel

        # Swapped in place: the placeholder is removed with deleteLater so Qt does
        # not free the widget the tab bar is currently painting.
        placeholder = self._tabs.widget(index)
        self._tabs.removeTab(index)
        self._tabs.insertTab(index, panel, _tab_label(index))
        self._tabs.setTabIcon(index, icons.icon(_tab_icon(index),
                                                tokens.active_theme().text_muted,
                                                tokens.METRICS.icon_size))
        self._tabs.setCurrentIndex(index)
        if placeholder is not None:
            placeholder.deleteLater()

        # A panel built after a device was already chosen must not come up with
        # an empty device box while the others show one.
        if self._selected_device is not None:
            panel.set_selected_device(self._selected_device)
        panel.refresh_actions()
        self._service.log_message("debug", f"built the {_tab_label(index)} tab on first use")
        return panel

    def panel(self, key: str) -> VendorPanel | None:
        """The built panel for a key, or None when its tab has never been opened."""
        return self._panels.get(key)

    def _on_tab_changed(self, index: int) -> None:
        self._ensure_panel(index)

    def _on_show_tab(self, index: int) -> None:
        self._ensure_panel(index)
        self._tabs.setCurrentIndex(index)

    # -- slots -------------------------------------------------------------

    def _on_scan(self) -> None:
        self._service.request_scan(
            read_string_descriptors=self.settings.read_usb_strings,
            include_root_hubs=self.settings.show_root_hubs,
        )

    def _on_cancel(self) -> None:
        self._service.cancel_current()

    def _on_clear_log(self) -> None:
        self._console.clear()

    def _on_save_log(self) -> None:
        """Ctrl+S, and the toolbar button. Delegates to the console's own save."""
        self._console.save_to_file()

    # -- the operation readout ---------------------------------------------

    def _on_job_started(self, job_name: str) -> None:
        """Names the operation before it reports anything.

        A job that takes ten seconds to reach its first progress update would
        otherwise leave the readout blank for those ten seconds, which is exactly
        the stretch where somebody wonders whether the button did anything.
        """
        if job_name in ("backend.open", "backend.scan"):
            # The scan shows its own message in the status bar; a second readout
            # for a job that finishes in 40 ms is noise.
            return
        self._operation.begin(job_name)

    def _on_job_progress(self, job_name: str, percent: int, message: str) -> None:
        if percent < 0 and message:
            self.statusBar().showMessage(message)
        if job_name == "backend.scan":
            return
        current = self._operation.operation
        if current and current != job_name:
            # A different job started without the previous one announcing its
            # end. Starting a new readout is the honest response: showing the new
            # job's progress against the old job's name would be worse.
            self._operation.begin(job_name)
        self._operation.report(None, message=message)
        if percent >= 0:
            self._operation.report(
                progress_module.JobProgress(
                    operation_type=job_name, percentage=float(percent),
                    status_message=message or self._operation.progress.status_message,
                )
            )

    def _on_job_progress_detail(self, job_name: str, progress: object) -> None:
        """Applies a full progress report - speed, bytes and ETA included."""
        if self._operation.operation not in ("", job_name):
            self._operation.begin(job_name)
        self._operation.report(progress_module.from_native(progress))

    def _on_job_finished(self, job_name: str) -> None:
        if job_name in ("backend.open", "backend.scan"):
            return
        if self._operation.operation and not self._operation.progress.running:
            return
        self._operation.finish(ok=True)

    def _on_job_failed(self, job_name: str, error_type: str, _traceback: str) -> None:
        if job_name in ("backend.open", "backend.scan"):
            return
        summary = error_type.rsplit(".", 1)[-1]
        self._operation.finish(ok=False, message=f"{summary} — see the log")

        where = job_name.replace("_", " ").replace(".", " ")
        if "ToolNotFound" in error_type:
            # The one failure in the tool whose fix is a click away, so it gets a
            # button rather than a sentence telling the operator to go looking.
            # The lifetime is longer than a normal error's because the card has to
            # survive being read and decided about.
            #
            # The title is the sentence, not the exception's name: "adb or fastboot
            # is not installed" is the news, and `ToolNotFoundError` is only useful
            # to somebody reading the log - where it already is.
            self._toasts.show(
                "error",
                "adb or fastboot is not installed",
                f"{where} needs it. Set the path in Settings → Tools & folders, or "
                "install Android platform-tools.",
                timeout_ms=20000,
                action=("Open Settings", partial(self._on_settings, page="tools")),
                key="toolchain",
            )
            self._console.append(
                "error",
                f"{where} failed: no adb or fastboot. Settings → Tools & folders takes a "
                "path; the traceback above names every location that was searched.",
            )
            return

        self.toast(
            "error",
            f"{where} failed",
            f"{summary}. The full traceback is in the log.",
            key=f"job:{job_name}",
        )

    def toast(
        self,
        level: str,
        title: str,
        message: str = "",
        *,
        key: str | None = None,
    ) -> None:
        """Shows a notification and puts the same text in the log.

        Deliberately both. A toast is transient by design, so anything it says and
        nothing else says is lost four seconds later - and a trouble-shooting
        session starts from the log.
        """
        self._toasts.show(level, title, message, key=key)
        if title or message:
            self._console.append(
                {"error": "error", "warn": "warn", "ok": "ok"}.get(level, "info"),
                f"{title}{' — ' + message if message and title else message}",
            )

    def _on_scan_started(self) -> None:
        self.statusBar().showMessage("Scanning for devices…")
        self._service.log_message("info", "scanning for devices")

    def _on_scan_finished(self, succeeded: bool) -> None:
        count = len(self._service.devices)
        if succeeded:
            self.statusBar().showMessage(f"Scan complete — {count} device(s) found.", 8000)
        else:
            self.statusBar().showMessage("Scan failed — see the log for details.", 8000)
            self.toast("error", "Scan failed", "The USB bus could not be enumerated.", key="scan")

    def _on_devices_changed(self, devices: list[Device]) -> None:
        self._device_panel.set_devices(devices)
        count = len(devices)
        targets = sum(1 for device in devices if device.recognised)
        if count and targets != count:
            self._device_count.setText(f"{count} devices · {targets} targets")
            self._device_count.setToolTip(
                f"{count - targets} attached device(s) are not a flashing target"
            )
        else:
            self._device_count.setText(f"{count} device{'s' if count != 1 else ''}")
            self._device_count.setToolTip("Attached USB devices. F5 scans again.")

        self._announce_device_changes(devices)

    def _announce_device_changes(self, devices: list[Device]) -> None:
        """Notices a device arriving or leaving between scans.

        Only for devices the tool recognises, and only after the first scan. The
        first scan is not an arrival - everything in it is news - and a USB
        keyboard being plugged in is not something a flashing tool should
        announce.
        """
        targets = {device.usb_id: device for device in devices if device.recognised}
        current = set(targets)
        if not self._scanned_once:
            self._scanned_once = True
            self._known_devices = current
            return

        for usb_id in sorted(current - self._known_devices):
            device = targets[usb_id]
            self.toast(
                "ok",
                f"{device.vendor or 'Device'} detected",
                f"{device.usb_id} in {device.target_label}. {device.identity}".strip(),
                key=f"arrive:{usb_id}",
            )
            self._device_panel.flash_device(device)
        for usb_id in sorted(self._known_devices - current):
            self.toast(
                "warn",
                "Device disconnected",
                f"{usb_id} is no longer on the bus. If a flash was running it has "
                "stopped.",
                key=f"depart:{usb_id}",
            )
        self._known_devices = current

    def _on_device_selected(self, device: Device | None) -> None:
        self._selected_device = device
        for panel in self._panels.values():
            panel.set_selected_device(device)
        if device is not None:
            self.statusBar().showMessage(
                f"Selected {device.usb_id} — {device.description} (bus {device.location})", 6000
            )

    def _on_state_changed(self, state: str) -> None:
        self._apply_chip_style(state)
        self._action_scan.setEnabled(state == "ready")
        if state == "unavailable":
            self.statusBar().showMessage("Backend unavailable — build the native module and restart.", 0)
        elif state == "ready":
            if self._startup_scan_pending:
                # The setting exists so an operator who always wants the device
                # list does not have to press F5 every launch.
                self._startup_scan_pending = False
                self._service.log_message("info", "scanning at startup (set in Settings)")
                self._on_scan()
            else:
                self.statusBar().showMessage("Backend ready. Run Scan Devices (F5).", 5000)

    def _on_busy_changed(self, busy: bool) -> None:
        self._action_scan.setEnabled(not busy and self._service.state == "ready")
        self._action_cancel.setEnabled(busy and self._service.can_cancel_current)
        for panel in self._panels.values():
            panel.refresh_actions()

    def _on_shutdown_backend(self) -> None:
        if self._service.shutdown():
            self._service.log_message("ok", "backend shut down")
        self.statusBar().showMessage("Backend stopped.", 5000)

    def _on_reset_layout(self) -> None:
        self._settings.remove("window/geometry")
        self._settings.remove("window/state")
        self._devices_dock.setVisible(True)
        self._log_dock.setVisible(True)
        self.resizeDocks([self._devices_dock], [_DEFAULT_DEVICE_DOCK_WIDTH], Qt.Orientation.Horizontal)
        self.resizeDocks([self._log_dock], [_DEFAULT_LOG_DOCK_HEIGHT], Qt.Orientation.Vertical)
        self.resize(*_DEFAULT_SIZE)
        self._apply_chip_style(self._service.state)
        self._operation.reset()
        self._service.log_message("info", "layout reset to defaults")
        self.toast("info", "Layout reset", "The window is back to its default arrangement.")

    def _on_settings(self, page: str | None = None) -> None:
        """Opens the settings dialog and applies what was accepted.

        `page` names the category to open on, so a notification about a missing
        adb can lead straight to the box that takes its path.
        """
        accepted = show_settings_dialog(self.settings, self, page=page)
        if accepted is None:
            return
        self.settings = accepted
        # Published before the backend is told, so anything the backend does in
        # response already sees the new values.
        set_active_settings(accepted)
        # The backend owns the log, so the settings have to reach it on the
        # worker thread rather than by the UI reaching into the Logger directly.
        self._service.apply_settings(accepted)
        self._console.set_auto_scroll(accepted.auto_scroll_log)
        self._apply_interface_settings(accepted)
        if accepted.theme != self.themes.current:
            # The picker is the authority on the theme; the dialog can change it
            # too, and the two must not be able to disagree. A straight call, with
            # no `hasattr` guard: the guard this replaced meant a picker that could
            # not be synced failed silently, which is how a chooser that never
            # worked survived a thousand passing checks.
            self.themes.set_theme(accepted.theme)
            self._theme_picker.syncToTheme(accepted.theme)
        self._service.log_message("info", f"settings saved to {settings_path()}")

    def _apply_interface_settings(self, settings: Settings) -> None:
        """Applies the settings that live in this process rather than in the core.

        Motion and the Android tool paths are not the backend's business - they
        belong to the interface and to the wrapper this process owns - so they are
        applied here, at startup and again whenever the dialog is accepted.
        """
        animations.set_animations_enabled(bool(settings.animations))
        try:
            from huaxin.core.adb_fastboot_wrapper import configure_tools

            tools = configure_tools(settings.adb_path, settings.fastboot_path)
        except Exception as exc:  # a missing tool must not stop the tool from running
            self._service.log_message("warn", f"could not configure adb/fastboot: {exc}")
            return
        self._service.log_message(
            "debug",
            f"android tools: adb={tools.adb_path or 'not found'}, "
            f"fastboot={tools.fastboot_path or 'not found'}",
        )

    def _on_gallery(self) -> None:
        """Opens the component gallery.

        Kept as one instance and raised rather than replaced: opening it twice
        from the menu should bring the window forward, not stack a second copy
        of the same forty components.
        """
        if self._gallery is None:
            from huaxin.ui.gallery import Gallery

            self._gallery = Gallery()
        self._gallery.show()
        self._gallery.raise_()
        self._gallery.activateWindow()

    def _on_driver_help(self) -> None:
        DriverHelpDialog(self).exec()

    def _on_open_log_folder(self) -> None:
        """Reveals the flash log in the platform's file manager."""
        # The settings file may name the log, but the backend is what actually
        # opened it - so ask it first rather than guessing. It falls back to the
        # default location when the backend has not opened one yet.
        target = self._service.log_path or self.settings.log_file.strip()
        if not target:
            target = str(default_log_path())
        if not open_in_file_manager(Path(target)):
            QMessageBox.information(
                self,
                "Log folder",
                f"This platform has no file manager to open.\n\nThe log is at:\n{target}",
            )

    def _on_about(self) -> None:
        """Opens the About dialog, which states the build and the support status."""
        AboutDialog(self, log_path=self._service.log_path or str(default_log_path())).exec()

    # -- drag and drop ------------------------------------------------------

    def _dropped_paths(self, event) -> list[Path]:
        """The local files in a drag, filtered to what this tool can open."""
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        paths: list[Path] = []
        for url in mime.urls():
            if not url.isLocalFile():
                # A URL from a browser or a network share is not opened: fetching
                # it would be a network operation disguised as a drop.
                continue
            path = Path(url.toLocalFile())
            if path.is_file():
                paths.append(path)
        return paths

    @staticmethod
    def _route_for(path: Path) -> str | None:
        """Which panel takes this file, or None when nothing does."""
        name = path.name.lower()
        for suffixes, key in _DROP_ROUTES:
            if name.endswith(suffixes):
                return key
        return None

    def _describe_drop(self, paths: list[Path]) -> str:
        """What the overlay says. Says "no" clearly when it means no."""
        handled = [(path, self._route_for(path)) for path in paths]
        handled = [(path, key) for path, key in handled if key]
        if not handled:
            return (
                "Not a firmware file this tool can read.\n"
                "Drop a .pac, .tar.md5, .tar or .pit — or use a tab's Load button."
            )
        if len(handled) == 1:
            path, key = handled[0]
            return f"Drop to open {path.name} in the {_PANEL_LABELS[key]} tab"
        return f"Drop to open {len(handled)} files"

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt naming
        paths = self._dropped_paths(event)
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self._show_drop_hint(paths)

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Accepted so the cursor keeps showing "copy" for the whole drag rather
        # than flickering back to "no" over every child widget.
        if self._dropped_paths(event):
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._hide_drop_hint()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt naming
        self._hide_drop_hint()
        paths = self._dropped_paths(event)
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self._open_dropped(paths)

    def _open_dropped(self, paths: list[Path]) -> None:
        """Opens each dropped file in the tab that understands it."""
        opened = 0
        for path in paths:
            key = self._route_for(path)
            if key is None:
                self.toast(
                    "warn",
                    f"{path.name} is not a package this tool reads",
                    "Expected a .pac, .tar.md5, .tar or .pit file.",
                    key=f"drop:{path.name}",
                )
                continue
            index = [key_of for key_of, _ in _PANEL_TYPES].index(key)
            panel = self._ensure_panel(index)
            if panel is None:  # pragma: no cover - the index always exists
                continue
            self._tabs.setCurrentIndex(index)
            # Routed through the panel's own loader, so a dropped file and a
            # chosen file take exactly the same path from here on.
            if key == "spd":
                panel.load_pac_file(path)
            elif path.name.lower().endswith(".pit"):
                panel.load_pit_file(path)
            else:
                panel.load_package_file(path)
            filedialog.remember_file(path)
            opened += 1

        if opened:
            names = ", ".join(path.name for path in paths[:2])
            more = f" and {opened - 2} more" if opened > 2 else ""
            self.toast("info", "Reading the dropped package", f"{names}{more}", key="drop")

    def _show_drop_hint(self, paths: list[Path]) -> None:
        """Places the overlay over the tab area and fades it in."""
        self._drop_paths = list(paths)
        self._drop_hint.setText(self._describe_drop(paths))
        self._drop_hint.setProperty("active", bool([p for p in paths if self._route_for(p)]))
        style.restyle(self._drop_hint)
        self._position_drop_hint()
        self._drop_hint.raise_()
        if not self._drop_hint.isVisible():
            animations.fade_in(self._drop_hint, animations.motion.hover)

    def _position_drop_hint(self) -> None:
        margin = 8
        top = self.titlebar.height() + self._toolbar.height() + margin
        self._drop_hint.setGeometry(
            margin,
            top,
            max(120, self.width() - margin * 2),
            max(80, self.height() - top - self.statusBar().height() - margin),
        )

    def _hide_drop_hint(self) -> None:
        self._drop_paths = []
        if self._drop_hint.isVisible():
            animations.fade_out(self._drop_hint, animations.motion.hover)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Keeps the drop overlay where it belongs while the window is resized."""
        super().resizeEvent(event)
        if self._drop_hint.isVisible():
            self._position_drop_hint()

    def _apply_chip_style(self, state: str) -> None:
        """Paints the backend state chip.

        The tone is a property the stylesheet understands, so the colour comes
        from the theme rather than from a literal chosen here - which is what
        keeps the chip correct after a theme switch. The previous version set an
        inline stylesheet with a hard-coded text colour, so on the light theme it
        stayed dark-on-light and was the one control the switch left behind.
        """
        tone, text = _STATE_CHIP.get(state, _STATE_CHIP["stopped"])
        self._state_chip.setTone(tone)
        self._state_chip.setText(text)

    def _on_theme_changed(self, name: str) -> None:
        """Re-renders the window chrome and remembers the choice."""
        self.refresh_chrome()
        self.settings.theme = name
        try:
            save_settings(self.settings)
        except OSError as exc:
            # Not fatal and not worth a dialog: the theme has already been
            # applied, it simply will not be remembered.
            self._service.log_message("warn", f"could not save the theme choice: {exc}")

    # -- layout persistence ------------------------------------------------

    def _restore_layout(self) -> None:
        geometry = self._settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        state = self._settings.value("window/state")
        if state is not None:
            self.restoreState(state)
        else:
            self.resizeDocks([self._devices_dock], [_DEFAULT_DEVICE_DOCK_WIDTH], Qt.Orientation.Horizontal)
            self.resizeDocks([self._log_dock], [_DEFAULT_LOG_DOCK_HEIGHT], Qt.Orientation.Vertical)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt naming
        self._settings.setValue("window/geometry", self.saveGeometry())
        self._settings.setValue("window/state", self.saveState())
        self._settings.sync()
        super().closeEvent(event)
