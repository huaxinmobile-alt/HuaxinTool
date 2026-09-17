"""Verify the adb/fastboot wrapper.

The interesting half of this module is not the parsing, it is the process
handling: output must arrive while the command runs rather than in one lump at
the end, a hung command must be killed, and a cancelled one must stop promptly.
All three are testable without a phone, by running a Python script as the "tool"
- `run_streaming` takes an argv, not a tool name.

    python tests/test_adb_fastboot.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from huaxin.core.adb_fastboot_wrapper import (  # noqa: E402
    AdbDevice,
    AndroidTools,
    CommandFailedError,
    CommandTimeoutError,
    ToolNotFoundError,
    feed_output,
    format_command,
    get_adb_devices,
    get_fastboot_devices,
    parse_adb_devices,
    parse_fastboot_devices,
    parse_getprop,
    run_streaming,
    validate_partition_name,
    validate_serial,
    verify_toolchain,
)
from huaxin.workers.worker import JobCancelled  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []
PYTHON = sys.executable


def check(description: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((bool(passed), description, detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {description}" + (f"  ({detail})" if detail else ""), flush=True)


class FakeContext:
    """Stands in for the worker's JobContext."""

    def __init__(self) -> None:
        self.events: list[tuple[float, str, str]] = []
        self._cancelled = False
        self._timer: threading.Timer | None = None

    def log(self, message: str, level: str = "info") -> None:
        self.events.append((time.monotonic(), level, message))

    def check_cancelled(self) -> None:
        if self._cancelled:
            raise JobCancelled("test job cancelled")

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel_after(self, delay: float) -> None:
        self._timer = threading.Timer(delay, self._cancel)
        self._timer.daemon = True
        self._timer.start()

    def _cancel(self) -> None:
        self._cancelled = True

    def lines(self, level: str = "output") -> list[str]:
        return [message for _, lvl, message in self.events if lvl == level]

    def stamps(self, level: str = "output") -> list[float]:
        return [stamp for stamp, lvl, _ in self.events if lvl == level]


def run(script: str, ctx: FakeContext, **kwargs) -> None:
    """Run a Python snippet as the fake tool."""
    run_streaming(ctx, [PYTHON, "-u", "-c", script], **kwargs)


def main() -> int:
    print("1. parsing adb device listings")
    listing = """
* daemon not running; starting now at tcp:5037
* daemon started successfully
List of devices attached
34ABC1234567	device product:redfin model:Pixel_5 device:redfin transport_id:1
0123456789ABCDEF	unauthorized
EMULATOR555	offline
10.0.0.5:5555	device
"""
    devices = parse_adb_devices(listing)
    check("daemon chatter is ignored", len(devices) == 4, f"{len(devices)} parsed")
    check("serial and state are read", devices[0].serial == "34ABC1234567" and devices[0].state == "device")
    check("-l extras are captured", devices[0].model == "Pixel_5" and devices[0].transport_id == "1")
    check("unauthorized is not ready", not devices[1].ready and devices[1].state == "unauthorized")
    check("offline is not ready", not devices[2].ready)
    check("network transports parse", devices[3].serial == "10.0.0.5:5555")
    check("a ready device is reported ready", devices[0].ready)
    check("empty input yields nothing", parse_adb_devices("List of devices attached\n") == [])

    print("\n2. parsing fastboot listings")
    fastboot = parse_fastboot_devices("34ABC1234567\tfastboot product:redfin usb:1-3\n\nHT7A1A01010\tfastbootd\n")
    check("both modes parse", [d.mode for d in fastboot] == ["fastboot", "fastbootd"], str(fastboot))
    check("product is captured", fastboot[0].product == "redfin")
    check("empty input yields nothing", parse_fastboot_devices("") == [])

    print("\n3. parsing getprop")
    props = parse_getprop("[ro.product.model]: [Pixel 5]\n[ro.build.version.release]: [14]\nnot a prop\n[]: []\n")
    check("properties parse", props.get("ro.product.model") == "Pixel 5", str(props))
    check("lines without the bracket form are ignored", "not a prop" not in props)
    check("a value containing brackets survives",
          parse_getprop("[a]: [x[y]z]").get("a") == "x[y]z")

    print("\n4. collapsing carriage-return progress")
    pending = bytearray()
    check("a plain line is emitted",
          feed_output(b"hello\n", pending) == ["hello"])
    check("CRLF does not leave a blank line",
          feed_output(b"hello\r\n", pending) == ["hello"])
    check("progress updates collapse to the final state",
          feed_output(b"\r10%\r40%\r70%\r100%\rdone\n", pending) == ["done"])
    check("a partial line is held back", feed_output(b"incomp", pending) == [])
    check("...and completed by the next chunk", feed_output(b"lete\n", pending) == ["incomplete"])
    check("bytes are decoded with replacement",
          feed_output(b"caf\xc3\xa9\n", pending) == ["café"])
    check("multi-byte characters split across chunks survive",
          feed_output(b"caf\xc3", pending) == [] and feed_output(b"\xa9\n", pending) == ["café"])
    oversized = feed_output(b"x" * (70 * 1024), pending)
    check("an over-long line is flushed rather than buffered", len(oversized) == 1 and len(oversized[0]) == 70 * 1024)
    check("the pending buffer is left clean", pending == b"")

    print("\n5. argument validation")
    check("a normal partition name passes", validate_partition_name("boot_a") == "boot_a")
    check("dots and dashes pass", validate_partition_name("vbmeta_system-1") == "vbmeta_system-1")
    for bad, why in (
        ("--slot", "option injection"),
        ("-w", "short option injection"),
        ("boot; rm -rf /", "shell metacharacter"),
        ("boot partition", "space"),
        ("", "empty"),
        ("x" * 100, "over-long"),
    ):
        try:
            validate_partition_name(bad)
        except ValueError:
            check(f"rejects {why}", True)
        else:
            check(f"rejects {why}", False, repr(bad))
    check("a normal serial passes", validate_serial("34ABC1234567") == "34ABC1234567")
    try:
        validate_serial("--whatever")
    except ValueError:
        check("rejects an option-shaped serial", True)
    else:
        check("rejects an option-shaped serial", False)

    print("\n6. real-time output")
    ctx = FakeContext()
    run("import time\nfor i in range(1,4):\n    print(f'line {i}', flush=True)\n    time.sleep(0.25)\n", ctx)
    stamps = ctx.stamps()
    check("every line arrived", ctx.lines() == ["line 1", "line 2", "line 3"], str(ctx.lines()))
    spread = (stamps[-1] - stamps[0]) if len(stamps) >= 2 else 0.0
    # If the output were buffered until exit, all three would land together.
    check("lines arrived as they were produced", spread >= 0.35, f"spread {spread:.2f}s")

    print("\n7. exit codes")
    ctx = FakeContext()
    result = run_streaming(ctx, [PYTHON, "-u", "-c", "print('all good', flush=True)"], timeout=20)
    check("a zero exit does not raise", result.returncode == 0)
    check("output is captured for the caller", result.lines == ("all good",), str(result.lines))
    check("the result carries the argv it ran", result.argv[0] == PYTHON)
    try:
        run("import sys\nprint('bad', flush=True)\nsys.exit(3)\n", FakeContext())
    except CommandFailedError as exc:
        check("a non-zero exit raises", exc.returncode == 3, str(exc.returncode))
        check("the tail of the output is kept", "bad" in " ".join(exc.tail), str(exc.tail))
    else:
        check("a non-zero exit raises", False)

    print("\n8. timeouts")
    ctx = FakeContext()
    started = time.monotonic()
    try:
        run("import time\nprint('working', flush=True)\ntime.sleep(30)\n", ctx, timeout=0.8)
    except CommandTimeoutError as exc:
        elapsed = time.monotonic() - started
        check("a hung command raises CommandTimeoutError", True)
        check("it stops at the timeout, not at the command's end", elapsed < 6.0, f"{elapsed:.2f}s")
        check("partial output is preserved", "working" in " ".join(exc.tail))
    else:
        check("a hung command raises CommandTimeoutError", False)

    print("\n9. cancellation")
    ctx = FakeContext()
    ctx.cancel_after(0.5)
    started = time.monotonic()
    try:
        run("import time\nprint('start', flush=True)\ntime.sleep(30)\n", ctx, timeout=60)
    except JobCancelled:
        elapsed = time.monotonic() - started
        check("cancellation surfaces as JobCancelled", True)
        check("the child is killed promptly", elapsed < 6.0, f"{elapsed:.2f}s")
    else:
        check("cancellation surfaces as JobCancelled", False)

    print("\n10. missing tools")
    try:
        run_streaming(FakeContext(), ["definitely-not-a-real-tool-9d3f", "--version"])
    except ToolNotFoundError as exc:
        check("a missing binary raises ToolNotFoundError", True)
        check("the message names the tool", "definitely-not-a-real-tool-9d3f" in str(exc))
    else:
        check("a missing binary raises ToolNotFoundError", False)

    tools = AndroidTools(adb="C:/nope/adb.exe", fastboot="C:/nope/fastboot.exe")
    try:
        get_adb_devices(FakeContext(), tools)
    except ToolNotFoundError as exc:
        check("a bad explicit path is reported, not run", True)
        check("the error explains how to fix it", "platform-tools" in str(exc))
    else:
        check("a bad explicit path is reported, not run", False)

    print("\n11. toolchain check reports rather than fails")
    ctx = FakeContext()
    result = verify_toolchain(ctx, tools)  # both paths bogus
    check("a missing toolchain does not raise", isinstance(result, AndroidTools))
    levels = {level: [m for _, lvl, m in ctx.events if lvl == level] for level in ("warn", "ok")}
    check("each tool is reported", any("adb" in m for m in levels["warn"]), str(levels["warn"]))
    check("the incompleteness is summarised", any("incomplete" in m for m in levels["warn"]))
    check("nothing was executed for a missing tool", ctx.lines() == [], str(ctx.lines()))

    print("\n12. command rendering")
    rendered = format_command(["adb", "-s", "ABC", "shell", "echo", "hello world"])
    check("arguments with spaces are quoted", "hello world" in rendered, rendered)

    print("\n13. discovery on this machine")
    discovered = AndroidTools.discover()
    print(f"     adb      : {discovered.adb_path}")
    print(f"     fastboot : {discovered.fastboot_path}")
    check("discovery always returns an object", isinstance(discovered, AndroidTools))
    check("missing tools are listed", isinstance(discovered.missing, list))
    check("the search record is kept for diagnostics", len(discovered.searched) > 0,
          f"{len(discovered.searched)} locations probed")
    if discovered.missing:
        print(f"     NOTE: {', '.join(discovered.missing)} not installed on this machine - "
              "the real commands cannot be exercised here")

    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("\nFailures:")
        for _, description, detail in failed:
            print(f"  - {description} {detail}")
        return 1
    print("adb/fastboot wrapper OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
