"""User settings, stored as JSON next to the user's data.

WHY A FILE AND NOT THE REGISTRY, and why not QSettings: an operator who has a
working setup on one machine wants to copy it to the next one, and a support
engineer wants to ask for it when a flash misbehaves. A readable JSON file in a
predictable place does both; an opaque per-platform store does neither.

THE THREE RULES THIS MODULE FOLLOWS

1. A bad settings file must never stop the tool from starting. A corrupted or
   hand-edited file falls back to the defaults and says so, rather than throwing
   on import.
2. Values are validated on the way in. A timeout of -1 or a log level of
   "loud" is a typo, and the answer is to ignore it and keep the default - not
   to pass it down to a USB transfer.
3. Settings that change tool behaviour live here and nowhere else. The backend
   reads them through `apply_to_logger()`; nothing else keeps a second copy.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

__all__ = [
    "Settings",
    "SETTINGS_PATH",
    "LOG_LEVELS",
    "NUMBER_BOUNDS",
    "SPEED_CHOICES",
    "default_log_path",
    "template_path",
    "active_settings",
    "set_active_settings",
    "load_settings",
    "save_settings",
    "settings_path",
    "reset_settings",
    "archive_session_log",
    "SESSION_LOG_KEEP",
]

#: Where settings are kept. %APPDATA% on Windows, XDG on Linux, ~/Library on
#: macOS; falling back to the home directory when none of those are set.
_APP_DIR_NAME = "HuaxinTool"


def _default_directory() -> Path:
    """The per-user configuration directory for this platform."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / _APP_DIR_NAME
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        if base:
            return Path(base) / _APP_DIR_NAME
        home = os.environ.get("HOME")
        if home:
            return Path(home) / ".config" / _APP_DIR_NAME
    # Last resort. Better a settings file in an odd place than no settings at all.
    return Path.home() / f".{_APP_DIR_NAME.lower()}"


SETTINGS_PATH = _default_directory() / "settings.json"

# --- the active settings -----------------------------------------------------
#
# Some settings have to be readable from deep in the UI, at the moment a button
# is pressed - the confirmation before an erase, the options a scan is started
# with. Threading a Settings object through every panel constructor for the sake
# of one boolean would put a settings parameter on a dozen classes that otherwise
# have no business knowing about settings at all.
#
# So there is one process-wide active Settings, set once at startup and replaced
# when the dialog is accepted. The same shape as the native retry policy, and for
# the same reason.
_ACTIVE: "Settings | None" = None


def active_settings() -> "Settings":
    """The settings the running application is using.

    Returns the shipped defaults before startup has set them, so a caller can
    never get None and have to guess what that means.
    """
    return _ACTIVE if _ACTIVE is not None else Settings()


def set_active_settings(settings: "Settings") -> None:
    """Replaces the active settings. Called at startup and when the dialog is accepted."""
    global _ACTIVE
    _ACTIVE = settings

#: Levels the logger understands, matching huaxin_core's parse_level().
LOG_LEVELS = ("debug", "info", "warning", "error", "critical")

#: Flashing speed caps some hosts need. The value is bytes per second; 0 means
#: no cap. A hub, an extension cable or a virtual machine all show up as a link
#: that enumerates fine and then drops packets during a long write.
SPEED_CHOICES = (0, 256 * 1024, 1024 * 1024, 4 * 1024 * 1024)


#: Bounds for the numeric settings, one row per field.
#:
#: A shared range would be wrong in both directions: a transfer timeout of half a
#: second is nonsense for an image measured in gigabytes, while half a second is
#: a perfectly reasonable command timeout. Keeping the bounds next to each other
#: is also what lets the settings dialog use the same numbers rather than
#: restating them - the two drifting apart is how a dialog comes to accept a
#: value the validator then silently changes.
NUMBER_BOUNDS: dict[str, tuple[float, float]] = {
    "default_timeout": (0.5, 3600.0),
    "transfer_timeout": (1.0, 7200.0),
    "connect_timeout": (0.5, 300.0),
    "max_retry_attempts": (1, 50),
    "retry_initial_delay_ms": (0, 60_000),
    "retry_max_delay_ms": (0, 300_000),
    "retry_total_budget_s": (0, 86_400),
    "log_max_bytes": (0, 1024 * 1024 * 1024),
    "speed_limit": (0, 1024 * 1024 * 1024),
}

#: Fields whose value is a whole number rather than a real one.
WHOLE_FIELDS = frozenset(
    {
        "max_retry_attempts",
        "retry_initial_delay_ms",
        "retry_max_delay_ms",
        "retry_total_budget_s",
        "log_max_bytes",
        "speed_limit",
    }
)

#: Fields holding free text.
TEXT_FIELDS = frozenset({"log_file", "last_firmware_directory", "adb_path", "fastboot_path"})


@dataclass
class Settings:
    """Everything the operator can change, with the shipped defaults.

    The defaults are deliberately conservative on anything that writes to a
    device and generous on anything that only reads.
    """

    # --- timeouts -----------------------------------------------------------
    #: Seconds to wait for a device to answer a command.
    default_timeout: float = 30.0
    #: Seconds to wait for a bulk transfer that moves data. A 4 GB image over a
    #: marginal hub can legitimately take fifteen minutes, so this is separate
    #: from the command timeout rather than derived from it.
    transfer_timeout: float = 180.0
    #: Seconds to wait when opening the device or scanning the bus.
    connect_timeout: float = 10.0

    # --- retries ------------------------------------------------------------
    #: Total attempts including the first. 1 disables retrying.
    max_retry_attempts: int = 3
    #: The wait before the second attempt, doubled each time after.
    retry_initial_delay_ms: int = 250
    #: The ceiling on that backoff.
    retry_max_delay_ms: int = 5000
    #: Give up on an operation entirely after this many seconds.
    retry_total_budget_s: int = 60

    # --- logging ------------------------------------------------------------
    #: debug, info, warning, error or critical.
    log_level: str = "info"
    #: Where the log is written. Empty means the default beside the executable.
    log_file: str = ""
    #: How large the log may grow before it rotates to a single .1 backup.
    log_max_bytes: int = 10 * 1024 * 1024
    #: Mirror log lines into the UI console. Turning this off does not stop the
    #: file, and is what an operator running a batch of identical devices wants.
    log_to_console: bool = True
    #: Write the log file at all.
    log_to_file: bool = True
    #: Follow the console to the bottom as lines arrive.
    auto_scroll_log: bool = True
    #: Keep one copy of the log per session, beside the live one.
    #:
    #: The live log is appended to and rotated, so after a week it is a mixture
    #: of every session - which is what you want when chasing something that has
    #: been happening all along, and exactly what you do not want when handing
    #: one device's history to somebody. This is the second thing.
    save_logs_automatically: bool = True

    # --- behaviour ----------------------------------------------------------
    #: Ask before anything that erases or writes a partition.
    confirm_destructive_operations: bool = True
    #: Read manufacturer/product/serial strings during a scan. Turning this off
    #: makes a scan much faster, because each string needs the device opened, but
    #: the device list then shows VID:PID alone.
    read_usb_strings: bool = True
    #: Show root hubs in the device list. Only useful when diagnosing why a
    #: device is not appearing.
    show_root_hubs: bool = False
    #: Ask for a scan as soon as the application starts.
    scan_on_startup: bool = True
    #: Bytes per second cap on flashing, 0 for none.
    speed_limit: int = 0

    # --- interface ----------------------------------------------------------
    #: Whether the interface animates. Off is not a cosmetic preference: a
    #: machine driven over a remote session redraws every animation frame across
    #: the wire, and an operator who finds motion distracting should not have to
    #: live with it. Honoured by every helper in huaxin.ui.animations.
    animations: bool = True

    # --- external tools -----------------------------------------------------
    #: Path to adb. Empty means search PATH, the usual SDK locations and the copy
    #: shipped with the tool. Set when a machine has several versions installed
    #: and the one on PATH is not the one the operator means.
    adb_path: str = ""
    #: Path to fastboot. Empty means the same search as adb.
    fastboot_path: str = ""

    #: Which colour theme to start in. Validated against the theme list on the
    #: way in, so a hand-edited file cannot name a theme that does not exist.
    theme: str = "midnight"

    # -----------------------------------------------------------------------
    #  Validation
    # -----------------------------------------------------------------------

    def sanitised(self) -> "Settings":
        """Returns a copy with every out-of-range value replaced by its default.

        Called after loading, so a hand-edited file cannot put a negative
        timeout into a USB transfer. Every correction is reported through
        `problems()` rather than applied silently.
        """
        fixed = Settings()

        for item in fields(Settings):
            value = getattr(self, item.name)
            default = getattr(fixed, item.name)

            if item.name in NUMBER_BOUNDS:
                low, high = NUMBER_BOUNDS[item.name]
                if item.name in WHOLE_FIELDS:
                    value = _whole(value, default, minimum=int(low), maximum=int(high))
                else:
                    value = _number(value, default, minimum=low, maximum=high)
            elif item.name == "log_level":
                text = str(value).strip().lower()
                value = text if text in LOG_LEVELS else default
            elif item.name == "theme":
                text = str(value).strip().lower()
                # Imported here rather than at module scope: config.py is
                # deliberately free of Qt, and ui.tokens imports Qt indirectly.
                from huaxin.ui.tokens import THEMES

                value = text if text in THEMES else default
            elif item.name in TEXT_FIELDS:
                value = str(value) if isinstance(value, str) else default
            elif item.name == "recent_packages":
                value = [str(entry) for entry in value] if isinstance(value, list) else []
                # Bounded: the list is a convenience, not a history.
                value = value[:10]
            elif isinstance(default, bool):
                # bool() would accept the string "false" as True, so a value that
                # is not already a boolean keeps the default instead.
                value = value if isinstance(value, bool) else default
            else:  # pragma: no cover - a new field without a rule lands here
                value = default

            setattr(fixed, item.name, value)

        # A ceiling below the initial delay would make the backoff meaningless.
        if fixed.retry_max_delay_ms < fixed.retry_initial_delay_ms:
            fixed.retry_max_delay_ms = fixed.retry_initial_delay_ms
        return fixed

    def problems(self) -> list[str]:
        """Human-readable list of what `sanitised()` would change."""
        fixed = self.sanitised()
        notes: list[str] = []
        for item in fields(Settings):
            before = getattr(self, item.name)
            after = getattr(fixed, item.name)
            if before != after:
                notes.append(f"{item.name}: {before!r} -> {after!r}")
        return notes

    # -----------------------------------------------------------------------
    #  Bridging to the native backend
    # -----------------------------------------------------------------------

    def apply_to_logger(self, logger: Any) -> None:
        """Pushes the logging settings into the native Logger.

        Called on the worker thread that owns the backend, because the Logger is
        process-wide and reading these from Python on the UI thread would be a
        second source of truth.
        """
        if logger is None:
            return
        logger.set_min_level(self.log_level)
        logger.console_enabled = bool(self.log_to_console)
        if self.log_max_bytes > 0:
            logger.max_file_bytes = self.log_max_bytes
        if self.log_to_file:
            target = self.log_file or logger.default_path()
            logger.open(target)
        else:
            logger.file_enabled = False

    def retry_policy(self) -> Any:
        """The native RetryPolicy these settings describe. None if unavailable."""
        try:
            import huaxin_core
        except ImportError:  # pragma: no cover - only when the build is missing
            return None
        policy = huaxin_core.RetryPolicy()
        policy.attempts = self.max_retry_attempts
        policy.initial_delay_ms = self.retry_initial_delay_ms
        policy.maximum_delay_ms = self.retry_max_delay_ms
        policy.total_budget_ms = self.retry_total_budget_s * 1000
        return policy

    # -----------------------------------------------------------------------
    #  Persistence
    # -----------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Settings":
        """Builds a Settings from a decoded dict, ignoring unknown keys.

        Unknown keys are ignored rather than rejected, so a file written by a
        newer build still loads in an older one - it just loses the settings the
        older build does not know about.
        """
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in payload.items() if key in known})


#: How many per-session log copies to keep. A busy shop runs dozens of sessions
#: a day and nobody prunes a folder called "logs" by hand, so the oldest go.
SESSION_LOG_KEEP = 10

#: Where the copies go, beside the log itself.
SESSION_LOG_DIR = "sessions"


def archive_session_log(log_path: Path) -> Path | None:
    """Copies a finished session log aside, returning where it went.

    The live log is appended to and rotated, so after a few sessions it is a
    mixture of all of them - which is what you want when diagnosing something
    that has been happening all week, and exactly what you do not want when you
    are trying to hand one device's history to somebody. This keeps one file per
    session alongside it.

    Returns None when there is nothing to copy, which is the normal case for a
    session that produced no log at all.
    """
    source = Path(log_path)
    try:
        if not source.is_file() or source.stat().st_size == 0:
            return None
    except OSError:
        return None

    folder = source.parent / SESSION_LOG_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = folder / f"{source.stem}-{stamp}{source.suffix or '.txt'}"

    # A same-second collision is possible when a session is very short; a
    # counter keeps the second one from overwriting the first.
    counter = 1
    while target.exists():
        target = folder / f"{source.stem}-{stamp}-{counter}{source.suffix or '.txt'}"
        counter += 1

    shutil.copy2(source, target)
    _prune_session_logs(folder)
    return target


def _prune_session_logs(folder: Path, keep: int = SESSION_LOG_KEEP) -> None:
    """Keeps the newest `keep` session logs, deleting older ones."""
    try:
        logs = sorted(
            (p for p in folder.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in logs[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def _number(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return min(max(number, minimum), maximum)


def _whole(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, minimum), maximum)


def settings_path() -> Path:
    """Where settings are read from and written to."""
    return SETTINGS_PATH


def template_path() -> Path:
    """Where a deployment template is looked for.

    Beside the executable, which for a packaged build is the install folder -
    the place an administrator would put one.
    """
    return Path(sys.executable).resolve().parent / "settings.template.json"


def default_log_path() -> Path:
    """Where the log goes when the settings do not name a file.

    Asked of the native backend rather than guessed, because the backend is what
    actually opens the file and the two must not disagree about where it is. The
    fallback is the working directory, which is where the backend puts it when it
    cannot determine anything better.
    """
    try:
        import huaxin_core

        return Path(huaxin_core.Logger.default_path())
    except Exception:  # noqa: BLE001 - a missing build must not break the dialog
        return Path.cwd() / "flash_log.txt"


def load_settings(path: Path | None = None) -> tuple[Settings, list[str]]:
    """Reads the settings file.

    Returns the settings and a list of notes about anything that had to be
    corrected or could not be read. It never raises: a tool that will not start
    because its settings file is malformed is a tool that cannot be used to fix
    the problem the settings file caused.

    When the user has no settings file yet, a deployment template beside the
    executable is used as the starting point. A user's own saved settings always
    win over the template - it is a starting point, never an override.
    """
    target = path or SETTINGS_PATH
    notes: list[str] = []

    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        # The normal first-run case. Not a problem worth reporting - but a
        # template, if the deployment put one there, is worth reporting, because
        # an operator should know why their defaults are not the shipped ones.
        template = _read_template()
        if template is not None:
            notes.append(f"settings: started from the deployment template at {template[1]}")
            return template[0], notes
        return Settings(), notes
    except OSError as exc:
        notes.append(f"settings could not be read from {target} ({exc}); using defaults")
        return Settings(), notes

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        notes.append(f"settings file {target} is not valid JSON ({exc}); using defaults")
        return Settings(), notes

    if not isinstance(payload, dict):
        notes.append(f"settings file {target} does not hold an object; using defaults")
        return Settings(), notes

    loaded = Settings.from_dict(payload)
    notes.extend(f"settings: {note}" for note in loaded.problems())
    return loaded.sanitised(), notes


def _read_template() -> tuple[Settings, Path] | None:
    """Reads the deployment template, or None when there is not one.

    A malformed template is ignored rather than reported as a settings problem:
    it is not the operator's file and they cannot fix it, and the tool still has
    to start.
    """
    candidate = template_path()
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return Settings.from_dict(payload).sanitised(), candidate


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """Writes the settings file, creating its directory if needed.

    Written to a temporary file and then moved into place, so a crash or a full
    disk during the write cannot leave a half-written file that the next start
    would have to reject.
    """
    target = path or SETTINGS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(settings.sanitised().to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # os.replace is atomic on both Windows and POSIX, unlike Path.rename.
    os.replace(temporary, target)
    return target


def reset_settings(path: Path | None = None) -> Settings:
    """Deletes the settings file and returns the defaults."""
    target = path or SETTINGS_PATH
    try:
        target.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # Leaving a file we cannot delete is better than refusing to reset.
        pass
    return Settings()
