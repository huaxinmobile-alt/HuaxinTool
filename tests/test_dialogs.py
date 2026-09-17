"""Verify the destructive-operation dialogs.

These dialogs are the last thing between an operator and an erased partition
table, so the properties worth asserting are the safety ones: the Flashing
button stays disabled until the input is valid, and a confirmation defaults to
"no".

Modal dialogs cannot be clicked through headlessly, but their widgets and logic
are ordinary objects, and QMessageBox's result can be substituted, which is
enough to check both.

    python tests/test_dialogs.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from PyQt6.QtWidgets import QApplication, QDialogButtonBox, QLabel, QMessageBox  # noqa: E402

from huaxin.ui.dialogs import FlashPartitionDialog, confirm_destructive  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []
TMP_IMAGE = Path(__file__).resolve().parent / "_flash_dialog_probe.img"


def check(description: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((bool(passed), description, detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {description}" + (f"  ({detail})" if detail else ""), flush=True)


def flash_button(dialog: FlashPartitionDialog):
    return dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)


def main() -> int:
    app = QApplication([])  # noqa: F841
    TMP_IMAGE.write_bytes(b"\0" * (2 * 1024 * 1024))

    try:
        print("1. flash dialog validation")
        dialog = FlashPartitionDialog(None, serial="34ABC1234567")
        labels = [label.text() for label in dialog.findChildren(QLabel)]
        check("the target device is shown", any("34ABC1234567" in text for text in labels),
              next((t for t in labels if "34ABC" in t), "not found"))
        check("the warning is present", any("unbootable" in text for text in labels))
        check("valid partition, no image -> button disabled", not flash_button(dialog).isEnabled())

        no_target = FlashPartitionDialog(None)
        check("with no serial it says how the device is chosen",
              any("only device" in text for text in (label.text() for label in no_target.findChildren(QLabel))))

        dialog._image.setText(str(TMP_IMAGE))
        check("valid partition + real file -> button enabled", flash_button(dialog).isEnabled())
        check("the preview shows the exact command",
              f"fastboot flash boot {TMP_IMAGE.name}" in dialog._preview.text(),
              dialog._preview.text())
        check("the preview shows the image size", "2.0 MiB" in dialog._preview.text())
        check("values() returns what will run", dialog.values() == ("boot", str(TMP_IMAGE)))

        print("\n2. flash dialog rejects bad input")
        for bad in ("--slot", "-w", "boot partition", "x" * 80):
            dialog._partition.setCurrentText(bad)
            check(f"{bad[:16]!r} disables the button", not flash_button(dialog).isEnabled())
            check(f"{bad[:16]!r} explains why", dialog._preview.text().startswith("⚠"))

        dialog._partition.setCurrentText("boot")
        dialog._image.setText("C:/does/not/exist.img")
        check("a missing image disables the button", not flash_button(dialog).isEnabled())
        # Only the file name is asserted: Path renders the separators the way the
        # platform does, so the full string differs between Windows and Linux.
        check("the missing path is named", "exist.img" in dialog._preview.text(),
              dialog._preview.text())

        dialog._image.setText(str(TMP_IMAGE))
        check("recovering valid input re-enables it", flash_button(dialog).isEnabled())

        print("\n3. partition name rule is shared with the command layer")
        # The dialog must not accept something the wrapper would reject later.
        from huaxin.core.adb_fastboot_wrapper import validate_partition_name

        for candidate in ("boot", "vbmeta_system", "init_boot"):
            dialog._partition.setCurrentText(candidate)
            ok = flash_button(dialog).isEnabled()
            try:
                validate_partition_name(candidate)
                accepted_by_wrapper = True
            except ValueError:
                accepted_by_wrapper = False
            check(f"{candidate!r}: dialog and wrapper agree", ok == accepted_by_wrapper)

        print("\n4. confirmation defaults to no")
        captured: dict[str, object] = {}

        def fake_exec(self) -> int:
            captured["default"] = self.defaultButton().text()
            captured["buttons"] = [b.text() for b in self.buttons()]
            captured["text"] = self.text()
            captured["informative"] = self.informativeText()
            return int(QMessageBox.StandardButton.No)

        original = QMessageBox.exec
        QMessageBox.exec = fake_exec
        try:
            answer = confirm_destructive(
                None,
                title="Wipe user data",
                message="This erases <b>userdata</b> and cache.",
                command="fastboot -w",
                confirm_label="Erase everything",
            )
        finally:
            QMessageBox.exec = original

        check("returning No yields False", answer is False)
        check("the default button is No/Cancel", "Cancel" in str(captured.get("default")),
              str(captured.get("default")))
        check("the confirm button is labelled explicitly",
              "Erase everything" in str(captured.get("buttons")), str(captured.get("buttons")))
        check("the command is shown before it runs", "fastboot -w" in str(captured.get("informative")))

        def fake_yes(self) -> int:
            return int(QMessageBox.StandardButton.Yes)

        QMessageBox.exec = fake_yes
        try:
            answer = confirm_destructive(None, title="t", message="m", command="c")
        finally:
            QMessageBox.exec = original
        check("returning Yes yields True", answer is True)

    finally:
        TMP_IMAGE.unlink(missing_ok=True)

    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("\nFailures:")
        for _, description, detail in failed:
            print(f"  - {description} {detail}")
        return 1
    print("Dialogs OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
