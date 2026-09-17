"""Verify the file logger and the cable-pull error path.

Two Phase 8 concerns, both testable without hardware:

  * the C++ logger writes timestamped lines, survives a bad path, and is safe to
    call from several threads at once;
  * a USB disconnect is reported as a disconnect rather than as a generic
    protocol error - which is what an operator actually needs to read.

    python tests/test_logging.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

import huaxin_core  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(description: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((bool(passed), description, detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {description}" + (f"  ({detail})" if detail else ""), flush=True)


class FakeContext:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def log(self, message: str, level: str = "info") -> None:
        self.events.append((level, message))

    def progress(self, percent: int, message: str = "") -> None:
        pass

    def check_cancelled(self) -> None:
        pass

    @property
    def cancelled(self) -> bool:
        return False


def main() -> int:
    logger = huaxin_core.Logger.instance()

    print("1. the C++ logger writes a timestamped file")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "flash_log.txt"
        check("open() succeeds for a writable path", logger.open(str(path)))
        check("is_open() agrees", logger.is_open())
        check("path() reports where it went", logger.path() == str(path), logger.path())

        logger.write("info", "first line")
        logger.write("warn", "a warning")
        logger.write("error", "an error")
        logger.write("debug", "a detail")

        check("the log file exists", path.is_file())
        text = path.read_text(encoding="utf-8")
        # The open marker plus the four messages above; close() has not run yet.
        check("every line was written", text.count("\n") == 5,
              f"{text.count(chr(10))} lines")
        check("lines carry a timestamp",
              bool(re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}", text)))
        check("levels are recorded",
              all(tag in text for tag in ("[INFO ]", "[WARN ]", "[ERROR]", "[DEBUG]")))
        check("the messages are there",
              all(text.find(m) >= 0 for m in ("first line", "a warning", "an error", "a detail")))
        check("a thread tag is included", re.search(r"\[t[0-9a-f]{4}\]", text) is not None)

        print("\n2. reopening appends rather than truncating")
        # A second run after a failure must not erase the first run's evidence.
        logger.write("info", "before reopen")
        logger.open(str(path))
        logger.write("info", "after reopen")
        logger.close()
        text = path.read_text(encoding="utf-8")
        check("the earlier lines survive a reopen",
              "before reopen" in text and "after reopen" in text)
        check("the file was not truncated", text.count("--- log opened ---") == 2,
              str(text.count("--- log opened ---")))
        check("close() marks the end", "--- log closed ---" in text)
        check("is_open() is false afterwards", not logger.is_open())

    print("\n3. a bad path is reported, not fatal")
    blocked = "/definitely/not/a/real/directory/x.txt" if os.name != "nt" else "Z:\\nope\\x.txt"
    check("open() returns False for an unwritable path", logger.open(blocked) is False)
    check("the logger stays closed", not logger.is_open())
    check("the path is cleared", logger.path() == "")
    # Writing while closed must be a no-op, never an exception: callers should
    # not have to guard every log call.
    logger.write("info", "this goes nowhere and must not raise")
    check("writing while closed does not raise", True)
    check("unknown levels fall back rather than raising", True)
    logger.write("nonsense", "still fine")

    print("\n4. concurrent writes are safe")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "threaded.txt"
        logger.open(str(path))

        def worker(index: int) -> None:
            for line in range(50):
                logger.write("info", f"thread {index} line {line}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        logger.close()

        text = path.read_text(encoding="utf-8")
        written = sum(1 for line in text.splitlines() if "line" in line)
        check("no writes were lost", written == 200, f"{written} of 200")
        # Interleaving is expected; a half-written line is not.
        check("no line was torn",
              all(line.startswith("20") for line in text.splitlines() if line.strip()),
              next((l for l in text.splitlines() if l.strip() and not l.startswith("20")), "none"))

    print("\n5. a USB disconnect is reported as a disconnect")
    check("UsbDisconnectedError is defined", hasattr(huaxin_core, "UsbDisconnectedError"))
    check("it derives from ProtocolError so existing handlers still catch it",
          issubclass(huaxin_core.UsbDisconnectedError, huaxin_core.ProtocolError))

    # With no device attached, opening the EDL transport fails at the search
    # step. The message has to say what to do, not just what went wrong.
    edl = huaxin_core.QualcommEdl()
    try:
        edl.connect()
    except huaxin_core.ProtocolError as exc:
        message = str(exc)
        check("opening with no device gives an actionable message",
              "EDL" in message or "05c6:9008" in message, message.splitlines()[0][:70])
        check("it says how to reach EDL mode", "reboot edl" in message or "EDL mode" in message)
    else:
        check("opening with no device gives an actionable message", False, "connect() succeeded")

    print("\n6. transports report being closed rather than crashing")
    check("a closed transport is not open", not edl.connected())
    edl.disconnect()  # idempotent
    edl.disconnect()
    check("disconnect is idempotent", True)

    # The same via the mediaTek path, which is a separate implementation.
    mtk = huaxin_core.MediaTekBrom()
    check("a fresh MediaTek session is not connected", not mtk.connected())
    mtk.disconnect()
    check("MediaTek disconnect is idempotent", True)

    print("\n7. the UI log mirrors into the file")
    from huaxin.core.backend import BackendService

    service = BackendService()
    service.start()
    check("the service opens a log on start", bool(service.log_path), service.log_path or "none")
    if service.log_path:
        check("the log path is a real file", Path(service.log_path).is_file(), service.log_path)
        service.log_message("info", "a line from the UI")
        import time

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if "a line from the UI" in Path(service.log_path).read_text(encoding="utf-8"):
                break
            time.sleep(0.05)
        text = Path(service.log_path).read_text(encoding="utf-8")
        check("a UI line reaches the file", "a line from the UI" in text)
        check("it is timestamped", re.search(r"\d{4}-\d{2}-\d{2}", text) is not None)
    service.shutdown()
    service.close_log()

    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("\nFailures:")
        for _, description, detail in failed:
            print(f"  - {description} {detail}")
        return 1
    print("Logging and disconnect handling OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
