"""Huaxin Tool - Python front-end package.

Layout:
    huaxin.app       Bootstrap: QApplication, theme, service, main window
    huaxin.ui        PyQt6 widgets and windows (never touches the native backend)
    huaxin.core      Backend wrapper + the Qt-facing BackendService
    huaxin.workers   QThread job runner - the only place backend work executes

Threading rule: the UI thread submits Jobs to BackendService, which hands them to
a single worker thread. Nothing in huaxin.ui may call into the native bridge
directly; Backend enforces that with a thread-identity assertion.

The compiled backend extension is imported as the top-level module `huaxin_core`
(staged into this directory's parent by the CMake build), not from inside this
package.
"""

__version__ = "0.8.4"
