"""PyQt6 user interface package.

Modules:
    theme        Dark stylesheet and colour tokens
    widgets      Reusable widgets (DeviceTable, LogConsole)
    panels       One vendor tab per supported target
    main_window  The application window that assembles the above
"""

from huaxin.ui.main_window import MainWindow
from huaxin.ui.panels import (
    AndroidPanel,
    MediaTekPanel,
    QualcommPanel,
    SamsungPanel,
    SpdPanel,
    VendorPanel,
)
from huaxin.ui.theme import COLORS, build_stylesheet
from huaxin.ui.widgets import DevicePanel, DeviceTable, LogConsole

__all__ = [
    "AndroidPanel",
    "COLORS",
    "DevicePanel",
    "DeviceTable",
    "LogConsole",
    "MainWindow",
    "MediaTekPanel",
    "QualcommPanel",
    "SamsungPanel",
    "SpdPanel",
    "VendorPanel",
    "build_stylesheet",
]
