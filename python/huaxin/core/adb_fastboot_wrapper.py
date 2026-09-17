"""ADB and Fastboot, driven as subprocesses.

Why shell out instead of implementing the protocols in C++
---------------------------------------------------------
ADB is not a wire protocol so much as a client/server system: the official `adb`
binary starts and talks to a background server that owns the USB endpoints, and
`fastboot` speaks a small USB protocol that Google's own tool already implements
correctly across chipsets, USB stacks and Windows driver quirks. Reimplementing
either would mean reimplementing the ADB server, and would lose interoperability
with every other tool on the machine. So: run the official binaries, and be
honest that the application depends on platform-tools being installed.

The C++/libusb path stays where it belongs - the vendor download modes
(Qualcomm EDL, MediaTek BROM, Unisoc, Samsung Odin) have no equivalent tool.

Threading
---------
Every function here takes a job context as its first argument and matches the
`fn(ctx, *args, **kwargs)` contract of `huaxin.workers.worker.Job`. That is not a
coincidence: a flash takes minutes, so all of this must run on the worker thread,
and the context is how output gets to the log and how cancellation gets in.

Output handling
---------------
`run_streaming` reads the child's output as it is produced and forwards each line
immediately. Lines are read as bytes and assembled by hand rather than with
`for line in process.stdout`, because adb and fastboot both draw progress bars
with carriage returns: universal-newline handling turns each of those ~100
updates per partition into a separate log line. Collapsing them to the state the
terminal would end up showing is the difference between a readable log and a
flooded one.
"""

from __future__ import annotations

import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol, Sequence

__all__ = [
    "AdbDevice",
    "AndroidTools",
    "CommandFailedError",
    "CommandResult",
    "CommandTimeoutError",
    "FastbootDevice",
    "ToolNotFoundError",
    "configure_tools",
    "default_tools",
    "erase_partition",
    "flash_partition",
    "format_command",
    "reset_default_tools",
    "get_adb_devices",
    "get_device_properties",
    "get_fastboot_devices",
    "parse_adb_devices",
    "parse_fastboot_devices",
    "parse_getprop",
    "reboot_device",
    "reboot_to_bootloader",
    "run_streaming",
    "validate_partition_name",
    "validate_serial",
    "verify_toolchain",
    "wipe_data",
]

# -- timeouts -----------------------------------------------------------------
DEFAULT_TIMEOUT = 30.0
REBOOT_TIMEOUT = 60.0
#: A partition write is bounded by the device, not by us. This is a backstop
#: against a wedged transfer, not a real time limit.
FLASH_TIMEOUT = 30.0 * 60.0
#: How long a terminated child gets to die before it is killed outright.
_CANCEL_GRACE_S = 5.0
#: A single line longer than this is flushed rather than buffered forever.
_MAX_LINE_BYTES = 64 * 1024
#: How often the wait loop wakes up to check for cancellation.
_POLL_INTERVAL_S = 0.1


class CommandContext(Protocol):
    """The slice of `huaxin.workers.worker.JobContext` this module uses.

    Declared structurally so the wrapper stays testable - and importable - with
    no Qt and no worker thread anywhere in sight.
    """

    def log(self, message: str, level: str = "info") -> None: ...

    def check_cancelled(self) -> None: ...

    @property
    def cancelled(self) -> bool: ...


# -- errors -------------------------------------------------------------------
class ToolNotFoundError(RuntimeError):
    """adb or fastboot could not be located."""

    #: Kept short and to the point, because this is the one error in the tool
    #: whose cause and fix are both certain. The settings page is named first:
    #: it is the route somebody can act on by clicking, and it is where the two
    #: paths are now decided. The environment variables are named second, for a
    #: machine being set up from a script where nobody opens a dialog.
    _HINT = (
        "Install Android platform-tools (they provide adb and fastboot), or set the "
        "path in Settings -> Tools & folders. On a machine configured by script, "
        "HUAXIN_ADB / HUAXIN_FASTBOOT also work."
    )

    def __init__(self, tool: str, searched: Sequence[str] = ()) -> None:
        # A configured full path that does not work is a different problem from a
        # tool that is simply not installed, and suggesting "set a path to this
        # path" for a path that is already a path reads as noise.
        is_path = any(separator and separator in tool for separator in (os.sep, os.altsep, "/"))
        if is_path:
            detail = f"{tool} does not exist or is not executable."
        else:
            detail = f"'{tool}' was not found on PATH or in any known SDK location."
        if searched:
            detail += "\nSearched: " + ", ".join(searched)
        detail += "\n" + self._HINT
        super().__init__(detail)
        self.tool = tool
        self.searched = tuple(searched)


class CommandFailedError(RuntimeError):
    """The tool ran and exited non-zero."""

    def __init__(self, argv: Sequence[str], returncode: int | None, tail: Sequence[str] = ()) -> None:
        lines = [f"{format_command(argv)} exited with {returncode}"]
        if tail:
            lines.append("Last output:")
            lines.extend(f"  {line}" for line in tail)
        super().__init__("\n".join(lines))
        self.argv = tuple(argv)
        self.returncode = returncode
        self.tail = tuple(tail)


class CommandTimeoutError(RuntimeError):
    """The tool did not finish in time and was terminated."""

    def __init__(self, argv: Sequence[str], timeout: float, tail: Sequence[str] = ()) -> None:
        lines = [f"{format_command(argv)} timed out after {timeout:.0f} s and was terminated"]
        if tail:
            lines.append("Last output:")
            lines.extend(f"  {line}" for line in tail)
        super().__init__("\n".join(lines))
        self.argv = tuple(argv)
        self.timeout = timeout
        self.tail = tuple(tail)


# -- validation ---------------------------------------------------------------
#: Partition names are passed as argv, so a name starting with '-' would be read
#: by fastboot as an option rather than a partition. Rejecting that is the
#: difference between "flash the boot partition" and "run an arbitrary flag".
_PARTITION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SERIAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

#: `adb reboot` targets we are willing to send. Anything else is a typo at best.
REBOOT_TARGETS = frozenset({"bootloader", "fastboot", "recovery", "sideload", "download", "edl", "system"})


def validate_partition_name(name: str) -> str:
    """Return a partition name safe to put in argv, or raise ValueError."""
    candidate = (name or "").strip()
    if not candidate:
        raise ValueError("partition name is empty")
    if not _PARTITION_RE.match(candidate):
        raise ValueError(
            f"invalid partition name {name!r}: expected letters, digits, '.', '_' or '-' "
            "(and it must not start with '-')"
        )
    return candidate


def validate_serial(serial: str) -> str:
    """Return a device serial safe to put in argv, or raise ValueError."""
    candidate = (serial or "").strip()
    if not candidate:
        raise ValueError("device serial is empty")
    if not _SERIAL_RE.match(candidate):
        raise ValueError(f"invalid device serial {serial!r}")
    return candidate


def format_command(argv: Sequence[str]) -> str:
    """Render argv the way the operator would have typed it."""
    parts = [str(part) for part in argv]
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


# -- device records -----------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AdbDevice:
    """One entry from `adb devices -l`."""

    serial: str
    state: str
    model: str = ""
    product: str = ""
    device: str = ""
    transport_id: str = ""

    @property
    def ready(self) -> bool:
        """True only for a device that is actually usable.

        `unauthorized` means the RSA prompt on the screen has not been accepted;
        `offline` means the connection is stale. Neither can run shell commands.
        """
        return self.state == "device"

    @property
    def label(self) -> str:
        return f"{self.serial} ({self.state}{', ' + self.model if self.model else ''})"


@dataclass(frozen=True, slots=True)
class FastbootDevice:
    """One entry from `fastboot devices -l`."""

    serial: str
    mode: str = "fastboot"
    product: str = ""


# -- parsers (pure functions: no process, no Qt, easy to test) ----------------
def parse_adb_devices(output: str) -> list[AdbDevice]:
    """Parse `adb devices -l` output."""
    devices: list[AdbDevice] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        # The daemon prints "* daemon not running; starting now at tcp:5037"
        # on a cold start and the listing starts with a header line.
        if line.startswith("*") or line.startswith("List of devices"):
            continue
        columns = line.split()
        if len(columns) < 2:
            continue
        serial, state = columns[0], columns[1]
        extras: dict[str, str] = {}
        for token in columns[2:]:
            key, separator, value = token.partition(":")
            if separator:
                extras[key] = value
        devices.append(
            AdbDevice(
                serial=serial,
                state=state,
                model=extras.get("model", ""),
                product=extras.get("product", ""),
                device=extras.get("device", ""),
                transport_id=extras.get("transport_id", ""),
            )
        )
    return devices


def parse_fastboot_devices(output: str) -> list[FastbootDevice]:
    """Parse `fastboot devices -l` output."""
    devices: list[FastbootDevice] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        columns = line.split()
        if len(columns) < 2:
            continue
        serial, mode = columns[0], columns[1]
        product = ""
        for token in columns[2:]:
            key, separator, value = token.partition(":")
            if separator and key == "product":
                product = value
        devices.append(FastbootDevice(serial=serial, mode=mode, product=product))
    return devices


def parse_getprop(output: str) -> dict[str, str]:
    """Parse `adb shell getprop` output: `[ro.product.model]: [Pixel 7]`."""
    properties: dict[str, str] = {}
    pattern = re.compile(r"^\[(?P<key>[^\]]+)\]:\s*\[(?P<value>.*)\]\s*$")
    for line in output.splitlines():
        match = pattern.match(line.strip())
        if match:
            properties[match.group("key")] = match.group("value")
    return properties


# -- output assembly ----------------------------------------------------------
def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def feed_output(chunk: bytes, pending: bytearray) -> list[str]:
    """Add `chunk` to `pending` and return the lines that are now complete.

    A carriage return means the terminal would have overwritten the line, so
    only the text after the last one survives. Without that, `fastboot flash`
    contributes a line per percent of progress.
    """
    pending.extend(chunk)
    completed: list[str] = []

    while True:
        newline = pending.find(b"\n")
        if newline < 0:
            break
        raw = bytes(pending[:newline])
        del pending[: newline + 1]
        if raw.endswith(b"\r"):  # CRLF
            raw = raw[:-1]
        carriage = raw.rfind(b"\r")
        if carriage >= 0:
            raw = raw[carriage + 1 :]
        completed.append(_decode(raw))

    # Drop text that a later carriage return has already superseded, but keep
    # the tail that has not been overwritten yet.
    carriage = pending.rfind(b"\r")
    if carriage >= 0:
        del pending[: carriage + 1]

    if len(pending) >= _MAX_LINE_BYTES:
        completed.append(_decode(bytes(pending)))
        pending.clear()

    return completed


# -- process plumbing ---------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    lines: tuple[str, ...]

    @property
    def output(self) -> str:
        return "\n".join(self.lines)


def _popen(argv: Sequence[str], cwd: str | None) -> subprocess.Popen[bytes]:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        # Without this a GUI application flashes a console window for every
        # single command it runs.
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.Popen(
            [str(part) for part in argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # one stream: correct interleaving, no deadlock
            bufsize=0,
            cwd=cwd,
            **kwargs,
        )
    except FileNotFoundError as exc:
        raise ToolNotFoundError(str(argv[0])) from exc
    except OSError as exc:
        raise CommandFailedError(argv, None, [str(exc)]) from exc


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Stop a child that is still running.

    adb and fastboot get no graceful shutdown on Windows - TerminateProcess is
    the only mechanism available - so they may leave their server or a USB
    transfer behind. That is the same outcome as the operator pressing Ctrl+C,
    and better than leaving the process running.
    """
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=_CANCEL_GRACE_S)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
        process.wait(timeout=_CANCEL_GRACE_S)
    except (OSError, subprocess.TimeoutExpired):
        pass


def run_streaming(
    ctx: CommandContext,
    argv: Sequence[str],
    *,
    timeout: float | None = DEFAULT_TIMEOUT,
    cwd: str | None = None,
    level: str = "output",
) -> CommandResult:
    """Run a command, forwarding its output line by line as it arrives.

    Blocks the calling thread until the command finishes, so it belongs on the
    worker thread. Raises CommandTimeoutError, CommandFailedError or - via the
    context - whatever cancellation raises.
    """
    if not argv:
        raise ValueError("argv is empty")

    ctx.log(f"$ {format_command(argv)}", "debug")
    process = _popen(argv, cwd)

    lines: queue.Queue[str | None] = queue.Queue()
    pending = bytearray()
    collected: list[str] = []

    def pump() -> None:
        stream = process.stdout
        try:
            while stream is not None:
                chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
                if not chunk:
                    break
                for line in feed_output(chunk, pending):
                    lines.put(line)
        except (OSError, ValueError):
            pass  # the process died under us; the return code tells the story
        finally:
            if pending:
                lines.put(_decode(bytes(pending)))
            lines.put(None)

    reader = threading.Thread(target=pump, name="huaxin-command-output", daemon=True)
    reader.start()

    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while True:
            if ctx.cancelled:
                ctx.check_cancelled()  # raises, and the finally block stops the child
            try:
                line = lines.get(timeout=_POLL_INTERVAL_S)
            except queue.Empty:
                if deadline is not None and time.monotonic() > deadline:
                    raise CommandTimeoutError(argv, timeout, collected[-5:])
                continue
            if line is None:
                break
            collected.append(line)
            ctx.log(line, level)
    finally:
        if process.poll() is None:
            _terminate(process)
        reader.join(timeout=2.0)

    returncode = process.wait(timeout=_CANCEL_GRACE_S)
    if returncode != 0:
        raise CommandFailedError(argv, returncode, collected[-5:])
    return CommandResult(tuple(str(part) for part in argv), returncode, tuple(collected))


# -- tool discovery -----------------------------------------------------------
class AndroidTools:
    """Locates the adb and fastboot binaries.

    Invariant: `adb_path` and `fastboot_path` are either None or the path of a
    file that exists. A path handed to the constructor is checked here rather
    than trusted, so `path is not None` always means "runnable" - callers rely on
    that to decide whether to execute anything at all.
    """

    def __init__(self, adb: str | os.PathLike[str] | None = None,
                 fastboot: str | os.PathLike[str] | None = None) -> None:
        self._searched: list[str] = []
        self._adb = self._accept_explicit(adb)
        self._fastboot = self._accept_explicit(fastboot)

    def _accept_explicit(self, path: str | os.PathLike[str] | None) -> Path | None:
        if not path:
            return None
        candidate = Path(path)
        self._searched.append(str(candidate))
        return candidate if candidate.is_file() else None

    # -- discovery ---------------------------------------------------------

    @staticmethod
    def candidate_dirs() -> list[Path]:
        """Places platform-tools is commonly installed, most specific first."""
        dirs: list[Path] = []
        for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            value = os.environ.get(variable)
            if value:
                dirs.append(Path(value) / "platform-tools")
        if os.name == "nt":
            local = os.environ.get("LOCALAPPDATA")
            if local:
                dirs.append(Path(local) / "Android" / "Sdk" / "platform-tools")
            dirs.extend((Path("C:/platform-tools"), Path("C:/adb")))
        else:
            dirs.extend((Path.home() / "Android" / "Sdk" / "platform-tools",
                         Path("/usr/lib/android-sdk/platform-tools")))
        # Shipped next to the application, for a self-contained install.
        dirs.append(Path(__file__).resolve().parents[3] / "third_party" / "platform-tools")
        return dirs

    def _find(self, tool: str) -> Path | None:
        override = os.environ.get(f"HUAXIN_{tool.upper()}")
        if override:
            path = Path(override)
            self._searched.append(str(path))
            if path.is_file():
                return path

        on_path = shutil.which(tool)
        self._searched.append(f"PATH ({tool})")
        if on_path:
            return Path(on_path)

        suffix = ".exe" if os.name == "nt" else ""
        for directory in self.candidate_dirs():
            candidate = directory / f"{tool}{suffix}"
            self._searched.append(str(candidate))
            if candidate.is_file():
                return candidate
        return None

    @classmethod
    def discover(cls) -> "AndroidTools":
        instance = cls()
        instance._adb = instance._find("adb")
        instance._fastboot = instance._find("fastboot")
        return instance

    # -- accessors ---------------------------------------------------------

    @property
    def adb_path(self) -> Path | None:
        return self._adb

    @property
    def fastboot_path(self) -> Path | None:
        return self._fastboot

    @property
    def searched(self) -> tuple[str, ...]:
        return tuple(self._searched)

    @property
    def missing(self) -> list[str]:
        found = []
        if self._adb is None:
            found.append("adb")
        if self._fastboot is None:
            found.append("fastboot")
        return found

    def require_adb(self) -> Path:
        if self._adb is None:
            raise ToolNotFoundError("adb", self._searched)
        return self._adb

    def require_fastboot(self) -> Path:
        if self._fastboot is None:
            raise ToolNotFoundError("fastboot", self._searched)
        return self._fastboot

    def __repr__(self) -> str:
        return f"AndroidTools(adb={self._adb}, fastboot={self._fastboot})"


_default_tools: AndroidTools | None = None
_default_lock = threading.Lock()


def default_tools() -> AndroidTools:
    """The process-wide discovered toolchain, resolved once, on first use."""
    global _default_tools
    with _default_lock:
        if _default_tools is None:
            _default_tools = AndroidTools.discover()
        return _default_tools


def configure_tools(adb_path: str = "", fastboot_path: str = "") -> AndroidTools:
    """Rebuilds the process-wide toolchain from the operator's settings.

    Called at startup and whenever the settings change. The paths are a
    *preference*, not an override: an empty string, or a path that does not
    exist, falls through to the same discovery the tool would have done anyway -
    because a settings file pointing at an adb that has since been uninstalled
    must not leave the application unable to find the one that is installed.
    """
    global _default_tools
    with _default_lock:
        tools = AndroidTools(adb_path or None, fastboot_path or None)
        if tools.adb_path is None:
            tools._adb = tools._find("adb")
        if tools.fastboot_path is None:
            tools._fastboot = tools._find("fastboot")
        _default_tools = tools
        return tools


def reset_default_tools() -> None:
    """Forgets the resolved toolchain, so the next use discovers it again."""
    global _default_tools
    with _default_lock:
        _default_tools = None


# -- command builders ---------------------------------------------------------
def _adb_argv(tools: AndroidTools, serial: str | None, args: Iterable[str]) -> list[str]:
    argv = [str(tools.require_adb())]
    if serial:
        argv.extend(("-s", validate_serial(serial)))
    argv.extend(args)
    return argv


def _fastboot_argv(tools: AndroidTools, serial: str | None, args: Iterable[str]) -> list[str]:
    argv = [str(tools.require_fastboot())]
    if serial:
        argv.extend(("-s", validate_serial(serial)))
    argv.extend(args)
    return argv


# -- discovery commands -------------------------------------------------------
def verify_toolchain(
    ctx: CommandContext, tools: AndroidTools | None = None, *, timeout: float = DEFAULT_TIMEOUT
) -> AndroidTools:
    """Report where adb and fastboot were found, and their versions.

    This is a diagnostic, so a missing tool is a finding, not a failure: it is
    reported at warning level and the function still returns normally. Raising
    here would paint a successful check red, and the hard failure belongs where
    a command is actually attempted.
    """
    tools = tools or default_tools()

    for name, path in (("adb", tools.adb_path), ("fastboot", tools.fastboot_path)):
        if path is None:
            ctx.log(f"{name}: not found", "warn")
        else:
            ctx.log(f"{name}: {path}", "ok")

    if tools.adb_path is not None:
        run_streaming(ctx, [str(tools.adb_path), "version"], timeout=timeout)
    if tools.fastboot_path is not None:
        # fastboot has no `version` subcommand; --version is the documented flag.
        run_streaming(ctx, [str(tools.fastboot_path), "--version"], timeout=timeout)

    missing = tools.missing
    if missing:
        ctx.log(
            f"toolchain incomplete: {', '.join(missing)} missing. Install Android "
            "platform-tools, or set the path in Settings -> Tools & folders.",
            "warn",
        )
    else:
        ctx.log("toolchain complete", "ok")
    return tools


def get_adb_devices(
    ctx: CommandContext,
    tools: AndroidTools | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[AdbDevice]:
    """List devices known to the ADB server."""
    tools = tools or default_tools()
    result = run_streaming(ctx, _adb_argv(tools, None, ["devices", "-l"]), timeout=timeout)

    devices = parse_adb_devices(result.output)
    if devices:
        ctx.log(f"{len(devices)} ADB device(s): " + ", ".join(d.label for d in devices), "ok")
        for device in devices:
            if not device.ready:
                ctx.log(
                    f"{device.serial} is '{device.state}' - "
                    + ("accept the RSA prompt on the device screen"
                       if device.state == "unauthorized" else "reconnect the device"),
                    "warn",
                )
    else:
        ctx.log("no ADB devices reported (device attached and USB debugging enabled?)", "warn")
    return devices


def get_fastboot_devices(
    ctx: CommandContext,
    tools: AndroidTools | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[FastbootDevice]:
    """List devices currently in fastboot or fastbootd mode."""
    tools = tools or default_tools()
    result = run_streaming(ctx, _fastboot_argv(tools, None, ["devices", "-l"]), timeout=timeout)

    devices = parse_fastboot_devices(result.output)
    if devices:
        ctx.log(f"{len(devices)} fastboot device(s): "
                + ", ".join(f"{d.serial} ({d.mode})" for d in devices), "ok")
    else:
        ctx.log("no fastboot devices reported (boot the device into the bootloader first)", "warn")
    return devices


# -- informational ------------------------------------------------------------
#: Properties worth putting in the log rather than the full several-hundred-key dump.
_SUMMARY_PROPERTIES = (
    "ro.product.manufacturer",
    "ro.product.model",
    "ro.product.device",
    "ro.build.version.release",
    "ro.build.id",
    "ro.build.version.security_patch",
    "ro.boot.slot_suffix",
    "ro.build.type",
    "ro.debuggable",
)


def get_device_properties(
    ctx: CommandContext,
    tools: AndroidTools | None = None,
    serial: str | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, str]:
    """Read the full property table of a booted device and summarise it."""
    tools = tools or default_tools()
    result = run_streaming(ctx, _adb_argv(tools, serial, ["shell", "getprop"]), timeout=timeout)

    properties = parse_getprop(result.output)
    if not properties:
        ctx.log("getprop returned nothing - is the device in 'device' state?", "warn")
        return properties

    ctx.log(f"{len(properties)} properties read", "debug")
    for key in _SUMMARY_PROPERTIES:
        value = properties.get(key)
        if value:
            ctx.log(f"{key} = {value}", "info")
    return properties


# -- state-changing commands --------------------------------------------------
def reboot_device(
    ctx: CommandContext,
    tools: AndroidTools | None = None,
    serial: str | None = None,
    target: str = "bootloader",
    *,
    timeout: float = REBOOT_TIMEOUT,
) -> None:
    """Reboot a booted device into `target` (bootloader, edl, download, ...)."""
    if target not in REBOOT_TARGETS:
        raise ValueError(f"unsupported reboot target {target!r}; expected one of {sorted(REBOOT_TARGETS)}")
    tools = tools or default_tools()
    ctx.log(f"rebooting into {target}", "info")
    run_streaming(ctx, _adb_argv(tools, serial, ["reboot", target]), timeout=timeout)


def reboot_to_bootloader(
    ctx: CommandContext,
    tools: AndroidTools | None = None,
    serial: str | None = None,
    *,
    timeout: float = REBOOT_TIMEOUT,
) -> None:
    """`adb reboot bootloader` - the usual way into fastboot mode."""
    reboot_device(ctx, tools, serial, "bootloader", timeout=timeout)


def flash_partition(
    ctx: CommandContext,
    tools: AndroidTools | None = None,
    serial: str | None = None,
    partition: str = "",
    image: str | os.PathLike[str] = "",
    *,
    timeout: float = FLASH_TIMEOUT,
) -> None:
    """Write an image to a partition. Requires the device to be in fastboot mode."""
    tools = tools or default_tools()
    partition_name = validate_partition_name(partition)

    image_path = Path(image)
    if not image_path.is_file():
        raise ValueError(f"image file does not exist: {image_path}")

    size_mb = image_path.stat().st_size / (1024 * 1024)
    ctx.log(f"writing {partition_name} <- {image_path.name} ({size_mb:.1f} MiB)", "info")
    run_streaming(
        ctx,
        _fastboot_argv(tools, serial, ["flash", partition_name, str(image_path)]),
        timeout=timeout,
    )
    ctx.log(f"{partition_name} written", "ok")


def erase_partition(
    ctx: CommandContext,
    tools: AndroidTools | None = None,
    serial: str | None = None,
    partition: str = "",
    *,
    timeout: float = FLASH_TIMEOUT,
) -> None:
    """Erase a partition. Destructive: the contents are not recoverable."""
    tools = tools or default_tools()
    partition_name = validate_partition_name(partition)
    ctx.log(f"erasing {partition_name}", "warn")
    run_streaming(ctx, _fastboot_argv(tools, serial, ["erase", partition_name]), timeout=timeout)
    ctx.log(f"{partition_name} erased", "ok")


def wipe_data(
    ctx: CommandContext,
    tools: AndroidTools | None = None,
    serial: str | None = None,
    *,
    timeout: float = FLASH_TIMEOUT,
) -> None:
    """`fastboot -w`: erase userdata and cache.

    This destroys every account, file and setting on the device and forces the
    next boot through setup. The UI confirms before calling it.
    """
    tools = tools or default_tools()
    ctx.log("fastboot -w: erasing userdata and cache; all user data is lost", "warn")
    run_streaming(ctx, _fastboot_argv(tools, serial, ["-w"]), timeout=timeout)
    ctx.log("user data wiped", "ok")


def capture(
    ctx: CommandContext,
    argv: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> CommandResult:
    """Run an arbitrary command through the same streaming path.

    Escape hatch for diagnostics from the UI; the argv is not validated beyond
    the checks the caller has already done.
    """
    return run_streaming(ctx, argv, timeout=timeout)
