#!/usr/bin/env python3
"""Launcher for the Huaxin Tool GUI.

    python python/main.py

Adds this directory to sys.path so the `huaxin` package resolves without an
install step, then hands over to huaxin.app.main().
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from huaxin.app import main  # noqa: E402 - must follow the sys.path setup

if __name__ == "__main__":
    raise SystemExit(main())
