"""A custom title bar, for a window without the platform's frame.

WHY FRAMELESS AT ALL. The native caption on Windows is drawn in the system
theme. On a dark interface it is a light grey bar with a different font and a
different corner radius sitting on top of an otherwise carefully built window,
and no amount of styling inside the client area makes up for it. Every tool this
one is measured against - Miracle Box, UMT, the vendor suites - draws its own.

WHAT THAT COSTS, AND HOW IT IS PAID BACK. Losing the native frame means losing
everything the native frame did, and each of those has to be reimplemented
correctly or the window feels broken:

* dragging  - done here, with a threshold so a click on a button is not a drag
* double-click to maximise - done here
* snapping  - left to the platform via `startSystemMove`, which is what keeps
              Windows Snap, Aero Shake and multi-monitor dragging working
* resizing  - a grip on each edge and corner, drawn by the window itself
* the taskbar and Alt+Tab - kept by using the normal window flags and only
              clearing the frame, not by making the window a tool window
* keyboard access - the system menu (Alt+Space) is forwarded

The one thing that is genuinely lost is the OS's own window shadow and rounded
corners on Windows 11. A drop shadow is applied to the window instead, and the
window is left square rather than faking a radius, because a square window with a
shadow looks intentional and a rounded window with a visible square behind it
looks like a bug.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QSizeGrip,
    QVBoxLayout,
    QWidget,
)

from huaxin.ui import icons, style, tokens
from huaxin.ui.components import IconButton, StyledButton

__all__ = ["TitleBar", "FramelessWindow", "ResizeHandle"]

#: How far the pointer must move with the button held before it counts as a
#: drag rather than a click. Without it, a slightly shaky click on the title
#: text starts a drag and the window jumps - which is the single most common way
#: a custom title bar feels worse than the real one.
_DRAG_THRESHOLD = 4

#: How close to an edge counts as being on the resize border.
_RESIZE_MARGIN = 5

#: Which of the eight resize directions a point is in. Bit flags so a corner is
#: just two of them combined.
_LEFT, _RIGHT, _TOP, _BOTTOM = 1, 2, 4, 8


class TitleBar(QWidget):
    """The bar along the top of a frameless window.

    Emits nothing and decides nothing: it reports clicks on its own buttons and
    leaves the window to act on them, so the same bar can be used by the main
    window and by a floating tool panel without either inheriting from a window
    class it does not want.
    """

    minimise_requested = pyqtSignal()
    maximise_requested = pyqtSignal()
    close_requested = pyqtSignal()
    #: A drag started on the bar. The window handles it, because whether it is
    #: a move or a restore-then-move depends on the window state.
    drag_started = pyqtSignal(QPoint)

    def __init__(self, parent: QWidget, *, title: str = "", subtitle: str = "") -> None:
        super().__init__(parent)
        self.setObjectName("TitleBar")
        self.setFixedHeight(tokens.METRICS.titlebar_height)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 0, 0, 0)
        row.setSpacing(tokens.METRICS.gap_sm)

        self._mark = QLabel(self)
        self._mark.setPixmap(icons.pixmap("flash", tokens.active_theme().accent, 18))
        row.addWidget(self._mark, 0, Qt.AlignmentFlag.AlignVCenter)

        self._title = QLabel(title, self)
        self._title.setObjectName("WindowTitle")
        row.addWidget(self._title, 0, Qt.AlignmentFlag.AlignVCenter)

        self._subtitle = QLabel(subtitle, self)
        self._subtitle.setObjectName("WindowSubtitle")
        row.addWidget(self._subtitle, 0, Qt.AlignmentFlag.AlignVCenter)

        # The stretch is what makes the area between the title and the buttons
        # draggable - the buttons are at the far end and the gap belongs to the
        # window, not to a label.
        row.addStretch(1)

        self._minimise = self._window_button("minimise", "Minimise")
        self._maximise = self._window_button("maximise", "Maximise")
        self._close = self._window_button("close", "Close", close=True)

        self._minimise.clicked.connect(self.minimise_requested.emit)
        self._maximise.clicked.connect(self.maximise_requested.emit)
        self._close.clicked.connect(self.close_requested.emit)

        row.addWidget(self._minimise)
        row.addWidget(self._maximise)
        row.addWidget(self._close, 0)

        self._drag_origin: QPoint | None = None

    # -- api ---------------------------------------------------------------

    def setTitle(self, title: str) -> None:
        self._title.setText(title)

    def setSubtitle(self, subtitle: str) -> None:
        self._subtitle.setText(subtitle)

    def setMaximised(self, maximised: bool) -> None:
        """Swaps the middle button's glyph. Called by the window on any state change."""
        self._maximise.setIcon("restore" if maximised else "maximise")
        self._maximise.setToolTip("Restore" if maximised else "Maximise")

    def add_widget(self, widget: QWidget) -> QWidget:
        """Adds something to the bar before the window buttons.

        Used for the theme picker: a control that belongs to the window rather
        than to any panel, and the title bar is the only strip visible on every
        tab.
        """
        layout = self.layout()
        assert isinstance(layout, QHBoxLayout)
        layout.insertWidget(layout.count() - 3, widget, 0, Qt.AlignmentFlag.AlignVCenter)
        return widget

    # -- internals ---------------------------------------------------------

    def _window_button(self, icon: str, tooltip: str, *, close: bool = False) -> StyledButton:
        button = StyledButton("", self, role="ghost", icon=icon, tooltip=tooltip, lift=0)
        button.setObjectName("WindowCloseButton" if close else "WindowButton")
        button.setCursor(Qt.CursorShape.ArrowCursor)
        # The window buttons sit at the very edge, so they must not be draggable
        # and must not inherit the row's hover lift.
        button.setFixedHeight(tokens.METRICS.titlebar_height)
        return button

    def refresh_icons(self) -> None:
        """Re-colours the glyphs after a theme change."""
        self._mark.setPixmap(icons.pixmap("flash", tokens.active_theme().accent, 18))
        for button, name in (
            (self._minimise, "minimise"),
            (self._maximise, "restore" if self._maximise.toolTip() == "Restore" else "maximise"),
            (self._close, "close"),
        ):
            button.setIcon(name)

    # -- mouse -------------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() != Qt.MouseButton.LeftButton:
            return
        # The offset is kept so the window does not jump to put its corner under
        # the pointer, which is what every naive implementation does.
        self._drag_origin = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._drag_origin is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        window = self.window()
        global_position = event.globalPosition().toPoint()

        # The threshold is measured against where the press happened, so a
        # click that wobbles by a pixel stays a click.
        if not getattr(self, "_dragging", False):
            pressed_at = getattr(self, "_press_point", None)
            if pressed_at is None:
                self._press_point = global_position
                return
            if (global_position - pressed_at).manhattanLength() < _DRAG_THRESHOLD:
                return
            self._dragging = True

        if window.isMaximized():
            # Dragging a maximised window has to restore it first, and the
            # restored window should end up under the pointer rather than
            # snapping to wherever it was before it was maximised.
            fraction = global_position.x() / max(1, window.width())
            window.showNormal()
            self.setMaximised(False)
            self._drag_origin = QPoint(int(window.width() * fraction), window.height() // 2)

        window.move(global_position - self._drag_origin)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._drag_origin = None
        self._press_point = None
        self._dragging = False
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton:
            self.maximise_requested.emit()
        event.accept()


class ResizeHandle(QWidget):
    """An invisible grab area along one edge or corner of a frameless window.

    Sixteen pixels of hit area around a five-pixel visible border, because
    hitting a one-pixel edge with a mouse is a game nobody wants to play. The
    cursor changes on hover so the edge is discoverable without being drawn.
    """

    def __init__(self, parent: QWidget, direction: int) -> None:
        super().__init__(parent)
        self._direction = direction
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._cursor_for_direction()

    def _cursor_for_direction(self) -> None:
        horizontal = self._direction & (_LEFT | _RIGHT)
        vertical = self._direction & (_TOP | _BOTTOM)
        if horizontal and vertical:
            diagonal = (
                Qt.CursorShape.SizeFDiagCursor
                if (self._direction & _LEFT and self._direction & _TOP)
                or (self._direction & _RIGHT and self._direction & _BOTTOM)
                else Qt.CursorShape.SizeBDiagCursor
            )
            self.setCursor(diagonal)
        elif horizontal:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            self.setCursor(Qt.CursorShape.SizeVerCursor)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        window = self.window()
        if not (event.buttons() & Qt.MouseButton.LeftButton) or window.isMaximized():
            return
        geometry = window.geometry()
        global_position = event.globalPosition().toPoint()
        minimum = window.minimumSize()

        if self._direction & _LEFT:
            new_left = min(global_position.x(), geometry.right() - minimum.width())
            geometry.setLeft(new_left)
        if self._direction & _RIGHT:
            geometry.setRight(max(global_position.x(), geometry.left() + minimum.width()))
        if self._direction & _TOP:
            new_top = min(global_position.y(), geometry.bottom() - minimum.height())
            geometry.setTop(new_top)
        if self._direction & _BOTTOM:
            geometry.setBottom(max(global_position.y(), geometry.top() + minimum.height()))

        window.setGeometry(geometry)


class FramelessWindow(QMainWindow):
    """A main window with no platform frame, a TitleBar, and edge resizing.

    Subclassing is the honest structure here: the window genuinely is a
    QMainWindow with different chrome, and the panels that go inside it are
    unchanged by that. The alternative - a plain window with the frame flags
    cleared, and everyone remembering to call four setup methods - is how a
    window ends up with a title bar and no way to resize it.
    """

    def __init__(self, parent: QWidget | None = None, *, title: str = "", subtitle: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        # The window is still a normal window as far as the taskbar, Alt+Tab and
        # the window manager are concerned; only the frame is gone.
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setMinimumSize(tokens.METRICS.window_min_width, tokens.METRICS.window_min_height)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setMouseTracking(True)

        self._root = QWidget(self)
        self._root_layout = QVBoxLayout(self._root)
        self._root_layout.setContentsMargins(0, 0, 0, 0)
        self._root_layout.setSpacing(0)
        super().setCentralWidget(self._root)

        self.titlebar = TitleBar(self._root, title=title, subtitle=subtitle)
        self._root_layout.addWidget(self.titlebar)

        self._content = QWidget(self._root)
        self._root_layout.addWidget(self._content, 1)

        self.titlebar.minimise_requested.connect(self.showMinimized)
        self.titlebar.maximise_requested.connect(self.toggle_maximised)
        self.titlebar.close_requested.connect(self.close)

        self._handles: list[ResizeHandle] = []
        for direction in (_LEFT, _RIGHT, _TOP, _BOTTOM,
                          _LEFT | _TOP, _RIGHT | _TOP, _LEFT | _BOTTOM, _RIGHT | _BOTTOM):
            self._handles.append(ResizeHandle(self, direction))
        self._grip = QSizeGrip(self)

    # -- api ---------------------------------------------------------------

    def content(self) -> QWidget:
        """Where the window's own content goes.

        Not `centralWidget()`: that is the root widget holding the title bar, so
        returning it would let a caller put content behind the bar. Overriding
        the Qt method with different semantics is deliberate and is why the
        accessor is renamed rather than shadowing.
        """
        return self._content

    def set_content_layout(self, layout) -> None:
        holder = QVBoxLayout(self._content)
        holder.setContentsMargins(0, 0, 0, 0)
        holder.setSpacing(0)
        holder.addLayout(layout)

    def toggle_maximised(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self.titlebar.setMaximised(self.isMaximized())

    def refresh_chrome(self) -> None:
        """Re-colours the title bar after a theme change."""
        self.titlebar.refresh_icons()

    # -- geometry ----------------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)

        # The central widget is resized explicitly, which QMainWindow normally
        # does for us. It does it in response to a *platform* resize event, and
        # there are two situations where that never arrives: the offscreen
        # platform Qt's headless rendering uses, and any platform that hands the
        # window a new size without a layout pass. The symptom is quiet and bad -
        # the window is 1440 wide, the interface inside it is stuck at its
        # startup width, and everything after the old right edge of the title bar
        # (the theme picker, the window buttons) is simply not drawn. The
        # screenshots in docs/ were rendered that way for several phases.
        if self._root.size() != self.size():
            self._root.resize(self.size())

        margin = _RESIZE_MARGIN
        width, height = self.width(), self.height()
        positions = {
            _LEFT: (0, margin, margin, height - 2 * margin),
            _RIGHT: (width - margin, margin, margin, height - 2 * margin),
            _TOP: (margin, 0, width - 2 * margin, margin),
            _BOTTOM: (margin, height - margin, width - 2 * margin, margin),
            _LEFT | _TOP: (0, 0, margin, margin),
            _RIGHT | _TOP: (width - margin, 0, margin, margin),
            _LEFT | _BOTTOM: (0, height - margin, margin, margin),
            _RIGHT | _BOTTOM: (width - margin, height - margin, margin, margin),
        }
        for handle in self._handles:
            x, y, w, h = positions[handle._direction]
            handle.setGeometry(x, y, w, h)
        # Raised above the content, or the content swallows the press.
        for handle in self._handles:
            handle.raise_()

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self.titlebar.setMaximised(self.isMaximized())
