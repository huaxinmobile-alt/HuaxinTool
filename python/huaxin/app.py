"""Application bootstrap: create the QApplication, the service and the window."""

from __future__ import annotations

import logging
import sys
import traceback

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QApplication

from huaxin import __version__
from huaxin.core.backend import BackendService
from huaxin.core.config import load_settings, set_active_settings
from huaxin.ui.main_window import MainWindow
from huaxin.ui.safe_slot import set_error_reporter
from huaxin.ui import style as ui_style
from huaxin.ui.theme import build_stylesheet

__all__ = ["main"]

#: Python's own logging levels mapped onto the names the tool's log uses.
_LEVEL_NAMES = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "critical",
}


class _ServiceLogHandler(logging.Handler):
    """Sends Python `logging` records to the same log as everything else.

    Without this, a module that logs through the standard library - and any
    third-party library that does - writes to a handler nobody installed, so the
    message is silently dropped. The tool has exactly one log; this is what makes
    that true rather than aspirational.
    """

    def __init__(self, service: BackendService) -> None:
        super().__init__()
        self._service = service

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = _LEVEL_NAMES.get(record.levelno, "info")
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
            # The logger's name matters when more than one module reports: a bare
            # message in a log that mixes Qt, USB and audit lines is not
            # attributable to anything.
            if record.name and record.name != "root":
                message = f"[{record.name}] {message}"
            self._service.log.emit(level, message)
        except Exception:  # noqa: BLE001 - logging must never be what crashes us
            pass


def _install_logging(service: BackendService) -> None:
    """Routes the standard library's logging into the tool's log."""
    root = logging.getLogger()
    # Replaced rather than added to, so calling this twice cannot double every
    # line in the console.
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(_ServiceLogHandler(service))
    root.setLevel(logging.DEBUG)


def _install_excepthook(service: BackendService) -> None:
    """Route unhandled exceptions into the log console as well as stderr.

    A crash that only writes to a console nobody is watching is a crash with no
    evidence; this at least leaves the traceback in the log the operator can save.
    """

    def hook(exc_type: type[BaseException], exc_value: BaseException, exc_tb: object) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))  # type: ignore[arg-type]
        print(text, file=sys.stderr)
        try:
            service.log.emit("error", f"unhandled {exc_type.__name__}: {exc_value}\n{text.rstrip()}")
        except Exception:  # logging must never be the thing that crashes us
            pass

    sys.excepthook = hook


def _install_high_dpi_policy() -> None:
    """Keeps fractional display scaling instead of rounding it away.

    Qt's default is to round a scale factor like 125% or 150% to the nearest whole
    number, which on a laptop at 125% turns a crisp interface into a slightly
    blurry one - and on the machines this tool runs on, 125% is the common case.
    PassThrough uses the scaling the display actually has.

    MUST be called before the QApplication is constructed: the policy is read
    when the application object is created, and setting it afterwards has no
    effect at all. That ordering is the whole reason this is a function called
    first rather than a line in the middle of `main()`.
    """
    policy = getattr(QGuiApplication, "setHighDpiScaleFactorRoundingPolicy", None)
    if policy is None:  # pragma: no cover - Qt 5 has no such setting
        return
    policy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)


def main(argv: list[str] | None = None) -> int:
    _install_high_dpi_policy()
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Huaxin Tool")
    app.setApplicationDisplayName("Huaxin Tool")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("HuaxinTool")
    app.setOrganizationDomain("huaxin.local")
    # Fusion renders identically on every platform, so the stylesheet is the
    # only thing that decides how the app looks - and a theme that only looks
    # right on one platform is a theme that has not been checked.
    app.setStyle("Fusion")
    # The theme comes from the settings file, so a choice made in the picker is
    # the one the tool starts with next time.
    starting_theme = load_settings()[0].theme
    try:
        ui_style.apply_theme(app, starting_theme)
    except KeyError:
        # A theme named in the settings file that this build does not have.
        # Falling back is right here: the alternative is refusing to start over
        # a colour scheme.
        ui_style.apply_theme(app)

    service = BackendService()
    _install_logging(service)
    _install_excepthook(service)
    # Exceptions raised by a Qt slot never reach excepthook: PyQt aborts the
    # process first. safe_slot catches them at the source and hands them here.
    set_error_reporter(lambda text: service.log.emit("error", text))

    window = MainWindow(service)
    # The settings the window read are the ones the rest of the UI consults -
    # the confirmation prompts above all. Published after the window is built, so
    # a failure to load them cannot leave the UI consulting nothing.
    set_active_settings(window.settings)
    window.show()

    # Order matters for a clean exit: stop the worker before Qt tears the
    # application down, so the native bridge is released on its own thread.
    app.aboutToQuit.connect(service.shutdown)
    app.aboutToQuit.connect(service.close_log)
    # Start the backend once the event loop is running, so every log line it
    # produces is delivered to a console that already exists.
    QTimer.singleShot(0, service.start)

    return app.exec()
