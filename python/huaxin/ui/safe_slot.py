"""Stop a broken slot from taking the application down with it.

PyQt6 terminates the process when a Python exception escapes a slot. In a tool
that may be halfway through writing a partition table, that is not an acceptable
failure mode: a typo in a UI callback would kill a running flash. Every slot
that touches widgets is therefore wrapped with `safe_slot`, which turns the
crash into a log line and lets the worker finish what it was doing.

Usage:

    @safe_slot
    def _on_devices_changed(self, devices): ...

    button.clicked.connect(safe_slot(lambda: self._do_thing()))
"""

from __future__ import annotations

import functools
import inspect
import sys
import traceback
from typing import Any, Callable

__all__ = ["safe_slot", "set_error_reporter"]

_reporter: Callable[[str], None] | None = None


def set_error_reporter(reporter: Callable[[str], None] | None) -> None:
    """Point slot errors at something the operator can see (the log console)."""
    global _reporter
    _reporter = reporter


def _report(text: str) -> None:
    print(text, file=sys.stderr)
    if _reporter is None:
        return
    try:
        _reporter(text)
    except Exception:  # the reporter itself must never raise
        pass


def safe_slot(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap `fn` so an exception inside it is reported instead of fatal.

    Signal arguments are truncated to the ones `fn` actually declares. PyQt hands
    every argument to a slot whose wrapper accepts `*args` - `QAction.triggered`
    always sends a bool, for instance - and forwarding those to a slot that does
    not want them would raise TypeError inside the wrapper, which is precisely
    the situation this module exists to prevent.
    """
    try:
        parameters = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        # Builtins and C++-bound Qt methods (QWidget.close, for instance) expose
        # no signature. They take nothing from a signal, so pass nothing.
        parameters = []
    accepted = sum(
        1 for p in parameters if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    )
    takes_varargs = any(p.kind is p.VAR_POSITIONAL for p in parameters)

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            if takes_varargs:
                return fn(*args, **kwargs)
            return fn(*args[:accepted], **kwargs)
        except Exception:
            name = getattr(fn, "__qualname__", repr(fn))
            _report(f"unhandled exception in slot '{name}':\n{traceback.format_exc()}")
            return None

    return wrapper
