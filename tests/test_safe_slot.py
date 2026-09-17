"""Verify safe_slot catches slot exceptions and forwards the right arguments.

Two things are being proven here:

  1. An exception inside a slot connected to a real Qt signal does not kill the
     process (PyQt6 aborts without this wrapper).
  2. A zero-argument slot is not handed the bool that QPushButton.clicked() and
     QAction.triggered() always send - the wrapper truncates signal arguments to
     what the slot declares.

    python tests/test_safe_slot.py
"""

from __future__ import annotations

import os
import sys
from functools import partial
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from PyQt6.QtCore import QObject, pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import QApplication, QPushButton  # noqa: E402

from huaxin.ui.safe_slot import safe_slot, set_error_reporter  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []
REPORTED: list[str] = []


def check(description: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((bool(passed), description, detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {description}" + (f"  ({detail})" if detail else ""), flush=True)


def main() -> int:
    app = QApplication([])  # noqa: F841
    set_error_reporter(REPORTED.append)

    print("1. argument forwarding")

    @safe_slot
    def no_args() -> str:
        return "zero"

    check("zero-argument slot ignores signal arguments", no_args(False) == "zero")

    @safe_slot
    def one_arg(value: str) -> str:
        return f"got:{value}"

    check("one-argument slot receives the first argument", one_arg("x", "y") == "got:x")

    @safe_slot
    def varargs(*values: str) -> str:
        return f"n={len(values)}"

    check("varargs slot receives everything", varargs("a", "b", "c") == "n=3")

    print("\n2. exception containment")

    @safe_slot
    def explodes() -> None:
        raise ValueError("boom")

    result = explodes(False)
    check("slot exception does not propagate", result is None)
    check("slot exception is reported", len(REPORTED) == 1 and "ValueError" in REPORTED[0])
    check("report names the slot", REPORTED and "explodes" in REPORTED[0])

    print("\n3. partial() binding (what the vendor panels use)")

    class Holder(QObject):
        def __init__(self) -> None:
            super().__init__()
            self.seen: list[str] = []

        def act(self, label: str) -> None:
            self.seen.append(label)

    holder = Holder()
    safe_slot(partial(holder.act, "flash"))(False)
    check("bound partial receives its bound argument only", holder.seen == ["flash"], str(holder.seen))

    print("\n4. through a real Qt signal")

    class Emitter(QObject):
        fired = pyqtSignal(bool)

    emitter = Emitter()
    calls: list[str] = []
    emitter.fired.connect(safe_slot(lambda: calls.append("clicked")))
    emitter.fired.emit(True)
    check("clicked-style signal drives a zero-arg slot", calls == ["clicked"], str(calls))

    button = QPushButton("Explode")
    button.clicked.connect(safe_slot(explodes))
    before = len(REPORTED)
    button.click()  # would abort the process without safe_slot
    check("exploding button slot keeps the process alive", True)
    check("exploding button slot was reported", len(REPORTED) == before + 1)
    check("application object still alive", app is not None)

    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        for _, description, detail in failed:
            print(f"  - {description} {detail}")
        return 1
    print("safe_slot OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
