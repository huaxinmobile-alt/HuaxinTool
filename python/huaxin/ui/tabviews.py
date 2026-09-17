"""The displays that belong to one vendor's tab.

Three of the five tabs show a table of what a *file* contains rather than what a
device reported: a Unisoc PAC's entries, a Samsung PIT's partitions, and the
members of a `.tar.md5` package. They share a shape - load a file, list it, let
the operator act on a row - and they share the rule that nothing here writes to a
device.

That rule is the point of these views. Unisoc flashing and Samsung flashing are
not implemented, so a tab offering to wipe the device's partition table would be
lying. Reading a package and showing its contents is useful on its own, though:
it is how an operator checks they have the right file before doing anything else,
and how they find out that the six-gigabyte download they just made is truncated.

Everything shown comes from the native parsers, which are covered by the test
suites and by a header-only read path that does not pull a multi-gigabyte package
into memory.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QTableWidgetItem, QVBoxLayout, QWidget

from huaxin.ui import components as ui
from huaxin.ui import tokens
from huaxin.ui.widgets import _SortableItem, _format_bytes

__all__ = ["PacContentsView", "PitTableView", "FirmwarePackageView"]


def _tone_item(text: str, value: object, tone: str) -> _SortableItem:
    """A cell coloured by the theme's tone tokens rather than a literal."""
    item = _SortableItem(text, value)
    theme = tokens.active_theme()
    item.setForeground(QColor(theme.colour(tone)))
    return item


class PacContentsView(QWidget):
    """What a Unisoc `.pac` contains: its header, and one row per entry.

    The header is shown as a caption rather than as a table row: product name,
    package version and the declared size are three facts an operator reads once,
    and a three-row table to hold them would be more furniture than information.

    The CRC state is shown and *not* implied. A header-only read cannot check the
    payload CRC, so the view says "not checked" rather than showing a green tick
    that was never earned - the difference matters when the next step is to write
    the package to a device.
    """

    COLUMNS = ("File ID", "File name", "Role", "Size", "Address", "Offset", "Notes")

    entry_activated = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(tokens.METRICS.gap_sm)

        self._caption = QLabel(self)
        self._caption.setProperty("role", "caption")
        self._caption.setWordWrap(True)
        layout.addWidget(self._caption)

        self.table = ui.StyledTable(0, len(self.COLUMNS), self)
        self.table.setHeaders(self.COLUMNS)
        self.table.setSortingEnabled(True)
        from PyQt6.QtWidgets import QHeaderView

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        for column in range(3, len(self.COLUMNS) - 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(self.COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        self.table.setRowMenu(self._menu_for_row, title="PAC entry")
        self.table.doubleClicked.connect(self._on_double_click)
        layout.addWidget(self.table, 1)

        self._package = None
        self.setPackage(None)

    # -- api ---------------------------------------------------------------

    def setPackage(self, package: object | None) -> None:
        """Renders a PacFile, or clears the view when given None."""
        self._package = package
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)

        if package is None:
            self._caption.setText(
                "No package loaded — use <b>Load PAC File…</b> to read one. "
                "Nothing is written to a device by loading a package."
            )
            self.table.setSortingEnabled(True)
            return

        header = package.header
        checks = []
        checks.append("header CRC ok" if header.header_crc_ok else "HEADER CRC BAD")
        if package.payload_crc_checked:
            checks.append("payload CRC ok" if package.payload_crc_ok else "PAYLOAD CRC BAD")
        else:
            # The honest wording: a header-only read cannot cover the payload.
            checks.append("payload CRC not checked (header-only read)")
        self._caption.setText(
            f"<b>{header.product_name or 'unnamed'}</b> · {header.product_version or '—'} · "
            f"{header.version_string or '—'} · {package.header.version.name} · "
            f"{_format_bytes(package.file_size)} · {len(package.entries)} entries · "
            f"{_format_bytes(package.total_download_bytes)} to write · "
            f"{' · '.join(checks)}"
        )

        for entry in package.entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            notes: list[str] = []
            if entry.is_marker:
                notes.append("marker — no data")
            if entry.is_logical_marker:
                notes.append("logical marker")
            if entry.can_omit:
                notes.append("may be omitted")
            if entry.file_id in package.unknown_ids:
                notes.append("unrecognised id")

            cells = (
                _SortableItem(entry.file_id),
                _SortableItem(entry.file_name or "—"),
                _SortableItem(entry.role.name),
                _SortableItem(_format_bytes(entry.size) if entry.size else "—", entry.size),
                _SortableItem(f"0x{entry.address:08x}" if entry.address else "—", entry.address),
                _SortableItem(f"0x{entry.data_offset:x}" if entry.size else "—",
                              entry.data_offset),
                _SortableItem("; ".join(notes)),
            )
            for column, item in enumerate(cells):
                item.setData(Qt.ItemDataRole.UserRole + 1, entry)
                self.table.setItem(row, column, item)
            # A marker carries no data, so it is dimmed: it is a step in the
            # sequence rather than something to be written.
            if entry.is_marker:
                for column in range(len(self.COLUMNS)):
                    cell = self.table.item(row, column)
                    if cell is not None:
                        cell.setForeground(QColor(tokens.active_theme().text_muted))

        self.table.setSortingEnabled(True)

    def package(self) -> object | None:
        return self._package

    def selected_entry(self) -> object | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return item.data(Qt.ItemDataRole.UserRole + 1) if item is not None else None

    def _menu_for_row(self, row: int) -> list[tuple[str, object]]:
        if row < 0 or row >= self.table.rowCount():
            return []
        item = self.table.item(row, 0)
        entry = item.data(Qt.ItemDataRole.UserRole + 1) if item is not None else None
        if entry is None:
            return []
        entries = [("Copy file ID", lambda: self._copy(entry.file_id))]
        if entry.file_name:
            entries.append(("Copy file name", lambda: self._copy(entry.file_name)))
        entries.append(("Show entry details", lambda: self.entry_activated.emit(entry)))
        return entries

    def _copy(self, text: str) -> None:
        from PyQt6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is not None and text:
            clipboard.setText(text)

    def _on_double_click(self, index) -> None:
        item = self.table.item(index.row(), 0)
        if item is not None:
            entry = item.data(Qt.ItemDataRole.UserRole + 1)
            if entry is not None:
                self.entry_activated.emit(entry)


class FirmwarePackageView(QWidget):
    """The contents of a Samsung firmware package.

    Two lists in one widget: the package's own summary, and one row per member.
    The point of the member list is the *partition mapping* - `boot.img` goes to
    the BOOT partition, `system.img` to SYSTEM - which is worked out by matching
    each member's base name against the partition names in the PIT. That mapping
    is shown rather than applied: this build does not write to a Samsung device,
    and a table that looked like a flash plan would imply otherwise.
    """

    COLUMNS = ("Member", "Size", "Partition", "Type")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(tokens.METRICS.gap_sm)

        self._caption = QLabel(self)
        self._caption.setProperty("role", "caption")
        self._caption.setWordWrap(True)
        layout.addWidget(self._caption)

        self.table = ui.StyledTable(0, len(self.COLUMNS), self)
        self.table.setHeaders(self.COLUMNS)
        self.table.setSortingEnabled(True)
        from PyQt6.QtWidgets import QHeaderView

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setRowMenu(self._menu_for_row, title="Package member")
        layout.addWidget(self.table, 1)

        self._package: Path | None = None
        self._partition_names: set[str] = set()
        self.setPackage(None, None)

    # -- api ---------------------------------------------------------------

    def setPartitionNames(self, names: object) -> None:
        """The partitions a PIT lists, for the mapping column.

        Called when the PIT changes, so a package loaded before the partition
        table is still mapped correctly.
        """
        self._partition_names = {str(name).upper() for name in (names or [])}
        if self._package is not None:
            self._remap()

    def setPackage(self, path: Path | None, archive: object | None) -> None:
        """Renders a package. `archive` is a TarArchive, or None when it could not be read."""
        self._package = path
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)

        if path is None:
            self._caption.setText(
                "No package loaded — use <b>Load Firmware Package…</b> to inspect a "
                ".tar.md5. Nothing is written to a device by loading one."
            )
            self.table.setSortingEnabled(True)
            return

        try:
            size = path.stat().st_size
        except OSError:
            size = 0

        if archive is None:
            self._caption.setText(
                f"<b>{path.name}</b> · {_format_bytes(size)} · "
                "<span>could not be read as a tar archive — see the log</span>"
            )
            self.table.setSortingEnabled(True)
            return

        files = archive.files()
        self._caption.setText(
            f"<b>{path.name}</b> · {_format_bytes(size)} · {len(files)} member(s) · "
            f"{_format_bytes(archive.total_file_bytes)} of payload · "
            f"digest {'verified' if getattr(archive, 'digest_ok', None) else 'not checked'}"
        )

        for entry in files:
            self._add_row(entry)
        self.table.setSortingEnabled(True)

    def package(self) -> Path | None:
        return self._package

    def _add_row(self, entry: object) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        partition = self._partition_for(entry.base_name)
        suffix = entry.base_name.rsplit(".", 1)[-1].upper() if "." in entry.base_name else "—"

        cells = (
            _SortableItem(entry.name),
            _SortableItem(_format_bytes(entry.size), entry.size),
            _tone_item(partition, partition, "ok" if partition != "—" else "text_muted"),
            _SortableItem(suffix),
        )
        for column, item in enumerate(cells):
            item.setData(Qt.ItemDataRole.UserRole + 1, entry)
            self.table.setItem(row, column, item)

    def _partition_for(self, name: str) -> str:
        """The PIT partition a member belongs to, or an em dash.

        The match is on the member's name without its extension, case-insensitively:
        a package holds `boot.img` and a PIT lists `BOOT`, and the two are the same
        partition under two spellings. It is deliberately a plain match rather than
        a fuzzy one - guessing that `sboot.bin` is the BOOTLOADER partition would be
        right and guessing that `cm.bin` is it would be wrong, and a wrong
        mapping in a table an operator is about to act on is worse than no mapping.
        """
        stem = name.rsplit(".", 1)[0].upper() if "." in name else name.upper()
        if stem in self._partition_names:
            return stem
        return "—"

    def _remap(self) -> None:
        """Recomputes the mapping column after the PIT changed."""
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            entry = item.data(Qt.ItemDataRole.UserRole + 1) if item is not None else None
            if entry is None:
                continue
            partition = self._partition_for(entry.base_name)
            cell = self.table.item(row, 2)
            if cell is not None:
                cell.setText(partition)
                cell.setForeground(
                    QColor(tokens.active_theme().colour(
                        "ok" if partition != "—" else "text_muted"))
                )

    def _menu_for_row(self, row: int) -> list[tuple[str, object]]:
        if row < 0 or row >= self.table.rowCount():
            return []
        item = self.table.item(row, 0)
        entry = item.data(Qt.ItemDataRole.UserRole + 1) if item is not None else None
        if entry is None:
            return []
        return [
            ("Copy member name", lambda: self._copy(entry.name)),
            ("Copy size in bytes", lambda: self._copy(str(entry.size))),
        ]

    def _copy(self, text: str) -> None:
        from PyQt6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is not None and text:
            clipboard.setText(text)


class PitTableView(QWidget):
    """A Samsung partition table, with the rows an operator acts on.

    The PIT is the file that decides what a device's partitions are, which makes
    it the one file in this tool that can leave a device unable to boot. So the
    view shows the flash size and the block count - the two fields that say
    whether the table belongs to this device - rather than only the names.
    """

    COLUMNS = ("Partition", "Identifier", "Block count", "Block size", "Size", "Image", "Type")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(tokens.METRICS.gap_sm)

        self._caption = QLabel(self)
        self._caption.setProperty("role", "caption")
        self._caption.setWordWrap(True)
        layout.addWidget(self._caption)

        self.table = ui.StyledTable(0, len(self.COLUMNS), self)
        self.table.setHeaders(self.COLUMNS)
        self.table.setSortingEnabled(True)
        from PyQt6.QtWidgets import QHeaderView

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(self.COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setRowMenu(self._menu_for_row, title="PIT partition")
        layout.addWidget(self.table, 1)

        self._pit = None
        self.setPit(None, None)

    def setPit(self, pit: object | None, source: str | None) -> None:
        """Renders a PitData, or clears the view when given None."""
        self._pit = pit
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)

        if pit is None:
            self._caption.setText(
                "No partition table — use <b>Read PIT From Device</b> or "
                "<b>Load PIT File…</b>. The PIT is what decides a device's partitions."
            )
            self.table.setSortingEnabled(True)
            return

        self._caption.setText(
            f"<b>{len(pit.entries)} partitions</b>"
            + (f" · from {source}" if source else "")
            + f" · {_format_bytes(sum(e.size_bytes for e in pit.entries))} "
              "of flash described"
        )

        for entry in pit.entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            cells = (
                _SortableItem(entry.partition_name),
                _SortableItem(str(entry.identifier)),
                _SortableItem(f"{entry.block_count:,}", entry.block_count),
                _SortableItem(f"{entry.block_size_or_offset:,}", entry.block_size_or_offset),
                _SortableItem(_format_bytes(entry.size_bytes), entry.size_bytes),
                _SortableItem(entry.flash_filename or "—"),
                _SortableItem(entry.device_type_name or str(entry.device_type)),
            )
            for column, item in enumerate(cells):
                item.setData(Qt.ItemDataRole.UserRole + 1, entry)
                self.table.setItem(row, column, item)
            # AP is the Android side and the one an operator looks for first.
            if entry.partition_name.upper() == "AP":
                for column in range(len(self.COLUMNS)):
                    cell = self.table.item(row, column)
                    if cell is not None:
                        cell.setForeground(QColor(tokens.active_theme().accent))

        self.table.setSortingEnabled(True)
        self.table.sortItems(2, Qt.SortOrder.AscendingOrder)

    def pit(self) -> object | None:
        return self._pit

    def partition_names(self) -> list[str]:
        if self._pit is None:
            return []
        return [entry.partition_name for entry in self._pit.entries]

    def selected_partition(self) -> object | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return item.data(Qt.ItemDataRole.UserRole + 1) if item is not None else None

    def _menu_for_row(self, row: int) -> list[tuple[str, object]]:
        if row < 0 or row >= self.table.rowCount():
            return []
        item = self.table.item(row, 0)
        entry = item.data(Qt.ItemDataRole.UserRole + 1) if item is not None else None
        if entry is None:
            return []
        return [
            ("Copy partition name", lambda: self._copy(entry.partition_name)),
            ("Copy PIT details", lambda: self._copy(
                f"{entry.partition_name} identifier={entry.identifier} "
                f"blocks={entry.block_count} "
                f"block_size={entry.block_size_or_offset}")),
        ]

    def _copy(self, text: str) -> None:
        from PyQt6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is not None and text:
            clipboard.setText(text)
