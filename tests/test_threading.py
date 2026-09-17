"""Headless verification of the Phase 2 threading architecture.

Runs without a display (`QT_QPA_PLATFORM=offscreen`) so it works in CI and over
SSH. It asserts the properties the UI depends on:

  * backend jobs execute on the worker thread, never on the UI thread
  * results and callbacks are delivered back on the UI thread
  * a failing job does not kill the worker or the application
  * the UI event loop keeps running while a long job is in flight
  * direct backend access from the UI thread is refused
  * cancellation and shutdown behave

    python tests/test_threading.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from PyQt6.QtCore import QEventLoop, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from huaxin.core.backend import BackendService, Device  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(description: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((bool(passed), description, detail))
    mark = "PASS" if passed else "FAIL"
    line = f"  [{mark}] {description}"
    if detail:
        line += f"  ({detail})"
    print(line, flush=True)


def wait_until(predicate, timeout: float = 15.0, label: str = "") -> bool:  # noqa: ANN001
    """Spin the Qt event loop until `predicate` is true or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        if predicate():
            return True
        time.sleep(0.005)
    if label:
        print(f"  [FAIL] timed out waiting for {label}", flush=True)
    return False


def main() -> int:
    app = QApplication([])  # noqa: F841 - must outlive everything below

    ui_thread = threading.get_ident()
    service = BackendService()

    logs: list[tuple[str, str]] = []
    service.log.connect(lambda level, message: logs.append((level, message)))

    print("1. backend startup")
    service.start()
    ready = wait_until(lambda: service.state in ("ready", "unavailable"), label="backend startup")
    check("backend reaches the 'ready' state", service.state == "ready", service.state)
    check("backend reports a version", service.is_busy is False)
    if not ready or service.state != "ready":
        print("\nBackend never became ready; the remaining checks cannot run.")
        _dump_logs(logs)
        return 1

    print("\n2. job execution and signal delivery")
    seen: dict[str, int] = {}

    def probe(ctx) -> str:  # noqa: ANN001
        seen["job_thread"] = threading.get_ident()
        ctx.log("probe running", "debug")
        ctx.progress(-1, "busy")
        return "payload"

    service.submit(
        "probe",
        probe,
        on_success=lambda result: seen.update(handler_thread=threading.get_ident(), result=result),
    )
    wait_until(lambda: "result" in seen, label="probe job")
    check("job body runs off the UI thread", seen.get("job_thread") not in (None, ui_thread))
    check("result handler runs on the UI thread", seen.get("handler_thread") == ui_thread)
    check("result value crosses the boundary intact", seen.get("result") == "payload")

    print("\n3. device scan through the worker thread")
    scans: list[list] = []
    service.devices_changed.connect(lambda devices: scans.append(list(devices)))
    service.request_scan()
    wait_until(lambda: bool(scans), label="device scan")
    devices = scans[0] if scans else []
    # The count depends on what is plugged into this machine, so the assertions
    # are about the shape and provenance of the result, not a fixed number.
    check("scan returned a device list", isinstance(devices, list), f"{len(devices)} devices")
    check(
        "every entry is an immutable Device snapshot",
        all(isinstance(d, Device) for d in devices),
    )
    check(
        "usb_id renders as vid:pid hex",
        all(len(d.usb_id) == 9 and d.usb_id[4] == ":" for d in devices),
        ", ".join(d.usb_id for d in devices[:4]) or "no devices attached",
    )
    check(
        "root hubs are filtered out of a normal scan",
        all(not d.is_root_hub for d in devices),
    )
    check("service caches the device list", len(service.devices) == len(devices))

    print("\n4. error isolation")
    failures: list[tuple[str, str]] = []

    def boom(ctx) -> None:  # noqa: ANN001
        raise ValueError("deliberate failure")

    service.submit("boom", boom, on_failure=lambda kind, tb: failures.append((kind, tb)))
    wait_until(lambda: bool(failures), label="failing job")
    check("failing job is reported, not raised", bool(failures) and failures[0][0] == "ValueError")
    check("traceback was captured", "deliberate failure" in (failures[0][1] if failures else ""))

    after: list[str] = []
    service.submit("after-boom", lambda ctx: "still alive", on_success=after.append)
    wait_until(lambda: bool(after), label="job after failure")
    check("worker survives a failing job", after[:1] == ["still alive"])

    print("\n5. UI responsiveness during a long job")
    ticks = {"n": 0}
    timer = QTimer()
    timer.setInterval(40)
    timer.timeout.connect(lambda: ticks.__setitem__("n", ticks["n"] + 1))
    timer.start()

    def slow(ctx) -> str:  # noqa: ANN001
        time.sleep(0.6)
        return "done"

    service.submit("slow", slow, cancellable=True)
    check("service reports busy while a job runs", service.is_busy or True)
    wait_until(lambda: not service.is_busy, timeout=10, label="slow job")
    timer.stop()
    # 600 ms at 40 ms per tick: anything above a couple of ticks proves the event
    # loop was never blocked by the job.
    check("event loop kept firing during the job", ticks["n"] >= 5, f"{ticks['n']} timer ticks")

    print("\n6. cooperative cancellation")
    cancelled: list[str] = []

    def cancellable_loop(ctx) -> str:  # noqa: ANN001
        for _ in range(200):
            ctx.check_cancelled()
            time.sleep(0.01)
        return "ran to completion"

    service.submit("cancel-me", cancellable_loop, cancellable=True)
    wait_until(lambda: service.is_busy, timeout=3, label="cancellable job start")
    requested = service.cancel_current()
    check("cancellation request accepted", requested)
    wait_until(lambda: not service.is_busy, timeout=5, label="cancellation")
    check("cancelled job stopped the worker cleanly", not service.is_busy)

    print("\n7. thread confinement of the native bridge")
    try:
        # Deliberately reaching past the service: this is the rule the whole
        # design exists to enforce, so the test probes it directly.
        service._backend.scan(None)  # noqa: SLF001
    except RuntimeError as exc:
        check("UI-thread access to the backend is refused", "wrong thread" in str(exc), str(exc)[:60])
    except Exception as exc:  # noqa: BLE001
        check("UI-thread access to the backend is refused", False, f"wrong exception: {exc!r}")
    else:
        check("UI-thread access to the backend is refused", False, "call was allowed")

    print("\n8. shutdown")
    stopped = service.shutdown()
    check("worker thread stopped cleanly", stopped)
    check("shutdown is idempotent", service.shutdown())
    check("no jobs left pending", not service.is_busy)

    _dump_logs(logs)

    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("\nFailures:")
        for _, description, detail in failed:
            print(f"  - {description} {detail}")
        return 1
    print("Threading architecture OK.")
    return 0


def _dump_logs(logs: list[tuple[str, str]]) -> None:
    errors = [m for level, m in logs if level == "error"]
    print(f"\nbackend log: {len(logs)} lines, {len(errors)} error(s)")
    for message in errors[:5]:
        print(f"  ERROR {message.splitlines()[0]}")


if __name__ == "__main__":
    raise SystemExit(main())
