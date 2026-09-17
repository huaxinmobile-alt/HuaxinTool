"""The file browser, styled to match the tool.

WHY NOT `QFileDialog`. Two reasons, and the second is the one that matters.

The first is cosmetic: the native dialog on Windows is drawn by the shell, so a
dark-themed application opens a light dialog, and on a machine with a scaling
factor above 100% it does not follow the application's font size either.

The second is that an operator choosing firmware is choosing something they
cannot take back, and the file name is a poor guide to what is in the file.
"SP9832E_1_1_64bit_USER_20200415.pac" says nothing about whether it is the right
package for the device on the bench. So this browser reads the file's own header
and says what is inside it - how many entries, how large the payload, what the
first partitions are - before the operator commits to it.

WHAT IS NOT GUESSED. The preview only claims what the parsers actually return.
For a format this tool cannot read, it shows the size and the date and says
nothing about the contents, rather than inferring from the extension.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QDir, QModelIndex, QSettings, Qt, pyqtSignal
# QFileSystemModel lives in QtGui as of Qt 6; in Qt 5 it was a widget.
from PyQt6.QtGui import QFileSystemModel
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSplitter,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from huaxin.core.progress import format_bytes
from huaxin.ui.components import StyledButton, button_row
from huaxin.ui.safe_slot import safe_slot

__all__ = [
    "FileDialog",
    "FIRMWARE_FILTERS",
    "get_existing_directory",
    "get_open_file_name",
    "get_save_file_name",
    "recent_files",
    "remember_file",
    "forget_recent_files",
]


#: (label, patterns). The first entry is the one that matters and is what the
#: dialog opens on; "All files" is last because it is the escape hatch, not the
#: default - an operator who has to pick "All files" to see their package has
#: been given the wrong filter.
FIRMWARE_FILTERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Firmware packages",
        ("*.pac", "*.tar.md5", "*.tar", "*.zip", "*.tgz"),
    ),
    ("Flash images", ("*.img", "*.bin", "*.mbn", "*.elf", "*.hex")),
    ("Samsung packages", ("*.tar.md5", "*.tar", "*.pit", "*.md5")),
    ("Unisoc packages", ("*.pac", "*.xml")),
    ("Qualcomm images", ("*.mbn", "*.elf", "*.xml", "*.img")),
    ("MediaTek packages", ("*.bin", "*.img", "*.txt")),
    ("Scripts", ("*.py", "*.bat", "*.sh", "*.ps1")),
    ("All files", ("*",)),
)

#: Everything the preview knows how to open. Kept next to the filters so adding a
#: format to one is one line from being added to the other.
_PACKAGE_SUFFIXES = (".pac", ".tar", ".tar.md5", ".tgz", ".zip")


def _settings() -> QSettings:
    return QSettings()


def recent_files() -> list[str]:
    """The files this tool was previously pointed at, newest first.

    Newest first because the second time somebody flashes a device they want the
    file from five minutes ago, not the first one they ever opened.
    """
    stored = _settings().value("files/recent", [])
    if isinstance(stored, str):
        stored = [stored]
    result: list[str] = []
    for entry in stored or []:
        text = str(entry)
        # Filtered on read rather than pruned on write: a file on a disconnected
        # drive should come back when the drive does.
        if text and text not in result:
            result.append(text)
    return result


def remember_file(path: str | Path, *, limit: int = 10) -> None:
    """Records a file at the top of the recent list."""
    text = str(path)
    if not text:
        return
    entries = [entry for entry in recent_files() if entry != text]
    entries.insert(0, text)
    _settings().setValue("files/recent", entries[:limit])


def forget_recent_files() -> None:
    _settings().remove("files/recent")


def _patterns(filters: tuple[tuple[str, tuple[str, ...]], ...]) -> list[str]:
    """Every pattern across every filter, for the filesystem model's own filter."""
    seen: list[str] = []
    for _, group in filters:
        for pattern in group:
            if pattern not in seen:
                seen.append(pattern)
    return seen


class FileDialog(QDialog):
    """A themed file browser: places, a details list, and a package preview.

    Used for open, save and directory selection rather than three classes: the
    only differences are which controls are disabled and what the accept button
    says, and three subclasses would be three places for those to drift apart.
    """

    #: Emitted with the path of whatever is selected, so a caller can react to a
    #: selection without polling the dialog.
    selection_changed = pyqtSignal(str)

    OPEN = "open"
    SAVE = "save"
    DIRECTORY = "directory"

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        mode: str = OPEN,
        title: str = "Open file",
        start: str = "",
        filters: tuple[tuple[str, tuple[str, ...]], ...] = FIRMWARE_FILTERS,
        name: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(880, 560)
        self._mode = mode
        self._filters = filters
        self._result_paths: list[str] = []
        self._preview_cache: dict[str, str] = {}

        self._places = self._build_places()
        self._recents = self._build_recents()
        self._sidebar = self._compose_sidebar()
        self._path_edit = self._build_path_bar()
        self._filter_box = self._build_filter_bar()
        self._model, self._view = self._build_list()
        self._preview = self._build_preview()

        listing = QWidget(self)
        listing_layout = QVBoxLayout(listing)
        listing_layout.setContentsMargins(0, 0, 0, 0)
        listing_layout.setSpacing(8)
        listing_layout.addWidget(self._path_edit)
        if self._filter_box is not None:
            listing_layout.addWidget(self._filter_box)
        listing_layout.addWidget(self._view, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self._sidebar)
        splitter.addWidget(listing)
        splitter.addWidget(self._preview)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([190, 460, 260])

        self._accept_button = StyledButton(
            {"open": "Open", "save": "Save", "directory": "Choose"}[mode],
            self,
            role="primary",
        )
        self._accept_button.clicked.connect(safe_slot(self._on_accept))
        cancel = StyledButton("Cancel", self, role="secondary")
        cancel.clicked.connect(safe_slot(self.reject))

        layout = QVBoxLayout(self)
        layout.addWidget(splitter, 1)
        if mode == self.SAVE:
            filename_row = QHBoxLayout()
            filename_row.addWidget(QLabel("File name", self))
            self._name_edit = QLineEdit(name, self)
            self._name_edit.setPlaceholderText("Type a name, or pick a file to replace")
            filename_row.addWidget(self._name_edit, 1)
            layout.addLayout(filename_row)
        else:
            self._name_edit = None
        layout.addWidget(button_row(cancel, self._accept_button))

        self.set_start(start)
        self._view.selectionModel().selectionChanged.connect(safe_slot(self._on_selection))
        self._view.doubleClicked.connect(safe_slot(self._on_double_click))
        self._recents.itemActivated.connect(safe_slot(self._on_recent_activated))
        self._places.selectionModel().selectionChanged.connect(safe_slot(self._on_place_chosen))

    # -- construction ------------------------------------------------------

    def _build_places(self) -> QTreeView:
        """The shortcuts column: home, the usual folders, and the drives.

        Backed by a filesystem model in `DirsOnly` mode, which means the drives of
        a machine this tool has never seen appear without a list of them being
        written down here - and a network drive that takes ten seconds to answer
        does not block the dialog while it does.
        """
        model = QFileSystemModel(self)
        model.setFilter(QDir.Filter.Drives | QDir.Filter.Dirs | QDir.Filter.NoDotAndDotDot)
        model.setRootPath("")

        view = QTreeView(self)
        view.setObjectName("FilePlaces")
        view.setModel(model)
        view.setHeaderHidden(True)
        view.setAnimated(False)
        view.setUniformRowHeights(True)
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for column in range(1, model.columnCount()):
            view.hideColumn(column)

        self._places_model = model
        self._places_view = view

        home = Path.home()
        for name, path in (
            ("Home", home),
            ("Desktop", home / "Desktop"),
            ("Downloads", home / "Downloads"),
        ):
            if path.is_dir():
                view.expand(model.index(str(path)))
        return view

    def _build_recents(self) -> QListWidget:
        """Recent files, above the places column."""
        widget = QListWidget(self)
        widget.setObjectName("FileList")
        widget.setMaximumHeight(170)
        widget.setToolTip("Files this tool has opened before")
        self._recent_widget = widget

        holder = QWidget(self)
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        heading = QLabel("Recent", holder)
        heading.setProperty("role", "heading")
        layout.addWidget(heading)
        layout.addWidget(widget)

        self._recents_layout_holder = holder
        self._fill_recents()
        return widget

    def _fill_recents(self) -> None:
        widget = getattr(self, "_recent_widget", None)
        if widget is None:
            return
        widget.clear()
        for path in recent_files():
            item = QListWidgetItem(Path(path).name)
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path)
            widget.addItem(item)
        if widget.count() == 0:
            item = QListWidgetItem("Nothing yet")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            widget.addItem(item)

    def _compose_sidebar(self) -> QWidget:
        """Recent files above the places tree, in one column.

        Together because they are the same job - "take me somewhere quickly" - and
        because the places tree on its own is mostly empty space on a machine with
        one drive.
        """
        holder = QWidget(self)
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self._recents_layout_holder)
        layout.addWidget(self._places, 1)
        return holder

    def _build_path_bar(self) -> QWidget:
        """Up button, editable path, and a Home shortcut."""
        self._up = StyledButton("", self, role="icon", icon="upload", lift=0)
        self._up.setToolTip("Go to the parent folder")
        self._up.clicked.connect(safe_slot(self._go_up))

        self._path = QLineEdit(self)
        self._path.setPlaceholderText("Path")
        self._path.returnPressed.connect(safe_slot(self._on_path_entered))

        home = StyledButton("Home", self, role="ghost", size="small")
        home.clicked.connect(safe_slot(lambda: self.set_directory(Path.home())))

        holder = QWidget(self)
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self._up)
        row.addWidget(self._path, 1)
        row.addWidget(home)
        return holder

    def _build_filter_bar(self) -> QWidget | None:
        if self._mode == self.DIRECTORY:
            self._filter = None
            return None
        self._filter = QComboBox(self)
        for label, patterns in self._filters:
            self._filter.addItem(label, list(patterns))
        self._filter.setToolTip("Which files are listed")
        self._filter.currentIndexChanged.connect(safe_slot(self._on_filter_changed))

        holder = QWidget(self)
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(QLabel("Show", holder))
        row.addWidget(self._filter, 1)
        return holder

    def _build_list(self) -> tuple[QFileSystemModel, QTreeView]:
        """The file list itself: a details view, because size is the column that
        matters when the files are gigabytes."""
        model = QFileSystemModel(self)
        model.setFilter(QDir.Filter.AllDirs | QDir.Filter.Files | QDir.Filter.NoDotAndDotDot)
        model.setNameFilterDisables(False)

        view = QTreeView(self)
        view.setObjectName("FileList")
        view.setModel(model)
        view.setRootIsDecorated(False)
        view.setItemsExpandable(False)
        view.setUniformRowHeights(True)
        view.setSortingEnabled(True)
        view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        view.setAlternatingRowColors(True)
        view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        return model, view

    def _build_preview(self) -> QFrame:
        frame = QFrame(self)
        frame.setObjectName("FilePreview")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        self._preview_name = QLabel("Nothing selected", frame)
        self._preview_name.setObjectName("PreviewName")
        self._preview_name.setWordWrap(True)
        layout.addWidget(self._preview_name)

        self._preview_body = QLabel("", frame)
        self._preview_body.setWordWrap(True)
        self._preview_body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._preview_body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._preview_body, 1)

        self._preview_note = QLabel("", frame)
        self._preview_note.setWordWrap(True)
        layout.addWidget(self._preview_note)
        return frame

    # -- api ---------------------------------------------------------------

    def set_start(self, start: str) -> None:
        """Opens at `start`: a file's folder, a folder, or the home directory."""
        target = Path(start) if start else Path.home()
        if target.is_file():
            self.set_directory(target.parent)
            if self._mode == self.OPEN:
                self._select_path(target)
            elif self._name_edit is not None:
                self._name_edit.setText(target.name)
        elif target.is_dir():
            self.set_directory(target)
        else:
            self.set_directory(Path.home())

    def set_directory(self, path: Path) -> None:
        if not path.is_dir():
            return
        text = str(path)
        index = self._model.setRootPath(text)
        self._view.setRootIndex(index)
        self._path.setText(text)
        self._places_view.setCurrentIndex(self._places_model.index(text))
        if self._name_edit is not None and not self._name_edit.text():
            self._name_edit.setText("")
        self._clear_preview()

    def directory(self) -> Path:
        return Path(self._path.text().strip() or Path.home())

    def selected_paths(self) -> list[str]:
        return list(self._result_paths)

    def accept_path(self, path: Path) -> None:
        """Accepts without a click, for a caller that already knows the file."""
        self._result_paths = [str(path)]
        self.accept()

    # -- behaviour ---------------------------------------------------------

    def _on_place_chosen(self) -> None:
        indexes = self._places_view.selectionModel().selectedIndexes()
        if not indexes:
            return
        path = self._places_model.filePath(indexes[0])
        if path:
            self.set_directory(Path(path))

    def _on_recent_activated(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path:
            return
        target = Path(str(path))
        if target.is_file():
            self.set_directory(target.parent)
            self._select_path(target)
        elif target.is_dir():
            self.set_directory(target)
        else:
            self._preview_note.setText(f"{target} is no longer there.")

    def _on_path_entered(self) -> None:
        target = Path(self._path.text().strip())
        if target.is_dir():
            self.set_directory(target)
        elif target.is_file():
            self.set_directory(target.parent)
            self._select_path(target)
        else:
            self._path.setText(str(self.directory()))

    def _go_up(self) -> None:
        parent = self.directory().parent
        if parent != self.directory():
            self.set_directory(parent)

    def _on_filter_changed(self) -> None:
        patterns = self._filter.currentData() if self._filter is not None else None
        self._model.setNameFilters(list(patterns or ["*"]))
        self._clear_preview()

    def _on_double_click(self, index: QModelIndex) -> None:
        path = Path(self._model.filePath(index))
        if path.is_dir():
            self.set_directory(path)
        else:
            self._on_accept()

    def _on_selection(self) -> None:
        indexes = self._view.selectionModel().selectedIndexes()
        if not indexes:
            self._result_paths = []
            self._clear_preview()
            return
        path = Path(self._model.filePath(indexes[0]))
        self._result_paths = [str(path)]
        self.selection_changed.emit(str(path))
        if self._name_edit is not None and path.is_file():
            self._name_edit.setText(path.name)
        self._show_preview(path)

    def _on_accept(self) -> None:
        if self._mode == self.DIRECTORY:
            self._result_paths = [str(self.directory())]
            self.accept()
            return

        if self._mode == self.SAVE:
            name = (self._name_edit.text() if self._name_edit else "").strip()
            if not name:
                self._preview_note.setText("Type a file name first.")
                return
            target = self.directory() / name
            self._result_paths = [str(target)]
            remember_file(target)
            self.accept()
            return

        if not self._result_paths:
            self._preview_note.setText("Select a file first.")
            return
        path = Path(self._result_paths[0])
        if not path.is_file():
            self.set_directory(path)
            return
        remember_file(path)
        self.accept()

    def _select_path(self, path: Path) -> None:
        index = self._model.index(str(path))
        if not index.isValid():
            return
        self._view.setCurrentIndex(index)
        self._view.selectionModel().select(
            index,
            self._view.selectionModel().SelectionFlag.ClearAndSelect
            | self._view.selectionModel().SelectionFlag.Rows,
        )
        self._view.scrollTo(index)

    # -- preview -----------------------------------------------------------

    def _clear_preview(self) -> None:
        self._preview_name.setText("Nothing selected")
        self._preview_body.setText("")
        self._preview_note.setText("")

    def _show_preview(self, path: Path) -> None:
        if not path.is_file():
            self._clear_preview()
            return
        try:
            stat = path.stat()
        except OSError as exc:
            self._preview_name.setText(path.name)
            self._preview_body.setText("")
            self._preview_note.setText(f"Could not read the file's details: {exc}")
            return

        self._preview_name.setText(path.name)
        lines = [
            f"<b>Size</b> {format_bytes(stat.st_size)}",
            f"<b>Modified</b> {datetime.fromtimestamp(stat.st_mtime):%Y-%m-%d %H:%M}",
        ]
        self._preview_body.setText("<br>".join(lines))
        self._preview_note.setText("")

        if path.suffix.lower() in (".pac", ".tar", ".md5") or path.name.lower().endswith(".tar.md5"):
            self._preview_note.setText("Reading the package header…")
            # Repainting before the read matters: a header read on a slow network
            # path takes long enough that the pane would otherwise appear frozen.
            self._preview_note.repaint()
            self._preview_note.setText(self._package_note(path, stat.st_size))

    def _package_note(self, path: Path, size: int) -> str:
        """What the parsers can say about a package's contents.

        Every failure is reported as a sentence rather than raised: a preview that
        throws while the operator is browsing would take the dialog with it, and
        the point of the pane is to help them choose, not to validate.
        """
        cached = self._preview_cache.get(str(path))
        if cached is not None:
            return cached

        note = ""
        try:
            from huaxin.core import parsers
        except ImportError:  # pragma: no cover - the package is always importable
            return ""

        if not parsers.available():
            note = "Package details need the native module, which is not built."
        elif path.suffix.lower() == ".pac":
            note = self._pac_note(parsers, path)
        elif path.name.lower().endswith((".tar", ".tar.md5")):
            note = self._tar_note(parsers, path)
        else:
            note = ""

        self._preview_cache[str(path)] = note
        return note

    @staticmethod
    def _pac_note(parsers, path: Path) -> str:
        try:
            # `read_pac` is the header-only reader in this module - it calls the
            # native read_pac_header, which reads a few kilobytes rather than the
            # whole package. A preview must never pull four gigabytes into memory
            # just because the operator clicked a file.
            package = parsers.read_pac(path)
        except Exception as exc:
            return f"Not readable as a Unisoc PAC package — {exc}"
        header = package.header
        count = len(package.entries)
        names = ", ".join(entry.file_name or entry.file_id for entry in package.entries[:4])
        more = f" and {count - 4} more" if count > 4 else ""
        # The header CRC is stated and the payload CRC is not: this is a
        # header-only read, so claiming the payload was checked would be a lie
        # told by a dialog that is about to be trusted with a device.
        integrity = "header CRC ok" if header.header_crc_ok else "HEADER CRC BAD"
        return (
            f"<b>Unisoc PAC package</b><br>"
            f"{count} entr{'y' if count == 1 else 'ies'} · "
            f"{format_bytes(package.total_download_bytes)} to write<br>"
            f"{names}{more}<br>"
            f"{header.product_name or 'unnamed'} · {header.product_version or '—'} · "
            f"{header.version_string or '—'}<br>"
            f"{integrity} · payload CRC not checked (header-only read)"
        )

    @staticmethod
    def _tar_note(parsers, path: Path) -> str:
        try:
            archive = parsers.read_package(path)
        except Exception as exc:
            return f"Not readable as a Samsung package — {exc}"
        members = archive.files()
        names = ", ".join(member.base_name for member in members[:4])
        more = f" and {len(members) - 4} more" if len(members) > 4 else ""
        terminated = "complete" if archive.terminated else "no end-of-archive marker"
        return (
            f"<b>Samsung Odin package</b><br>"
            f"{len(members)} member{'s' if len(members) != 1 else ''} · "
            f"{format_bytes(archive.total_file_bytes)} of payload<br>"
            f"{names}{more}<br>"
            f"{terminated}"
        )


# -----------------------------------------------------------------------------
#  Module-level entry points
#
#  Shaped like the Qt calls they replace, so a call site changes one line:
#
#      path, _ = QFileDialog.getOpenFileName(self, "Open", start, "Images (*.img)")
#      path = filedialog.get_open_file_name(self, title="Open", start=start)[0]
#
#  The recent-files list is written here rather than by the caller, because a
#  caller that forgets to is a caller whose file never appears in Recent.
# -----------------------------------------------------------------------------


def _run(dialog: FileDialog) -> list[str]:
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return []
    return dialog.selected_paths()


def get_open_file_name(
    parent: QWidget | None = None,
    *,
    title: str = "Open file",
    start: str = "",
    filters: tuple[tuple[str, tuple[str, ...]], ...] = FIRMWARE_FILTERS,
) -> tuple[str, str]:
    """Returns (path, filter label). The path is "" when cancelled."""
    dialog = FileDialog(parent, mode=FileDialog.OPEN, title=title, start=start, filters=filters)
    paths = _run(dialog)
    label = filters[dialog._filter.currentIndex()][0] if paths and dialog._filter else ""
    return (paths[0] if paths else "", label)


def get_save_file_name(
    parent: QWidget | None = None,
    *,
    title: str = "Save file",
    start: str = "",
    name: str = "",
    filters: tuple[tuple[str, tuple[str, ...]], ...] = FIRMWARE_FILTERS,
) -> str:
    dialog = FileDialog(
        parent, mode=FileDialog.SAVE, title=title, start=start, filters=filters, name=name
    )
    paths = _run(dialog)
    return paths[0] if paths else ""


def get_existing_directory(
    parent: QWidget | None = None,
    *,
    title: str = "Choose a folder",
    start: str = "",
) -> str:
    dialog = FileDialog(parent, mode=FileDialog.DIRECTORY, title=title, start=start)
    paths = _run(dialog)
    return paths[0] if paths else ""


def choose_firmware_file(
    parent: QWidget | None = None,
    *,
    title: str = "Open firmware package",
    start: str = "",
    tooltip: str = "",
) -> str:
    """The one call every panel makes. Returns "" when nothing was chosen."""
    del tooltip  # accepted so call sites can pass the button's tooltip through
    path, _ = get_open_file_name(parent, title=title, start=start)
    return path


# -----------------------------------------------------------------------------
#  Qt-shaped adapters
#
#  These two exist so replacing a native dialog is a one-word change at the call
#  site - `QFileDialog.getOpenFileName(...)` becomes
#  `filedialog.open_file_name(...)` with the arguments untouched. The filter
#  string is Qt's own format ("Images (*.img *.bin);;All files (*)"), parsed here
#  rather than rewritten at thirty call sites, because thirty hand-edits to
#  filter strings is thirty chances to drop a pattern.
# -----------------------------------------------------------------------------


def parse_qt_filters(text: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Converts Qt's filter string into the (label, patterns) form used here.

    Anything unparseable is kept as a whole-file filter rather than dropped: a
    filter that shows too much is a nuisance, and one that shows nothing looks
    like an empty folder.
    """
    groups: list[tuple[str, tuple[str, ...]]] = []
    for chunk in (text or "").split(";;"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "(" in chunk and chunk.endswith(")"):
            label, _, rest = chunk.partition("(")
            patterns = tuple(part for part in rest[:-1].replace(",", " ").split() if part)
            groups.append((label.strip() or "Files", patterns or ("*",)))
        else:
            groups.append((chunk, ("*",)))
    if not groups:
        groups.append(("All files", ("*",)))
    return tuple(groups)


def open_file_name(
    parent: QWidget | None,
    title: str = "Open file",
    start: str = "",
    qt_filters: str = "",
) -> tuple[str, str]:
    """`QFileDialog.getOpenFileName`, drawn by this application."""
    return get_open_file_name(
        parent,
        title=title,
        start=start,
        filters=parse_qt_filters(qt_filters) if qt_filters else FIRMWARE_FILTERS,
    )


def save_file_name(
    parent: QWidget | None,
    title: str = "Save file",
    start: str = "",
    qt_filters: str = "",
) -> tuple[str, str]:
    """`QFileDialog.getSaveFileName`. The second element is the filter label."""
    start_path = Path(start) if start else Path.home()
    path = get_save_file_name(
        parent,
        title=title,
        start=str(start_path.parent) if start_path.suffix else start,
        name=start_path.name if start_path.suffix else "",
        filters=parse_qt_filters(qt_filters) if qt_filters else FIRMWARE_FILTERS,
    )
    return (path, "")
