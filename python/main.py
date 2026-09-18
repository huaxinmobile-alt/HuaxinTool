#!/usr/bin/env python3
"""Launcher for the Huaxin Tool GUI.

    python python/main.py

Adds this directory to sys.path so the `huaxin` package resolves without an
install step, then hands over to `huaxin.app.main()`, which owns the QApplication,
the theme, the logging and the exit code.

There is deliberately nothing else here. A version of this file grew its own
high-DPI setup and a fallback that built a MainWindow directly when
`huaxin.app.main` was missing. The high-DPI policy belongs in `huaxin.app` with
the rest of the application setup, where every entry point gets it; the fallback
would have raised TypeError, because MainWindow needs a BackendService. Both were
on a path nothing exercises, which is exactly where a broken path survives.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from huaxin.app import main  # noqa: E402 - must follow the sys.path setup

if __name__ == "__main__":
    raise SystemExit(main())
