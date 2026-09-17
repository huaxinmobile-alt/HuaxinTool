"""Runs the real application briefly and reports what it printed.

Used to check the packaged and unpackaged builds behave the same way at
startup and at shutdown - in particular that no pybind11 GIL assertion fires
while the interpreter is being torn down.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Run headless: this is a startup and shutdown check, not a visual one.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from huaxin.core.backend import BackendService  # noqa: E402
from huaxin.ui.main_window import MainWindow  # noqa: E402


def main() -> int:
    app = QApplication([])
    service = BackendService()
    window = MainWindow(service)
    window.show()
    service.start()

    def scan() -> None:
        service.request_scan()

    QTimer.singleShot(800, scan)

    def finish() -> None:
        # Exercises the settings path too, since that is the newest route into
        # the Logger and the backend.
        from huaxin.core.config import Settings

        service.apply_settings(Settings())
        window.close()
        service.shutdown()
        app.quit()

    QTimer.singleShot(2500, finish)
    app.exec()

    log = Path("startup_check.log")
    path = service.log_path
    log.write_text(
        f"backend state: {service.state}\n"
        f"log path: {path}\n"
        f"log exists: {Path(path).exists() if path else False}\n"
        f"libusb: {service.libusb_version}\n",
        encoding="utf-8",
    )
    print(log.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
