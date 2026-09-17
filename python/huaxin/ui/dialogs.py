"""Modal dialogs for the operations that change a device.

Two rules are applied consistently here:

* the exact command that will run is shown before it runs, and
* anything irreversible defaults to "no" and names what it destroys.

The partition-name rule is imported from the wrapper rather than re-implemented,
so the dialog cannot accept something the command would then reject.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from huaxin.core.adb_fastboot_wrapper import validate_partition_name
from huaxin.core.config import active_settings
from huaxin.ui.theme import COLORS

__all__ = ["FlashPartitionDialog", "ask_partition", "confirm_destructive"]

#: Destructive operations that were not confirmed. The lines go to the log file
#: alongside everything else, so turning the prompts off leaves a record of what
#: was done without one.
_audit = logging.getLogger("huaxin.audit")

#: Frequently written partitions. Not exhaustive - the field is editable, and
#: the authoritative list for any given device is its own partition table.
COMMON_PARTITIONS = (
    "boot",
    "init_boot",
    "vendor_boot",
    "recovery",
    "dtbo",
    "vbmeta",
    "vbmeta_system",
    "super",
    "system",
    "vendor",
    "product",
    "userdata",
    "cache",
    "metadata",
    "bootloader",
    "radio",
)


class FlashPartitionDialog(QDialog):
    """Collect a partition name and an image file, and preview the command."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        serial: str | None = None,
        suggested_partition: str = "boot",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Flash partition")
        self.setMinimumWidth(560)

        self._partition = QComboBox(self)
        self._partition.setEditable(True)
        self._partition.addItems(COMMON_PARTITIONS)
        self._partition.setCurrentText(suggested_partition)
        self._partition.currentTextChanged.connect(self._revalidate)

        self._image = QLineEdit(self)
        self._image.setPlaceholderText("path to the .img file")
        self._image.textChanged.connect(self._revalidate)
        browse = QPushButton("Browse…", self)
        browse.clicked.connect(self._choose_image)

        image_row = QHBoxLayout()
        image_row.setContentsMargins(0, 0, 0, 0)
        image_row.setSpacing(6)
        image_row.addWidget(self._image, 1)
        image_row.addWidget(browse)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.addRow("Partition", self._partition)
        form.addRow("Image", image_row)

        target = QLabel(
            f"Target device: <b>{serial}</b>" if serial
            else "Target device: the only device fastboot can see",
            self,
        )
        target.setObjectName("PanelSubtitle")

        self._preview = QLabel(self)
        self._preview.setObjectName("CommandPreview")
        self._preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._preview.setWordWrap(True)

        warning = QLabel(
            "⚠  This writes to the selected partition. Flashing an image that does not "
            "match this device can leave it unbootable.",
            self,
        )
        warning.setObjectName("DialogWarning")
        warning.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_button.setText("Flash")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addWidget(target)
        layout.addLayout(form)
        layout.addWidget(self._preview)
        layout.addWidget(warning)
        layout.addWidget(buttons)

        self._revalidate()

    # -- api ---------------------------------------------------------------

    def values(self) -> tuple[str, str]:
        """(partition, image path) - only meaningful after accept()."""
        return self._partition.currentText().strip(), self._image.text().strip()

    # -- internals ---------------------------------------------------------

    def _choose_image(self) -> None:
        path, _ = filedialog.open_file_name(
            self, "Select image", str(Path.home()), "Images (*.img);;All files (*)"
        )
        if path:
            self._image.setText(path)

    def _revalidate(self) -> None:
        partition_text = self._partition.currentText().strip()
        image_text = self._image.text().strip()

        try:
            partition = validate_partition_name(partition_text)
            partition_problem = ""
        except ValueError as exc:
            partition, partition_problem = "", str(exc)

        image_problem = ""
        if image_text:
            path = Path(image_text)
            if not path.is_file():
                image_problem = f"no such file: {path}"
            else:
                image_problem = ""

        problems = [problem for problem in (partition_problem, image_problem) if problem]
        self._ok_button.setEnabled(bool(partition) and bool(image_text) and not problems)

        if problems:
            self._preview.setText("⚠  " + problems[0])
            self._preview.setStyleSheet(f"color: {COLORS['warn']};")
        elif partition and image_text:
            size_mb = Path(image_text).stat().st_size / (1024 * 1024)
            self._preview.setText(
                f"fastboot flash {partition} {Path(image_text).name}   ({size_mb:.1f} MiB)"
            )
            self._preview.setStyleSheet(f"color: {COLORS['text_dim']};")
        else:
            self._preview.setText("Choose a partition and an image file.")
            self._preview.setStyleSheet(f"color: {COLORS['text_dim']};")


def ask_partition(
    parent: QWidget,
    *,
    title: str,
    prompt: str,
    suggestion: str = "userdata",
) -> str | None:
    """Ask for one partition name. Returns None if cancelled or invalid."""
    current = COMMON_PARTITIONS.index(suggestion) if suggestion in COMMON_PARTITIONS else 0
    text, accepted = QInputDialog.getItem(parent, title, prompt, COMMON_PARTITIONS, current, True)
    if not accepted:
        return None
    try:
        return validate_partition_name(text)
    except ValueError as exc:
        QMessageBox.warning(parent, "Invalid partition", str(exc))
        return None


def confirm_destructive(
    parent: QWidget,
    *,
    title: str,
    message: str,
    command: str = "",
    confirm_label: str = "Proceed",
) -> bool:
    """Ask before something irreversible. Defaults to No.

    The operator can turn these prompts off in Settings - that is what
    `confirm_destructive_operations` is for, and it carries the one tooltip in
    the dialog that says a mistake becomes unrecoverable. When it is off the
    question is skipped, but the action is still recorded in the log together
    with the command that ran, so what was destroyed is still on the record even
    though nobody was asked.
    """
    settings = active_settings()
    if not settings.confirm_destructive_operations:
        _audit.warning(
            "destructive operation not confirmed (turned off in Settings): %s%s",
            title,
            f" - {command}" if command else "",
        )
        return True

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(message)
    if command:
        box.setInformativeText(f"Command:\n{command}")
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    box.button(QMessageBox.StandardButton.Yes).setText(confirm_label)
    box.button(QMessageBox.StandardButton.No).setText("Cancel")
    return box.exec() == QMessageBox.StandardButton.Yes
