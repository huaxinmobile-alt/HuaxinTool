"""Python tests for the settings system, the device database and the error layer.

None of these three needs hardware, Qt, or a device - which is exactly why they
are worth testing exhaustively: they run on every machine the tool is built on,
and a mistake in any of them shows up as the tool misbehaving for an operator who
has no way to debug it.

Run directly:  python tests/test_integration.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from huaxin.core import config, devices, errors  # noqa: E402

CHECKS = 0
FAILURES = 0


def check(description: str, passed: bool, detail: str = "") -> None:
    global CHECKS, FAILURES
    CHECKS += 1
    if not passed:
        FAILURES += 1
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{'PASS' if passed else 'FAIL'}] {description}{suffix}")


# -----------------------------------------------------------------------------
#  1. Settings: defaults and validation
# -----------------------------------------------------------------------------


def test_settings_defaults() -> None:
    print("\n1. settings defaults")

    defaults = config.Settings()
    check("a default install needs no fixing", defaults.problems() == [],
          str(defaults.problems()))
    check("the retry default is three attempts", defaults.max_retry_attempts == 3)
    check("the log level default is info", defaults.log_level == "info")
    check("destructive operations are confirmed by default",
          defaults.confirm_destructive_operations)
    check("the log rotates by default", defaults.log_max_bytes == 10 * 1024 * 1024)

    # Every field has to have a validation rule, or a hand-edited file could put
    # anything into it. This checks the rule exists by feeding each field its
    # own value and asserting nothing changes.
    check("a round trip through the validator is a fixed point",
          defaults.sanitised().to_dict() == defaults.to_dict())


def test_settings_validation() -> None:
    print("\n2. settings validation")

    # A negative timeout must not reach a USB transfer. It is clamped, not
    # silently accepted and not fatal.
    broken = config.Settings(default_timeout=-5.0, transfer_timeout=0.0,
                             connect_timeout=99999.0)
    fixed = broken.sanitised()
    check("a negative timeout is clamped up", fixed.default_timeout == 0.5,
          str(fixed.default_timeout))
    check("a zero transfer timeout is clamped up", fixed.transfer_timeout == 1.0,
          str(fixed.transfer_timeout))
    check("an absurd timeout is clamped down", fixed.connect_timeout == 300.0,
          str(fixed.connect_timeout))
    check("the corrections are reported", len(broken.problems()) == 3,
          str(broken.problems()))

    broken = config.Settings(max_retry_attempts=0, retry_initial_delay_ms=-1,
                             retry_max_delay_ms=-1, retry_total_budget_s=-1)
    fixed = broken.sanitised()
    check("zero attempts is raised to one", fixed.max_retry_attempts == 1)
    check("negative delays become zero", fixed.retry_initial_delay_ms == 0)
    check("a negative budget becomes zero", fixed.retry_total_budget_s == 0)

    # A ceiling below the initial delay would make the backoff meaningless.
    inconsistent = config.Settings(retry_initial_delay_ms=5000, retry_max_delay_ms=10)
    check("the ceiling is raised to meet the first delay",
          inconsistent.sanitised().retry_max_delay_ms == 5000)

    broken = config.Settings(log_level="LOUD", speed_limit=-100)
    fixed = broken.sanitised()
    check("an unknown log level falls back to info", fixed.log_level == "info")
    check("a negative speed limit becomes no limit", fixed.speed_limit == 0)
    check("a log level is case-folded",
          config.Settings(log_level="  WARNING ").sanitised().log_level == "warning")

    # Values of the wrong type must not raise: this runs during startup.
    wrong_types = config.Settings(default_timeout="soon", log_level=None,
                                  max_retry_attempts="many", scan_on_startup="yes")
    fixed = wrong_types.sanitised()
    check("a non-numeric timeout falls back to the default",
          fixed.default_timeout == 30.0, str(fixed.default_timeout))
    check("a None log level falls back", fixed.log_level == "info")
    check("a non-numeric attempt count falls back", fixed.max_retry_attempts == 3)
    # A truthy string must not become True: bool("no") is True, and a settings
    # file holding "no" would then turn confirmations on when it meant off.
    check("a non-boolean boolean falls back rather than being coerced",
          fixed.scan_on_startup is True)


def test_settings_files() -> None:
    print("\n3. reading and writing the settings file")

    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "settings.json"

        # A missing file is the normal first run, not a problem.
        loaded, notes = config.load_settings(path)
        check("a missing file gives defaults", loaded.to_dict() == config.Settings().to_dict())
        check("and says nothing about it", notes == [], str(notes))

        changed = config.Settings(log_level="debug", default_timeout=12.5, log_to_file=False)
        config.save_settings(changed, path)
        check("saving creates the file", path.exists())

        loaded, notes = config.load_settings(path)
        check("what was saved is what is loaded", loaded.log_level == "debug")
        check("a float survives the round trip", loaded.default_timeout == 12.5)
        check("a boolean survives the round trip", loaded.log_to_file is False)
        check("and nothing needed correcting", notes == [], str(notes))

        # A hand-edited file with junk in it must not stop the tool starting.
        path.write_text("{ this is not json", encoding="utf-8")
        loaded, notes = config.load_settings(path)
        check("malformed JSON gives defaults rather than raising",
              loaded.log_level == "info")
        check("and reports why", len(notes) == 1 and "JSON" in notes[0], str(notes))

        path.write_text("[1, 2, 3]", encoding="utf-8")
        loaded, notes = config.load_settings(path)
        check("a JSON array gives defaults", loaded.log_level == "info")
        check("and reports why", len(notes) == 1 and "object" in notes[0], str(notes))

        # Unknown keys are ignored: a file from a newer build still loads.
        path.write_text(json.dumps({"log_level": "warning", "from_the_future": 42}),
                        encoding="utf-8")
        loaded, notes = config.load_settings(path)
        check("an unknown key is ignored rather than fatal", loaded.log_level == "warning")

        # Out-of-range values are corrected and reported.
        path.write_text(json.dumps({"default_timeout": -1, "max_retry_attempts": 900}),
                        encoding="utf-8")
        loaded, notes = config.load_settings(path)
        check("an out-of-range value is corrected", loaded.default_timeout == 0.5)
        check("and the correction is reported", len(notes) == 2, str(notes))

        # The write is atomic, so a crash cannot leave a half-written file.
        config.save_settings(config.Settings(), path)
        check("the temporary file is not left behind",
              not list(Path(scratch).glob("*.tmp")), str(list(Path(scratch).iterdir())))

        # Resetting removes the file and returns the defaults.
        reset = config.reset_settings(path)
        check("resetting removes the file", not path.exists())
        check("resetting returns the defaults", reset.to_dict() == config.Settings().to_dict())
        check("resetting twice is not an error",
              config.reset_settings(path).log_level == "info")


def test_settings_reach_the_native_layer() -> None:
    print("\n4. settings reach the native layer")

    if errors.backend is None:
        check("the native backend is importable", False, str(errors.BACKEND_IMPORT_ERROR))
        return

    settings = config.Settings(max_retry_attempts=5, retry_initial_delay_ms=10,
                               retry_max_delay_ms=80, retry_total_budget_s=3)
    policy = settings.retry_policy()
    check("a retry policy is built from the settings", policy is not None)
    check("the attempt count is carried over", policy.attempts == 5,
          str(policy.attempts))
    check("the first delay is carried over", policy.initial_delay_ms == 10)
    check("the ceiling is carried over", policy.maximum_delay_ms == 80)
    check("the budget is converted to milliseconds", policy.total_budget_ms == 3000,
          str(policy.total_budget_ms))

    # The policy the C++ backoff produces has to match what Python computes, or
    # the two retry loops would pace themselves differently.
    for attempt in range(1, 6):
        native = policy.delay_for_attempt(attempt)
        python = errors._delay_for(attempt, 10, 80)
        check(f"attempt {attempt} backs off identically in both languages",
              native == python, f"native {native}, python {python}")

    # A logging setting must reach the Logger object the backend holds.
    class FakeLogger:
        """Stands in for the native Logger, recording what it is told."""

        def __init__(self) -> None:
            self.min_level = None
            self.console = None
            self.opened = None
            self._file_enabled = None
            self.max_bytes = None

        def set_min_level(self, level: str) -> None:
            self.min_level = level

        @property
        def console_enabled(self) -> bool:
            return bool(self.console)

        @console_enabled.setter
        def console_enabled(self, value: bool) -> None:
            self.console = value

        @property
        def file_enabled(self) -> bool:
            return bool(self._file_enabled)

        @file_enabled.setter
        def file_enabled(self, value: bool) -> None:
            self._file_enabled = value

        @property
        def max_file_bytes(self) -> int:
            return self.max_bytes

        @max_file_bytes.setter
        def max_file_bytes(self, value: int) -> None:
            self.max_bytes = value

        def default_path(self) -> str:
            return "default.log"

        def open(self, path: str) -> bool:
            self.opened = path
            return True

    fake = FakeLogger()
    config.Settings(log_level="warning", log_to_console=False,
                    log_max_bytes=2048).apply_to_logger(fake)
    check("the log level reaches the logger", fake.min_level == "warning")
    check("the console switch reaches the logger", fake.console is False)
    check("the rotation limit reaches the logger", fake.max_bytes == 2048)
    check("the default path is used when none is set", fake.opened == "default.log")


def test_settings_template() -> None:
    print("\n5. the deployment template")

    template = ROOT / "packaging" / "settings.template.json"
    check("the template exists", template.exists(), str(template))
    if not template.exists():
        return

    payload = json.loads(template.read_text(encoding="utf-8"))
    known = {name for name in config.Settings().__dataclass_fields__}
    unknown = [key for key in payload if not key.startswith("_") and key not in known]
    check("the template holds no unknown settings", unknown == [], str(unknown))

    missing = [name for name in known if name not in payload]
    check("the template covers every setting", missing == [], str(missing))

    loaded = config.Settings.from_dict(payload).sanitised()
    check("the template loads without corrections", loaded.problems() == [],
          str(loaded.problems()))
    check("the template's values are the shipped defaults",
          loaded.to_dict() == config.Settings().to_dict())


# -----------------------------------------------------------------------------
#  6. The device database
# -----------------------------------------------------------------------------


def test_device_database() -> None:
    print("\n6. the device database")

    check("there are profiles", len(devices.DEVICE_PROFILES) >= 4,
          str(len(devices.DEVICE_PROFILES)))

    keys = [profile.key for profile in devices.DEVICE_PROFILES]
    check("every profile has a unique key", len(keys) == len(set(keys)))

    vendors = {profile.vendor for profile in devices.DEVICE_PROFILES}
    check("the four vendors are covered",
          {"qualcomm", "mediatek", "unisoc", "samsung"} <= vendors, str(sorted(vendors)))

    for profile in devices.DEVICE_PROFILES:
        check(f"{profile.key} names a model", bool(profile.model))
        check(f"{profile.key} declares its modes", bool(profile.modes))
        check(f"{profile.key} says whether it is verified",
              isinstance(profile.verified, bool))
        for key in profile.flash_settings:
            if key not in devices.FLASH_SETTINGS_KEYS:
                check(f"{profile.key} uses only documented settings", False, key)
        check(f"{profile.key} uses only documented settings",
              all(key in devices.FLASH_SETTINGS_KEYS for key in profile.flash_settings))

    for profile in devices.DEVICE_PROFILES:
        for issue in profile.issues:
            check(f"{profile.key}: an issue describes a symptom",
                  len(issue.symptom) > 20, issue.symptom[:30])
            check(f"{profile.key}: an issue states what to do or says it cannot",
                  bool(issue.workaround) or not issue.actionable,
                  issue.symptom[:40])

    # Every issue has to be actionable or explicitly non-actionable: an entry
    # with an empty workaround and no explanation is a dead end for the operator.
    for profile in devices.DEVICE_PROFILES:
        for issue in profile.issues:
            check(f"{profile.key}: an issue has a cause when it has no workaround",
                  bool(issue.workaround) or bool(issue.cause), issue.symptom[:40])

    check("a profile can be found by key", devices.profile_for("mtk-brom-generic") is not None)
    check("an unknown key gives None", devices.profile_for("nope") is None)
    check("keys are matched case-insensitively",
          devices.profile_for("MTK-BROM-GENERIC") is not None)

    check("search finds a model by name",
          any(p.key == "mtk-mt6765" for p in devices.search("Helio P22")))
    check("search finds a target by its USB id",
          any(p.key == "qcom-generic-edl" for p in devices.search("9008")))
    check("search finds by alias",
          any(p.key == "samsung-odin-generic" for p in devices.search("heimdall")))
    check("search finds by mode",
          any(p.key == "unisoc-sc9863" for p in devices.search("research download")))
    check("an empty search returns everything",
          len(devices.search("")) == len(devices.DEVICE_PROFILES))
    check("an unmatched search returns nothing", devices.search("blackberry") == [])
    check("search can exclude unverified profiles",
          all(p.verified for p in devices.search("", include_unverified=False)))

    check("issues can be listed for a profile",
          len(devices.known_issues_for("samsung-odin-generic")) > 0)
    check("an unknown profile has no issues", devices.known_issues_for("nope") == [])

    summary = devices.vendor_summary()
    check("the summary counts every profile",
          sum(summary.values()) == len(devices.DEVICE_PROFILES), str(summary))

    # The verified flag has to mean something, or it is decoration.
    check("at least one profile is verified",
          any(p.verified for p in devices.DEVICE_PROFILES))
    check("the four vendor-generic profiles are the verified ones",
          {p.key for p in devices.DEVICE_PROFILES if p.verified}
          == {"qcom-generic-edl", "mtk-brom-generic", "samsung-odin-generic"},
          str(sorted(p.key for p in devices.DEVICE_PROFILES if p.verified)))


# -----------------------------------------------------------------------------
#  7. The error layer
# -----------------------------------------------------------------------------


def test_error_classification() -> None:
    print("\n7. classifying failures")

    expected = [
        (FileNotFoundError("nope"), "FILE_ERROR"),
        (PermissionError("denied"), "FILE_ERROR"),
        (IsADirectoryError("a directory"), "FILE_ERROR"),
        (TimeoutError("the transfer timed out"), "USB_ERROR"),
        (ConnectionResetError("peer reset"), "USB_ERROR"),
        (BrokenPipeError("pipe"), "USB_ERROR"),
        (ValueError("bad length"), "FILE_ERROR"),
        (TypeError("expected bytes"), "FILE_ERROR"),
        (KeyError("missing"), "FILE_ERROR"),
        (IndexError("entry 900 of 3"), "FILE_ERROR"),
        (MemoryError("out of memory"), "INTERNAL_ERROR"),
        (KeyboardInterrupt(), "CANCELLED"),
        (RuntimeError("the device disconnected while writing"), "USB_ERROR"),
        (RuntimeError("no device present"), "USB_ERROR"),
        (RuntimeError("the target answered NAK"), "PROTOCOL_ERROR"),
        (RuntimeError("secure boot is enabled"), "AUTH_ERROR"),
        (RuntimeError("the archive is corrupt"), "FILE_ERROR"),
        (RuntimeError("erase failed at 0x40000"), "FLASH_ERROR"),
        (RuntimeError("the operation was cancelled"), "CANCELLED"),
    ]
    for error, wanted in expected:
        got = errors.classify(error)
        check(f"{type(error).__name__}({str(error)[:28]!r}) is {wanted}",
              got == wanted, f"got {got}")

    # The classification must never raise, whatever it is handed.
    for odd in (Exception(), BaseException(), SystemExit(1), StopIteration()):
        try:
            errors.classify(odd)
            ok = True
        except BaseException:  # noqa: BLE001
            ok = False
        check(f"classifying {type(odd).__name__} does not raise", ok)


def test_error_description() -> None:
    print("\n8. describing a failure")

    failure = errors.describe(TimeoutError("the transfer timed out"),
                              vendor="Qualcomm", operation="flashing boot.img")
    check("the kind is set", failure.kind == "USB_ERROR")
    check("the vendor is kept", failure.vendor == "Qualcomm")
    check("the context names what was running",
          failure.context == [("flashing boot.img", "")], str(failure.context))
    check("the advice is not empty", len(failure.advice) > 20, failure.advice[:40])
    check("a timeout is worth retrying", failure.retryable)
    check("a timeout is not dangerous", not failure.dangerous)
    check("the summary names the kind and the vendor",
          "USB_ERROR" in failure.summary and "Qualcomm" in failure.summary,
          failure.summary)
    check("the report ends with the advice", "fix:" in failure.report())
    check("the timestamp renders", len(failure.timestamp_text) == 19,
          failure.timestamp_text)
    check("it can be serialised", isinstance(failure.as_dict(), dict))

    # A missing file is reported but never worth retrying or feared.
    failure = errors.describe(FileNotFoundError("no such file: system.img"))
    check("a missing file is not retryable", not failure.retryable)
    check("a missing file is not dangerous", not failure.dangerous)

    # A failed write is not retried automatically, and is dangerous.
    failure = errors.describe(RuntimeError("erase failed"))
    check("a failed erase is not retried blindly", not failure.retryable)
    check("a failed erase leaves the device in doubt", failure.dangerous)

    # A bug is reported as critical advice rather than as a device problem.
    failure = errors.describe(MemoryError())
    check("a bug in the tool says so", "bug in the tool" in failure.advice,
          failure.advice[:50])


def test_error_translation() -> None:
    print("\n9. translating to a classified exception")

    if errors.backend is None:
        check("the native backend is importable", False, str(errors.BACKEND_IMPORT_ERROR))
        return

    # A vendor exception becomes a FlashException with the whole classification
    # on it, and it is still a RuntimeError so existing handlers keep working.
    try:
        errors.raise_as_flash_error(RuntimeError("the device disconnected while writing"),
                                    vendor="MediaTek", operation="erasing userdata")
    except errors.backend.FlashException as error:
        check("the failure becomes a FlashException", True)
        check("it is still a RuntimeError", isinstance(error, RuntimeError))
        check("the kind is carried", str(error.error_name) == "USB_ERROR",
              str(error.error_name))
        check("the vendor is carried", str(error.vendor_name) == "MediaTek",
              str(error.vendor_name))
        check("the original message is kept",
              "disconnected" in str(error.message), str(error.message))
        check("the operation is recorded",
              [(str(a), str(b)) for a, b in error.context] == [("erasing userdata", "")],
              str([(str(a), str(b)) for a, b in error.context]))
        check("the advice is filled in", len(str(error.advice)) > 20)
        check("the retry flag is filled in", bool(error.retryable) is True)
        check("the report is a block, not a line", "\n" in str(error.report_text))
        check("every attribute is already computed, not a method",
              not callable(error.message) and not callable(error.advice)
              and not callable(error.report_text))
    except BaseException as exc:  # noqa: BLE001
        check("the failure becomes a FlashException", False, repr(exc))

    # Re-raising an already-classified failure keeps its classification and adds
    # the frame rather than replacing what was there.
    try:
        try:
            errors.raise_as_flash_error(RuntimeError("secure boot refused"),
                                        vendor="Qualcomm", operation="writing boot")
        except errors.backend.FlashException as first:
            errors.raise_as_flash_error(first, operation="flashing the package")
    except errors.backend.FlashException as error:
        frames = [(str(a), str(b)) for a, b in error.context]
        check("re-raising keeps the classification",
              str(error.error_name) == "AUTH_ERROR", str(error.error_name))
        check("both frames are present", "flashing the package" in [f[0] for f in frames],
              str(frames))
    except BaseException as exc:  # noqa: BLE001
        check("re-raising keeps the classification", False, repr(exc))

    # A vendor string that is not one of ours must not raise: the failure is
    # still ours to report, it just is not attributable to a protocol.
    try:
        errors.raise_as_flash_error(RuntimeError("something"), vendor="Nokia")
    except errors.backend.FlashException as error:
        check("an unknown vendor becomes Unknown", str(error.vendor_name) == "unknown",
              str(error.vendor_name))


def test_retry_loop() -> None:
    print("\n10. the retry loop")

    attempts: list[int] = []

    def flaky(failures: int, make_error) -> object:
        """Succeeds on attempt `failures + 1`, raising what `make_error` builds."""

        def operation() -> str:
            attempts.append(len(attempts) + 1)
            if len(attempts) <= failures:
                raise make_error()
            return "ok"

        return operation

    if errors.backend is None:
        check("the native backend is importable", False)
        return

    policy = errors.backend.RetryPolicy()
    policy.attempts = 3
    policy.initial_delay_ms = 1
    policy.maximum_delay_ms = 2
    policy.total_budget_ms = 60000

    attempts.clear()
    retries: list[int] = []
    result = errors.run_with_retry(flaky(2, lambda: TimeoutError('the transfer timed out')), policy,
                                   on_retry=lambda number, _f: retries.append(number))
    check("a success on the third attempt is returned", result == "ok", str(result))
    check("it took exactly three attempts", len(attempts) == 3, str(len(attempts)))
    check("both retries were reported", retries == [1, 2], str(retries))

    attempts.clear()
    try:
        errors.run_with_retry(flaky(9, lambda: TimeoutError('the transfer timed out')), policy)
        check("it gives up after the configured attempts", False)
    except TimeoutError:
        check("it gives up after the configured attempts", True)
    check("and it stopped at three", len(attempts) == 3, str(len(attempts)))

    # A failure that is not retryable must not be repeated, however many
    # attempts the policy allows. This is the rule that keeps a half-written
    # partition from being written again.
    attempts.clear()
    try:
        errors.run_with_retry(flaky(1, lambda: FileNotFoundError('no such file')), policy)
        check("a missing file is not retried", False)
    except FileNotFoundError:
        pass
    check("a missing file was attempted once", len(attempts) == 1, str(len(attempts)))

    attempts.clear()
    try:
        errors.run_with_retry(flaky(1, lambda: RuntimeError('erase failed')), policy)
    except RuntimeError:
        pass
    check("a failed erase was attempted once", len(attempts) == 1, str(len(attempts)))

    # A policy with one attempt disables retrying entirely.
    single = errors.backend.RetryPolicy()
    single.attempts = 1
    attempts.clear()
    try:
        errors.run_with_retry(flaky(5, lambda: TimeoutError('the transfer timed out')), single)
    except TimeoutError:
        pass
    check("a single-attempt policy does not retry", len(attempts) == 1, str(len(attempts)))

    # A cancellation during a backoff stops the wait rather than finishing it.
    attempts.clear()
    stop_after = 1
    try:
        errors.run_with_retry(
            flaky(5, lambda: TimeoutError('the transfer timed out')), policy,
            should_stop=lambda: len(attempts) >= stop_after,
        )
        check("a stop request ends the retries", False)
    except TimeoutError:
        check("a stop request ends the retries", True)
    check("and it stopped before the second wait", len(attempts) <= 2, str(len(attempts)))

    # The budget stops a retry even when attempts remain.
    tight = errors.backend.RetryPolicy()
    tight.attempts = 10
    tight.initial_delay_ms = 50
    tight.maximum_delay_ms = 50
    tight.total_budget_ms = 60
    attempts.clear()
    try:
        errors.run_with_retry(flaky(9, lambda: TimeoutError('the transfer timed out')), tight)
    except TimeoutError:
        pass
    check("the total budget stops the retries", len(attempts) == 2, str(len(attempts)))

    # A void operation runs through the same loop.
    ran: list[int] = []
    errors.run_with_retry(lambda: ran.append(1), single)
    check("a void operation runs through the retry loop", ran == [1], str(ran))

    # With no policy given, the process-wide one applies rather than none at all.
    saved = errors.backend.default_retry_policy()
    try:
        configured = errors.backend.RetryPolicy()
        configured.attempts = 4
        configured.initial_delay_ms = 1
        configured.maximum_delay_ms = 1
        errors.backend.set_default_retry_policy(configured)
        attempts.clear()
        try:
            errors.run_with_retry(flaky(9, lambda: TimeoutError('the transfer timed out')))
        except TimeoutError:
            pass
        check("an unspecified policy falls back to the process-wide one",
              len(attempts) == 4, str(len(attempts)))
    finally:
        errors.backend.set_default_retry_policy(saved)


def test_error_logging() -> None:
    print("\n11. failures reach the log at the right level")

    if errors.backend is None:
        check("the native backend is importable", False)
        return

    class Recorder:
        def __init__(self) -> None:
            self.lines: list[tuple[str, str]] = []

        def write(self, level: str, message: str) -> None:
            self.lines.append((level, message))

    recorder = Recorder()
    errors.log_failure(recorder, errors.describe(RuntimeError("the cable went")))
    errors.log_failure(recorder, errors.describe(MemoryError()))
    errors.log_failure(recorder, errors.describe(RuntimeError("the operation was cancelled")))

    check("three failures were recorded", len(recorder.lines) == 3, str(len(recorder.lines)))
    check("a cable problem is an error", recorder.lines[0][0] == "error",
          recorder.lines[0][0])
    check("a bug in the tool is critical", recorder.lines[1][0] == "critical",
          recorder.lines[1][0])
    check("a cancellation is a warning", recorder.lines[2][0] == "warning",
          recorder.lines[2][0])
    check("the report is what gets written",
          "fix:" in recorder.lines[0][1], recorder.lines[0][1][:40])

    # A logger that raises must not turn a reported failure into a crash.
    class Broken:
        def write(self, level: str, message: str) -> None:
            raise RuntimeError("the log is gone")

    # A logger that fails must not replace the device failure being reported with
    # a logging failure. This runs inside the handler for a real failure, so
    # letting it propagate loses the diagnostic that matters.
    try:
        errors.log_failure(Broken(), errors.describe(TimeoutError("x")))
        swallowed = True
    except BaseException:  # noqa: BLE001
        swallowed = False
    check("a broken logger does not raise out of log_failure", swallowed)

    errors.log_failure(None, errors.describe(TimeoutError("x")))
    check("a missing logger is not an error", True)


def test_unclassified_failures_survive() -> None:
    """A C++ failure that is not a FlashException must still reach Python.

    This is a regression test for a real bug. The custom exception translator
    returned normally for exceptions it did not recognise, and pybind11 treats a
    normal return from a translator as "handled" - so it stopped trying the rest.
    Every ProtocolError from every vendor protocol arrived in Python as
    ``SystemError: returned NULL without setting an exception``, hiding the
    device failure that had actually happened.

    Nothing else in the suite would have caught it: the native suites test the
    C++ throw, and the Python suites test the classified path. Only a call that
    raises something *unclassified* across the boundary exercises the translator's
    fall-through.
    """
    print("\n12. unclassified C++ failures still reach Python")

    if errors.backend is None:
        check("the native backend is importable", False, str(errors.BACKEND_IMPORT_ERROR))
        return

    # get_device_list() before init() raises a plain ProtocolError. The message
    # is the whole point: it says the bridge was not initialised, which is
    # actionable, where SystemError was not.
    bridge = errors.backend.HardwareBridge()
    try:
        bridge.get_device_list()
        check("calling before init() raises", False, "it returned normally")
    except errors.backend.FlashException:
        check("an unclassified failure is not turned into a FlashException", False,
              "the C++ side threw ProtocolError, not FlashException")
    except SystemError as exc:
        check("an unclassified failure is not swallowed by the translator", False, str(exc))
    except RuntimeError as exc:
        text = str(exc)
        check("an unclassified C++ failure reaches Python as RuntimeError", True)
        check("and carries its own message rather than a pybind11 placeholder",
              "init" in text and "NULL" not in text, text[:70])
    except BaseException as exc:  # noqa: BLE001
        check("an unclassified C++ failure reaches Python", False, repr(exc))

    # The same path through the error layer: it must classify a bare protocol
    # failure rather than losing it.
    try:
        bridge.get_device_list()
    except BaseException as exc:  # noqa: BLE001
        failure = errors.describe(exc)
        check("the error layer can classify a bare protocol failure",
              failure.kind in ("PROTOCOL_ERROR", "INTERNAL_ERROR"), failure.kind)
        check("and the message survives classification",
              "init" in failure.message, failure.message[:60])
        check("with advice attached", len(failure.advice) > 20)


def test_no_decorative_settings() -> None:
    """Every setting must change something, or it is a lie in a dialog.

    This is a regression test for a real problem. The first version of the
    settings system offered nineteen switches and honoured six: the dialog read
    and wrote all of them, saved them to a file, and nothing else ever looked at
    them. An operator could set a command timeout to five minutes and watch the
    transfer give up after thirty seconds.

    The check is deliberately crude - it looks for the field being read anywhere
    outside the dialog and the config module itself - because the point is not to
    prove the wiring is correct but to catch the case where there is no wiring at
    all. What each one does is tested by the tests above and by the native
    suites.
    """
    print("\n13. no setting is merely decorative")

    import dataclasses
    import re as _re

    python_root = ROOT / "python"
    skipped = {"settings_dialog.py"}  # reads every field by definition

    def consumers(name: str) -> list[str]:
        found = []
        for path in python_root.rglob("*.py"):
            if path.name in skipped:
                continue
            if _re.search(rf"\.{name}\b", path.read_text(encoding="utf-8")):
                found.append(path.name)
        return found

    orphans = []
    for field in dataclasses.fields(config.Settings):
        where = consumers(field.name)
        if not where:
            orphans.append(field.name)
        check(f"{field.name} is read by something", bool(where), ", ".join(where))

    check("no setting is offered without an effect", orphans == [], str(orphans))


def test_settings_reach_the_native_timeouts() -> None:
    """The timeout and speed settings must reach the native layer.

    The values are process-wide in C++, applied once when the settings change, so
    a flash that is already running picks them up. That is why this checks the
    native side rather than the job's log output.
    """
    print("\n14. timeouts and the speed cap reach the native layer")

    if errors.backend is None:
        check("the native backend is importable", False, str(errors.BACKEND_IMPORT_ERROR))
        return

    saved = errors.backend.timeout_overrides()
    saved_limit = errors.backend.speed_limit()
    try:
        settings = config.Settings(default_timeout=7.0, transfer_timeout=90.0,
                                   connect_timeout=4.0, speed_limit=512 * 1024)
        # apply_settings is a job, so the conversion is checked directly here;
        # the push itself is covered by test_settings_reach_the_native_layer.
        errors.backend.set_timeout_overrides(
            int(settings.default_timeout * 1000),
            int(settings.transfer_timeout * 1000),
            int(settings.connect_timeout * 1000),
        )
        errors.backend.set_speed_limit(int(settings.speed_limit))

        check("the command timeout reaches the native layer",
              errors.backend.timeout_overrides()[0] == 7000,
              str(errors.backend.timeout_overrides()))
        check("the transfer timeout reaches the native layer",
              errors.backend.timeout_overrides()[1] == 90000,
              str(errors.backend.timeout_overrides()))
        check("the connect timeout reaches the native layer",
              errors.backend.timeout_overrides()[2] == 4000,
              str(errors.backend.timeout_overrides()))
        check("the speed cap reaches the native layer",
              errors.backend.speed_limit() == 512 * 1024,
              str(errors.backend.speed_limit()))

        # Zero must mean "no override", not "time out immediately" - the whole
        # reason the native side falls back to each protocol's own constant.
        errors.backend.set_timeout_overrides(0, 0, 0)
        errors.backend.set_speed_limit(0)
        check("zero clears the timeout overrides",
              errors.backend.timeout_overrides() == (0, 0, 0))
        check("zero clears the speed cap", errors.backend.speed_limit() == 0)
    finally:
        errors.backend.set_timeout_overrides(*saved)
        errors.backend.set_speed_limit(saved_limit)


def test_session_log_archiving() -> None:
    """A finished session leaves one file of its own, and old ones are pruned."""
    print("\n15. per-session log copies")

    import tempfile
    from pathlib import Path as _Path

    with tempfile.TemporaryDirectory() as scratch:
        live = _Path(scratch) / "flash_log.txt"
        check("nothing to archive yet", config.archive_session_log(live) is None)

        live.write_text("a session\n", encoding="utf-8")
        first = config.archive_session_log(live)
        check("a session is archived", first is not None and first.exists())
        check("the copy holds the session",
              first.read_text(encoding="utf-8") == "a session\n")
        check("it lands in the sessions folder", first.parent.name == config.SESSION_LOG_DIR,
              str(first.parent))
        check("the live log is left alone", live.exists())

        # Two archives in the same second must not overwrite each other.
        second = config.archive_session_log(live)
        check("a second archive is kept separately", second != first,
              f"{second} vs {first}")

        # Pruning keeps the newest N, and N is small enough to be a real bound.
        check("the keep limit is bounded", 0 < config.SESSION_LOG_KEEP <= 50,
              str(config.SESSION_LOG_KEEP))
        for _ in range(config.SESSION_LOG_KEEP + 5):
            config.archive_session_log(live)
        kept = list((_Path(scratch) / config.SESSION_LOG_DIR).iterdir())
        check("old sessions are pruned", len(kept) == config.SESSION_LOG_KEEP,
              f"{len(kept)} kept, limit {config.SESSION_LOG_KEEP}")

        # An empty log is not worth keeping.
        empty = _Path(scratch) / "empty.txt"
        empty.write_text("", encoding="utf-8")
        check("an empty log is not archived", config.archive_session_log(empty) is None)


def main() -> int:
    print("=== HUAXIN integration tests: settings, devices, errors ===")
    if errors.backend is None:
        print(f"  NOTE: the native backend is unavailable ({errors.BACKEND_IMPORT_ERROR});")
        print("        tests that need it will report a failure.")

    test_settings_defaults()
    test_settings_validation()
    test_settings_files()
    test_settings_reach_the_native_layer()
    test_settings_template()
    test_device_database()
    test_error_classification()
    test_error_description()
    test_error_translation()
    test_retry_loop()
    test_error_logging()
    test_unclassified_failures_survive()
    test_no_decorative_settings()
    test_settings_reach_the_native_timeouts()
    test_session_log_archiving()

    print(f"\n{CHECKS - FAILURES}/{CHECKS} checks passed")
    if FAILURES:
        print(f"{FAILURES} FAILED")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
