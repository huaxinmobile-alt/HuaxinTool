"""Reusable widgets: the device list and the log console."""

from __future__ import annotations

import html
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from huaxin.core.backend import Device
from huaxin.ui import animations, components
from huaxin.ui import filedialog
from huaxin.ui.safe_slot import safe_slot
from huaxin.ui.theme import COLORS, LOG_COLORS

__all__ = [
    "DevicePanel",
    "DeviceTable",
    "JobProgressBar",
    "LogConsole",
    "PartitionTableWidget",
    "ScatterTableWidget",
]

_LEVEL_TAGS = {
    "debug": "DEBUG",
    "info": "INFO ",
    "output": "OUT  ",  # raw stdout/stderr of a tool we ran
    "ok": "OK   ",
    "warn": "WARN ",
    "error": "ERROR",
}


class DeviceTable(QTableWidget):
    """Table of `Device` snapshots.

    The Device object is stored in each item's UserRole data, so the selected
    device survives re-sorting by any column - reading it back from the row index
    would silently break once the user clicks a header.
    """

    selection_changed = pyqtSignal(object)  # Device | None

    COLUMNS = ("Vendor", "USB ID", "Target mode", "Device")
    _EMPTY_TEXT = "No devices found — run Scan Devices (F5)"

    #: How long a newly-arrived row stays tinted, and how often the tint is
    #: recomputed. 40 ms is 25 frames a second, which is smooth enough for a
    #: colour fade and cheap enough not to be worth measuring.
    FLASH_MS = 900
    FLASH_TICK_MS = 40

    #: Set by set_devices(): why the table is empty, so the placeholder can say
    #: whether nothing was found or something is being hidden.
    _empty_text = _EMPTY_TEXT

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, len(self.COLUMNS), parent)
        self.setObjectName("DeviceTable")
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setSortingEnabled(True)
        self.setWordWrap(False)
        self._flash_rows: list[int] = []
        self._flash_started = 0.0
        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(self.FLASH_TICK_MS)
        self._flash_timer.timeout.connect(safe_slot(self._tick_flash))

        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setHighlightSections(False)

        self.itemSelectionChanged.connect(safe_slot(self._emit_selection))

    # -- data --------------------------------------------------------------

    def set_devices(self, devices: list[Device], empty_text: str | None = None) -> None:
        """Replace the contents, keeping the selection if that device is still present.

        `empty_text` explains an empty result; it differs between "nothing was
        found" and "the filter is hiding everything", which look identical
        otherwise.
        """
        previous = self.selected_device()
        previous_ids = set(self._device_ids())
        self._empty_text = empty_text or self._EMPTY_TEXT
        self._stop_flash()

        # Sorting must be off while rows are inserted: with it on, each setItem()
        # can move the row out from under the loop and scramble the table.
        self.setSortingEnabled(False)
        self.clearContents()

        if not devices:
            self._show_empty_state()
        else:
            self.setRowCount(len(devices))
            for row, device in enumerate(devices):
                cells = (
                    device.vendor or "—",
                    device.usb_id,
                    device.target_label,
                    device.identity,
                )
                for column, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, device)
                    item.setToolTip(device.detail)
                    if column < 2:
                        item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                    self.setItem(row, column, item)

            self.setSortingEnabled(True)
            # Enabling sorting re-applies whatever sort indicator Qt last had,
            # which on a fresh table means devices come out in reverse order.
            # Sort explicitly so the list is grouped by vendor from the first scan.
            self.sortItems(0, Qt.SortOrder.AscendingOrder)
            # Tinted after the sort, because the tint follows the *row*, and the
            # sort is what decides which row a device ended up in.
            self._flash_new(*[device.usb_id for device in devices
                              if device.usb_id not in previous_ids])

        self._reselect(previous)
        self._emit_selection()

    def _device_ids(self) -> list[str]:
        ids: list[str] = []
        for row in range(self.rowCount()):
            item = self.item(row, 0)
            device = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if isinstance(device, Device):
                ids.append(device.usb_id)
        return ids

    # -- the arrival tint --------------------------------------------------

    def _flash_new(self, *usb_ids: str) -> None:
        """Tints the rows of devices that were not there at the last scan.

        A new device appearing in a list of eight is almost invisible otherwise -
        the operator has to read every row to find out whether their cable is
        seen. Half a second of accent tint answers that without being read.
        """
        if not usb_ids or not animations.animations_enabled():
            return
        wanted = set(usb_ids)
        rows = [
            row
            for row in range(self.rowCount())
            if (item := self.item(row, 0)) is not None
            and isinstance(item.data(Qt.ItemDataRole.UserRole), Device)
            and item.data(Qt.ItemDataRole.UserRole).usb_id in wanted
        ]
        if not rows:
            return
        self._flash_rows = rows
        self._flash_started = time.monotonic()
        self._tick_flash()
        self._flash_timer.start()

    def _tick_flash(self) -> None:
        elapsed = time.monotonic() - self._flash_started
        fraction = min(1.0, elapsed * 1000.0 / self.FLASH_MS)
        accent = QColor(COLORS["accent"])
        for row in self._flash_rows:
            if row >= self.rowCount():
                continue
            for column in range(self.columnCount()):
                item = self.item(row, column)
                if item is None:
                    continue
                if fraction >= 1.0:
                    # Cleared rather than set to the row colour: an explicit
                    # background would override the alternating-row styling and
                    # leave the row looking permanently selected.
                    item.setBackground(QBrush())
                else:
                    tint = QColor(accent)
                    tint.setAlpha(int(70 * (1.0 - fraction)))
                    item.setBackground(QBrush(tint))
        if fraction >= 1.0:
            self._stop_flash()

    def _stop_flash(self) -> None:
        self._flash_timer.stop()
        for row in self._flash_rows:
            for column in range(self.columnCount()):
                item = self.item(row, column) if row < self.rowCount() else None
                if item is not None:
                    item.setBackground(QBrush())
        self._flash_rows = []

    def flash_device(self, device: Device) -> bool:
        """Tints one device's row. False when it is not listed."""
        if not self.select_device(device):
            return False
        self._flash_new(device.usb_id)
        return True

    def _reselect(self, device: Device | None) -> None:
        self.select_device(device)

    def _emit_selection(self) -> None:
        self.selection_changed.emit(self.selected_device())

    def _show_empty_state(self) -> None:
        """One unselectable placeholder row, so an empty list reads as a state
        rather than as a broken table."""
        self.setRowCount(1)
        item = QTableWidgetItem(self._empty_text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setForeground(Qt.GlobalColor.gray)
        self.setItem(0, 0, item)
        self.setSpan(0, 0, 1, len(self.COLUMNS))

    def selected_device(self) -> Device | None:
        items = self.selectedItems()
        if not items:
            return None
        device = items[0].data(Qt.ItemDataRole.UserRole)
        return device if isinstance(device, Device) else None

    def select_device(self, device: Device | None) -> bool:
        """Select the row holding `device`. No-op, returning False, if absent."""
        if device is None:
            return False
        for row in range(self.rowCount()):
            item = self.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == device:
                self.selectRow(row)
                return True
        return False

    def _reselect(self, device: Device | None) -> None:
        self.select_device(device)

    def _emit_selection(self) -> None:
        self.selection_changed.emit(self.selected_device())


class DevicePanel(QWidget):
    """The dock's contents: filter, summary line and the device table."""

    selection_changed = pyqtSignal(object)  # Device | None
    #: Emitted from the table's context menu, so "Scan" is reachable where the
    #: operator is looking rather than only from the toolbar.
    scan_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._devices: tuple[Device, ...] = ()
        self._scanned = False
        # Remembered so toggling the filter does not throw away the operator's
        # selection just because the row briefly disappears.
        self._last_selected: Device | None = None

        self.table = DeviceTable(self)

        self._filter = QCheckBox("Supported targets only", self)
        self._filter.setToolTip(
            "Hide USB devices that are not a flashing target, such as keyboards, "
            "hubs and mass storage."
        )
        self._filter.toggled.connect(safe_slot(self._on_filter_toggled))

        # Plays once when a device arrives. Deliberately a one-shot: an indicator
        # that pulses forever stops being a signal within ten seconds.
        self._pulse = components.PulseRing(self, size=22, tone="ok")
        self._pulse.setToolTip("A device just appeared")

        self._summary = QLabel("No scan yet", self)
        self._summary.setObjectName("PanelSubtitle")

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)
        controls.addWidget(self._filter)
        controls.addWidget(self._pulse)
        controls.addStretch(1)
        controls.addWidget(self._summary)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addLayout(controls)
        layout.addWidget(self.table, 1)

        self.table.selection_changed.connect(safe_slot(self._on_table_selection))
        self._install_context_menu()
        self._apply_filter()  # show the initial "no scan yet" state

    # -- context menu ------------------------------------------------------

    def _install_context_menu(self) -> None:
        """Right-click on a device: copy its identifiers, or filter to it.

        The copy entries are the useful ones - a USB ID is what gets pasted into
        a driver binding tool or a bug report - and they are built per row, so
        they are absent rather than disabled when the click is not on a device.
        """
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(safe_slot(self._show_menu))

    def _show_menu(self, position) -> None:
        index = self.table.indexAt(position)
        device: Device | None = None
        if index.isValid():
            item = self.table.item(index.row(), 0)
            if item is not None:
                data = item.data(Qt.ItemDataRole.UserRole)
                device = data if isinstance(data, Device) else None

        menu = QMenu(self.table)
        if device is None:
            menu.addAction("Scan for devices (F5)", safe_slot(self.scan_requested.emit))
            menu.addSeparator()
            action = menu.addAction("Supported targets only")
            action.setCheckable(True)
            action.setChecked(self.filter_enabled)
            action.toggled.connect(safe_slot(self._filter.setChecked))
            menu.exec(self.table.viewport().mapToGlobal(position))
            return

        header = menu.addAction(f"{device.vendor or 'Device'} · {device.usb_id}")
        header.setEnabled(False)
        menu.addSeparator()
        menu.addAction("Copy USB ID", safe_slot(self._copy, device.usb_id))
        menu.addAction("Copy description", safe_slot(self._copy, device.description))
        menu.addAction("Copy all details", safe_slot(self._copy, device.detail))
        menu.addSeparator()
        menu.addAction("Select and show in the tab", safe_slot(self._select, device))
        menu.exec(self.table.viewport().mapToGlobal(position))

    @staticmethod
    def _copy(text: str) -> None:
        from PyQt6.QtWidgets import QApplication

        QApplication.clipboard().setText(text)

    def _select(self, device: Device) -> None:
        self.table.select_device(device)
        self.selection_changed.emit(device)

    def flash_device(self, device: Device) -> None:
        """Draws attention to a device that has just arrived."""
        self.table.flash_device(device)
        self._pulse.start(rings=3)

    def _on_table_selection(self, device: Device | None) -> None:
        if device is not None:
            self._last_selected = device
        self.selection_changed.emit(device)

    # -- api ---------------------------------------------------------------

    def set_devices(self, devices: list[Device]) -> None:
        self._devices = tuple(devices)
        self._scanned = True
        self._apply_filter()

    def selected_device(self) -> Device | None:
        return self.table.selected_device()

    def clear(self) -> None:
        self._devices = ()
        self._scanned = False
        self._apply_filter()

    @property
    def filter_enabled(self) -> bool:
        return self._filter.isChecked()

    # -- internals ---------------------------------------------------------

    def _on_filter_toggled(self, _checked: bool) -> None:
        self._apply_filter()

    def _apply_filter(self) -> None:
        visible = [device for device in self._devices if self._is_visible(device)]
        self.table.set_devices(visible, empty_text=self._empty_message(bool(visible)))
        if self.table.selected_device() is None:
            # Re-select what the operator had before the filter hid it.
            self.table.select_device(self._last_selected)

        total = len(self._devices)
        targets = sum(1 for device in self._devices if device.recognised)
        if total == 0:
            self._summary.setText("No scan yet" if not self._scanned else "Nothing found")
            self._summary.setToolTip("")
            return

        parts = [f"{targets} target{'s' if targets != 1 else ''}"]
        hidden = total - targets
        if hidden:
            parts.append(
                f"{hidden} other device{'s' if hidden != 1 else ''}"
                + (" hidden" if self.filter_enabled else "")
            )
        self._summary.setText(" · ".join(parts))
        self._summary.setToolTip(
            "\n".join(f"{d.usb_id}  {d.description}" for d in self._devices)
        )

    def _is_visible(self, device: Device) -> bool:
        return device.recognised if self.filter_enabled else True

    def _empty_message(self, has_visible: bool) -> str:
        """Why the table is empty - three different situations, three messages."""
        if has_visible:
            return ""
        if not self._scanned:
            return "No scan yet — run Scan Devices (F5)"
        if self._devices and self.filter_enabled:
            hidden = len(self._devices)
            return (
                f"No supported targets — {hidden} other USB device"
                f"{'s are' if hidden != 1 else ' is'} hidden by the filter"
            )
        return "No USB devices found — check the connection and scan again"


class LogConsole(QWidget):
    """Timestamped, colour-coded, append-only log view.

    `append()` must be called on the UI thread; backend logs arrive here through
    `BackendService.log`, which Qt delivers as a queued connection from the
    worker thread.
    """

    MAX_BLOCKS = 5000  # keeps memory flat during a long flash session

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._view = QPlainTextEdit(self)
        self._view.setObjectName("LogConsole")
        self._view.setReadOnly(True)
        self._view.setUndoRedoEnabled(False)
        self._view.setMaximumBlockCount(self.MAX_BLOCKS)
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._view.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self._view.document().setDocumentMargin(6)

        self._autoscroll = QCheckBox("Auto-scroll", self)
        self._autoscroll.setChecked(True)

        self._counts: dict[str, int] = {}
        self._summary = QLabel("0 lines", self)
        self._summary.setObjectName("LogSummary")

        clear_button = QPushButton("Clear", self)
        clear_button.clicked.connect(safe_slot(self.clear))
        save_button = QPushButton("Save…", self)
        save_button.clicked.connect(safe_slot(self._save))

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(8)
        toolbar.addWidget(clear_button)
        toolbar.addWidget(save_button)
        toolbar.addWidget(self._autoscroll)
        toolbar.addStretch(1)
        toolbar.addWidget(self._summary)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addLayout(toolbar)
        layout.addWidget(self._view, 1)
        self._build_context_menu()

    # -- api ---------------------------------------------------------------

    def append(self, level: str, message: str) -> None:
        """Append one entry. Multi-line messages get aligned continuation lines."""
        level = (level or "info").lower()
        if level not in _LEVEL_TAGS:
            level = "info"

        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        color = LOG_COLORS.get(level, LOG_COLORS["info"])
        tag = _LEVEL_TAGS[level]

        lines = str(message).splitlines() or [""]
        for index, line in enumerate(lines):
            if index == 0:
                prefix = (
                    f'<span style="color:{COLORS["text_dim"]}">{stamp}</span> '
                    f'<span style="color:{color}"><b>{tag}</b></span> '
                )
            else:
                # Indent continuation lines (tracebacks) under the message column.
                prefix = " " * 25
            self._view.appendHtml(prefix + f'<span style="color:{color}">{html.escape(line)}</span>')

        self._counts[level] = self._counts.get(level, 0) + 1
        self._update_summary()

        if self._autoscroll.isChecked():
            self._view.verticalScrollBar().setValue(self._view.verticalScrollBar().maximum())

    def clear(self) -> None:
        self._view.clear()
        self._counts.clear()
        self._update_summary()

    def set_auto_scroll(self, enabled: bool) -> None:
        """Sets the starting state from the saved setting.

        The checkbox stays the live control - an operator who scrolls back to
        read something can untick it there - so this only applies the saved
        preference, it does not take the choice away.
        """
        self._autoscroll.setChecked(bool(enabled))

    def auto_scroll(self) -> bool:
        return self._autoscroll.isChecked()

    def text(self) -> str:
        return self._view.toPlainText()

    # -- internals ---------------------------------------------------------

    def _update_summary(self) -> None:
        total = sum(self._counts.values())
        errors = self._counts.get("error", 0)
        warnings = self._counts.get("warn", 0)
        parts = [f"{total} line{'s' if total != 1 else ''}"]
        if warnings:
            parts.append(f"{warnings} warning{'s' if warnings != 1 else ''}")
        if errors:
            parts.append(f"{errors} error{'s' if errors != 1 else ''}")
        self._summary.setText(" · ".join(parts))

    def _save(self) -> None:
        self.save_to_file()

    def save_to_file(self) -> bool:
        """Asks for a path and writes the console's contents there.

        The same code path as Ctrl+S and the toolbar's Save Log, which is the
        point: one save, three ways in, so the three cannot differ in what they
        write or in whether they report a failure.
        """
        default = Path.home() / f"huaxin-log-{datetime.now():%Y%m%d-%H%M%S}.txt"
        path, _ = filedialog.save_file_name(
            self, "Save log", str(default),
            "Text files (*.txt);;Log files (*.log);;All files (*)",
        )
        if not path:
            return False
        try:
            Path(path).write_text(self.text(), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Save failed", f"Could not write {path}:\n{exc}")
            return False
        self.append("ok", f"log saved to {path}")
        return True

    # -- context menu ------------------------------------------------------

    def _build_context_menu(self) -> None:
        """Right-click in the console: copy, save, clear.

        Qt's own copy handling stays in use for the selection, and the three
        actions that belong to the *console* rather than to the text are added
        beside it - so Save and Clear are one click away rather than only in a
        toolbar at the other end of the window.
        """
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(safe_slot(self._show_context_menu))

    def _show_context_menu(self, position) -> None:
        view = self._view
        menu = QMenu(view)

        copy_selection = menu.addAction("Copy selected lines")
        copy_selection.setEnabled(view.textCursor().hasSelection())
        copy_selection.triggered.connect(safe_slot(view.copy))
        copy_all = menu.addAction("Copy everything")
        copy_all.triggered.connect(
            safe_slot(lambda: QApplication.clipboard().setText(self.text()))
        )
        menu.addSeparator()
        menu.addAction("Save the log to a file…", safe_slot(self.save_to_file))
        menu.addAction("Open the log folder", safe_slot(self._on_open_log_folder))
        menu.addSeparator()
        menu.addAction("Clear the console", safe_slot(self.clear))
        menu.exec(view.viewport().mapToGlobal(position))

    def _on_open_log_folder(self) -> None:
        from huaxin.core.config import default_log_path
        from huaxin.ui.settings_dialog import open_in_file_manager

        if not open_in_file_manager(Path(default_log_path())):
            self.append("info", f"the log folder is {default_log_path().parent}")


class _SortableItem(QTableWidgetItem):
    """A cell that sorts by the value behind it, not by its formatted text.

    "12.3 MiB" and "998.0 KiB" sort as text into the wrong order, and a size
    column that orders 998 KiB above 12 MiB is worse than no sorting at all. The
    raw value is stashed in UserRole and compared there.
    """

    def __init__(self, text: str, sort_key: object = None) -> None:
        super().__init__(text)
        if sort_key is not None:
            self.setData(Qt.ItemDataRole.UserRole, sort_key)

    def __lt__(self, other: QTableWidgetItem) -> bool:  # type: ignore[override]
        left = self.data(Qt.ItemDataRole.UserRole)
        right = other.data(Qt.ItemDataRole.UserRole)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return bool(left < right)
        return str(self.text()).lower() < str(other.text()).lower()


class PartitionTableWidget(QTableWidget):
    """The GPT a device reported, one row per partition in use.

    Unused slots are counted in the caption rather than given a row each: a table
    that is mostly "unused" hides the partitions the operator is looking for.

    Each row carries its PartitionRecord in UserRole, so the selection survives
    re-sorting - the same reason DeviceTable stores the Device there.
    """

    COLUMNS = ("Name", "Start LBA", "Sectors", "Size", "LUN", "Type GUID")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, len(self.COLUMNS), parent)
        self.setObjectName("PartitionTable")
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setSortingEnabled(True)
        self.setWordWrap(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(self.COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

    # -- api ---------------------------------------------------------------

    def set_table(self, table: object | None) -> None:
        """Renders a PartitionTable (or clears the view when given None)."""
        # Sorting is switched off while the rows go in: with it on, Qt re-sorts
        # after every setItem and the rows land in whatever order the sort
        # happens to leave them.
        self.setSortingEnabled(False)
        self.setRowCount(0)
        if table is None:
            self.setSortingEnabled(True)
            return

        for record in table.partitions:
            if record.is_unused:
                continue
            row = self.rowCount()
            self.insertRow(row)
            cells = (
                _SortableItem(record.display_name),
                _SortableItem(f"{record.first_sector:,}", record.first_sector),
                _SortableItem(f"{record.sector_count:,}", record.sector_count),
                _SortableItem(_format_bytes(record.size_bytes), record.size_bytes),
                _SortableItem(str(table.lun), table.lun),
                _SortableItem(record.type_guid),
            )
            for column, item in enumerate(cells):
                item.setData(Qt.ItemDataRole.UserRole + 1, record)
                self.setItem(row, column, item)

        self.setSortingEnabled(True)
        # Re-enabling sorting makes Qt sort by whatever column its indicator is
        # on, which is not knowable from here. Sorting explicitly by start sector
        # leaves the rows in flash-layout order - the order the GPT itself is in
        # - and is deterministic. A click on any header overrides it.
        self.sortItems(1, Qt.SortOrder.AscendingOrder)

    def selected_partition(self) -> object | None:
        rows = self.selectionModel().selectedRows() if self.selectionModel() else []
        if not rows:
            return None
        item = self.item(rows[0].row(), 0)
        return item.data(Qt.ItemDataRole.UserRole + 1) if item is not None else None


def _format_bytes(count: float) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


#: What each scatter action is coloured with. A write is the accent, an erase
#: is the warning, and a skip is dimmed - the three things a package can say
#: about a partition, each with the tone that matches how much attention it
#: deserves.
_ACTION_COLOURS = {
    "write": COLORS["accent"],
    "erase": COLORS["warn"],
    "skip": COLORS["text_dim"],
}


class ScatterTableWidget(QTableWidget):
    """The partitions a MediaTek scatter file lists, one row each.

    A different shape from the GPT table on purpose. A scatter file is a *plan*
    rather than a report: every row says what will happen to that partition -
    written from an image, or erased because the package lists it with none -
    and the operator reads that column before agreeing to anything.

    Each row carries its PartitionRecord in UserRole, so the selection survives
    re-sorting the same way the device and partition tables do.
    """

    COLUMNS = ("Name", "Index", "Action", "Address", "Size", "Region", "Image file")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, len(self.COLUMNS), parent)
        self.setObjectName("ScatterTable")
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setSortingEnabled(True)
        self.setWordWrap(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3, 4, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

    # -- api ---------------------------------------------------------------

    def set_scatter(self, info: object | None) -> None:
        """Renders a ScatterInfo, or clears the view when given None."""
        self.setSortingEnabled(False)
        self.setRowCount(0)
        if info is None:
            self.setSortingEnabled(True)
            return

        for record in info.partitions:
            action = self._action_for(record)
            row = self.rowCount()
            self.insertRow(row)
            cells = (
                _SortableItem(record.display_name),
                _SortableItem(record.index),
                _SortableItem(action),
                _SortableItem(f"0x{record.start_address:x}", record.start_address),
                _SortableItem(_format_bytes(record.size), record.size),
                _SortableItem(record.region),
                _SortableItem(record.file_name),
            )
            for column, item in enumerate(cells):
                item.setData(Qt.ItemDataRole.UserRole + 1, record)
                # The action column is tinted by what the package will do to that
                # partition, and the row's other cells stay neutral - a whole row
                # in warning yellow reads as an error rather than as a plan.
                # write is the accent, erase is the warning, skip is dimmed to
                # the point of being ignorable, which is what "nothing happens to
                # this one" should look like in a list an operator scans.
                if column == 2:
                    item.setForeground(QColor(_ACTION_COLOURS.get(action, COLORS["text_dim"])))
                elif action == "skip":
                    item.setForeground(QColor(COLORS["text_dim"]))
                self.setItem(row, column, item)

        self.setSortingEnabled(True)
        # Flash-layout order, which is the order the package means them in and
        # the order a run would write them. A header click overrides it.
        self.sortItems(3, Qt.SortOrder.AscendingOrder)

    def selected_partitions(self) -> list[object]:
        """The records for every selected row, in table order."""
        if self.selectionModel() is None:
            return []
        records: list[object] = []
        for index in sorted(self.selectionModel().selectedRows(), key=lambda i: i.row()):
            item = self.item(index.row(), 0)
            if item is not None:
                records.append(item.data(Qt.ItemDataRole.UserRole + 1))
        return records

    def selected_partition(self) -> object | None:
        records = self.selected_partitions()
        return records[0] if records else None

    def download_count(self) -> int:
        count = 0
        for row in range(self.rowCount()):
            item = self.item(row, 2)
            if item is not None and item.text() == "write":
                count += 1
        return count

    @staticmethod
    def _action_for(record: object) -> str:
        if record.is_download and record.file_name:
            return "write"
        if record.size:
            return "erase"
        return "skip"


class JobProgressBar(QWidget):
    """Determinate progress with the job's own message under it.

    Hidden until a job reports progress, so the panel does not carry a dead bar.
    A percent below zero means "running, duration unknown" and switches the bar
    to its busy animation rather than showing a meaningless zero.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bar = QProgressBar(self)
        self._bar.setTextVisible(True)
        self._bar.setRange(0, 100)
        self._bar.setValue(0)

        self._label = QLabel("", self)
        self._label.setObjectName("PanelSubtitle")
        self._label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._bar)
        layout.addWidget(self._label)
        self.setVisible(False)

    # -- api ---------------------------------------------------------------

    def begin(self, label: str) -> None:
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setFormat("%p%")
        self._label.setText(label)
        self.setVisible(True)

    def set_progress(self, percent: int, message: str = "") -> None:
        if percent < 0:
            # No percentage available: animate instead of implying "0% done".
            if self._bar.maximum() != 0:
                self._bar.setRange(0, 0)
                self._bar.setFormat("")
        else:
            if self._bar.maximum() == 0:
                self._bar.setRange(0, 100)
                self._bar.setFormat("%p%")
            self._bar.setValue(max(0, min(100, int(percent))))
        if message:
            self._label.setText(message)

    def finish(self, message: str = "") -> None:
        if message:
            self._label.setText(message)
        self._bar.setRange(0, 100)
        self._bar.setFormat("%p%")
        self._bar.setValue(100)

    def reset(self) -> None:
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._label.clear()
        self.setVisible(False)
