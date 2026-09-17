"""Render the UI offscreen and save PNGs for review.

    python python/tools/screenshot_ui.py [output_dir]
    python python/tools/screenshot_ui.py --gallery
    python python/tools/screenshot_ui.py --themes
    python python/tools/screenshot_ui.py --sizes
    python python/tools/screenshot_ui.py --all

Uses Qt's offscreen platform plugin, so it needs no display. The default mode is
one image per vendor tab plus one of the whole window; the flags add the
component gallery, one shot per theme, and the layout at several window widths.

WHY THIS EXISTS AT ALL. A Qt stylesheet fails quietly: a rule with a selector
that never matches, a sub-control Qt does not have, a token substituted into a
place Qt will not accept it - all of them are dropped without a warning, and the
source looks correct. Rendering the result is the only way to see which of them
happened, which is why every visual change in this project goes through here.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Screenshots are taken after the layout settles; an animation caught mid-flight
# is a picture of a half-drawn window.
os.environ.setdefault("HUAXIN_REDUCE_MOTION", "1")

PYTHON_ROOT = Path(__file__).resolve().parents[1]
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from PyQt6.QtCore import QEventLoop, QSize  # noqa: E402
from PyQt6.QtWidgets import QApplication, QTabWidget  # noqa: E402

from huaxin.core import progress as progress_module  # noqa: E402
from huaxin.core.backend import BackendService  # noqa: E402
from huaxin.ui import style, tokens  # noqa: E402
from huaxin.ui.main_window import MainWindow  # noqa: E402
from huaxin.ui.widgets import DeviceTable  # noqa: E402

TABS = ("android", "qualcomm", "mediatek", "spd", "samsung")

#: The sizes the main window is rendered at when checking that the layout holds
#: together. The smallest is the enforced minimum and the largest is a 1080p
#: display with the window maximised; if anything overflows, it overflows at one
#: of these two.
SIZES = ((1024, 640), (1440, 900), (1920, 1080))


def forget_saved_layout() -> None:
    """Drops any window geometry this tool saved on an earlier run.

    MainWindow remembers its size and restores it on construction, which is right
    for the application and wrong here: every render would inherit the *previous*
    render's window size, so the file named `size-1920x1080` could hold a 751x647
    window - which is exactly what happened, and why the sizes looked arbitrary.
    Clearing it before each window makes the requested size the only thing that
    decides what is rendered.
    """
    from PyQt6.QtCore import QSettings

    settings = QSettings()
    settings.remove("window/geometry")
    settings.remove("window/state")
    settings.sync()


def capture(window, size: tuple[int, int] | None = None) -> "QImage":  # noqa: ANN001, F821
    """Renders a window to an image, chrome and all, at `size` (default: as it is).

    NOT `window.grab()`. That asks the platform for the window's own surface,
    and on the offscreen platform - which is the whole point of this tool, it has
    to run over SSH - the result does not include most of the custom chrome: the
    title bar's gradient paints, and the theme picker, the window buttons and the
    subtitle in it do not. The images therefore showed a title bar with nothing
    in it, for several phases, and looked plausible enough that nobody noticed.

    Grabbing the central widget renders the widget tree directly, which is what
    the user sees on a real screen. `size` is passed explicitly because the
    offscreen platform is an 800x800 screen and silently clamps a window shown on
    it, so "the size the window ended up at" is not always the size that was
    asked for - and the file named size-1920x1080 has to hold 1920x1080.
    """
    target = window.centralWidget() or window
    wanted = QSize(*size) if size is not None else window.size()
    if target.size() != wanted:
        target.resize(wanted)
    return target.grab().toImage()


def pump(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        time.sleep(0.01)


def pump_until(predicate, timeout: float = 15.0) -> bool:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _new_app() -> QApplication:
    app = QApplication([])
    # Isolated settings scope so a saved layout from a real run cannot skew the
    # screenshots (and so this tool never touches the user's own QSettings).
    app.setOrganizationName("HuaxinToolScreenshots")
    app.setApplicationName("ScreenshotTool")
    app.setStyle("Fusion")
    style.apply_theme(app)
    return app


def _render_main(app: QApplication, out_dir: Path, *, size: tuple[int, int] = (1366, 860),
                 theme: str | None = None, prefix: str = "ui") -> list[Path]:
    """One image of the whole window per vendor tab."""
    # Always applied, even when no theme is named:  renders the gallery in
    # all three themes first, so without this the size and feature renders come
    # out in whichever one happened to be last - files named size-1440x900 with
    # Daylight in them and nothing saying so.
    style.apply_theme(app, theme or tokens.DEFAULT_THEME)
    forget_saved_layout()
    service = BackendService()
    window = MainWindow(service)
    window.resize(*size)
    window.show()
    pump(0.3)
    actual = window.size()
    print(f"window shown at {actual.width()}x{actual.height()}"
          + ("" if (actual.width(), actual.height()) == size else f" (asked for {size[0]}x{size[1]})"),
          flush=True)

    service.start()
    if not pump_until(lambda: service.state in ("ready", "unavailable")):
        print("backend never became ready", file=sys.stderr)
        return []

    service.request_scan()
    if not pump_until(lambda: bool(service.devices), timeout=8.0):
        print("device scan produced nothing; rendering without a selection", flush=True)
    else:
        print(f"scan returned {len(service.devices)} devices", flush=True)

    table = window.findChild(DeviceTable)
    if table is not None and table.rowCount():
        table.selectRow(0)  # so the panels show a selected-device banner
    pump(0.5)

    tabs = window.findChild(QTabWidget)
    written: list[Path] = []
    count = tabs.count() if tabs is not None else 0
    for index in range(count):
        tabs.setCurrentIndex(index)
        pump(0.3)
        label = TABS[index] if index < len(TABS) else f"tab{index}"
        target = out_dir / f"{prefix}-{index + 1}-{label}.png"
        if not capture(window, size=size).save(str(target)):
            print(f"failed to write {target}", file=sys.stderr)
            continue
        written.append(target)

    service.shutdown()
    window.close()
    pump(0.2)
    return written


def _render_features(app: QApplication, out_dir: Path, *, theme: str | None = None) -> list[Path]:
    """The window with the live features in states a still screen can show.

    A screenshot of an idle window proves nothing about the parts of this
    interface that only exist while something is happening: the operation readout
    in the status bar, the toast stack in the corner, and the arrival tint on a
    device row. This drives them into those states and captures the result, which
    is the only way to check they are drawn where they are supposed to be.
    """
    style.apply_theme(app, theme or tokens.DEFAULT_THEME)
    forget_saved_layout()
    service = BackendService()
    window = MainWindow(service)
    window.resize(1366, 860)
    window.show()
    pump(0.3)

    service.start()
    if not pump_until(lambda: service.state in ("ready", "unavailable")):
        print("backend never became ready", file=sys.stderr)
        return []

    # A job in flight, with figures that look like a real transfer.
    window._on_job_started("edl.flash")
    window._on_job_progress_detail(
        "edl.flash",
        progress_module.JobProgress(
            operation_type="flashing", current_partition="super",
            current_partition_index=2, total_partitions=5,
            bytes_written=1_500_000_000, total_bytes=4_000_000_000,
            percentage=43.0, speed_mbps=21.75, eta_seconds=88.0,
            status_message="writing super.img",
        ),
    )
    # The four toast tones, which is also the only way to see the tone edges
    # together against the real background.
    window.toast("ok", "Qualcomm device detected", "05c6:9008 in EDL (Emergency Download)")
    window.toast("warn", "Cable may be marginal", "The link renegotiated twice during the last "
                                                  "transfer.")
    window.toast("error", "Partition verification failed", "vendor — retrying once.")
    pump(0.6)

    tabs = window.findChild(QTabWidget)
    if tabs is not None:
        tabs.setCurrentIndex(1)  # the Qualcomm tab: the one a running flash is on
        pump(0.3)

    target = out_dir / f"features-{theme or tokens.active_theme().name}.png"
    capture(window).save(str(target))
    print(f"features rendered ({theme or tokens.active_theme().name}, "
          f"{len(window._toasts.live)} toasts)", flush=True)

    service.shutdown()
    window.close()
    pump(0.2)
    return [target]


def _render_gallery(app: QApplication, out_dir: Path, *, theme: str | None = None,
                    height: int = 940) -> list[Path]:
    """The component gallery: every component on one page.

    The window is grown to whatever the content needs before the shot is taken.
    The gallery is a scrolling page, and grabbing the viewport at a fixed height
    silently crops everything below the fold - which is how three new cards were
    added without appearing in the screenshot that is meant to show them.
    """
    from huaxin.ui.gallery import Gallery

    if theme:
        style.apply_theme(app, theme)
    gallery = Gallery()
    gallery.resize(1180, height)
    gallery.show()
    pump(0.6)

    content = gallery._page.parentWidget()
    needed = content.sizeHint().height() if content is not None else height
    if needed > gallery.height():
        gallery.resize(gallery.width(), min(needed + 24, 4000))
        pump(0.4)

    name = f"gallery-{theme or tokens.active_theme().name}"
    target = out_dir / f"{name}.png"
    capture(gallery).save(str(target))
    print(f"gallery rendered ({theme or tokens.active_theme().name}, "
          f"{gallery.width()}x{gallery.height()})", flush=True)
    gallery.close()
    pump(0.2)
    return [target]


def _render_sizes(app: QApplication, out_dir: Path) -> list[Path]:
    written: list[Path] = []
    for width, height in SIZES:
        written += _render_main(app, out_dir, size=(width, height),
                                prefix=f"size-{width}x{height}")
    return written


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Render the UI to PNG files.")
    parser.add_argument("output_dir", nargs="?", help="where to write the images")
    parser.add_argument("--gallery", action="store_true", help="the component gallery")
    parser.add_argument("--themes", action="store_true", help="the gallery in every theme")
    parser.add_argument("--sizes", action="store_true", help="the window at several widths")
    parser.add_argument("--features", action="store_true",
                        help="the window with a job running and notifications showing")
    parser.add_argument("--all", action="store_true", help="every mode")
    args = parser.parse_args(argv)

    out_dir = (Path(args.output_dir) if args.output_dir
               else Path(__file__).resolve().parents[2] / "docs" / "screenshots")
    out_dir.mkdir(parents=True, exist_ok=True)

    app = _new_app()
    written: list[Path] = []

    if args.all:
        written += _render_main(app, out_dir)
    if args.all or args.gallery:
        written += _render_gallery(app, out_dir)
    if args.all or args.themes:
        for name in tokens.theme_names():
            written += _render_gallery(app, out_dir, theme=name, height=640)
    if args.all or args.sizes:
        written += _render_sizes(app, out_dir)
    if args.all or args.features:
        written += _render_features(app, out_dir)
    if not (args.gallery or args.themes or args.sizes or args.features or args.all):
        written += _render_main(app, out_dir)
        written += _render_gallery(app, out_dir, height=640)

    if not written:
        print("nothing was rendered", file=sys.stderr)
        return 1

    for path in written:
        print(f"{path}  ({path.stat().st_size // 1024} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
