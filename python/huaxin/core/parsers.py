"""Reading firmware packages, without touching a device.

Three formats, one job each: what is in this file, and is it intact. All three
come from the native parsers, which are covered by the test suites.

    read_pac(path)      a Unisoc .pac - header, entry table, loaders
    read_pit(path)      a Samsung .pit - the partition table
    read_package(path)  a Samsung .tar.md5 - its members and their digest

WHY THIS MODULE EXISTS rather than the panels importing the extension directly.
The panels are UI code; giving them the native module means every one of them can
reach any native function, and the next person to add a button has to decide for
themselves whether bypassing the worker thread is acceptable. Here, the answer is
structural: these are plain functions with no Qt in them, so they are safe to run
on the worker thread and cannot touch a widget.

THE TWO FILE SIZES. A PAC is hundreds of megabytes to a few gigabytes, and a
Samsung package is two to six. `read_pac` reads only the header and the entry
table; `read_package` streams the archive's headers and skips the member data.
Neither holds a package in memory, which is the difference between a listing that
costs a few kilobytes and one that costs more RAM than the machine has.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

__all__ = [
    "PACKAGE_SUFFIXES",
    "available",
    "read_pac",
    "read_pit",
    "read_package",
    "parse_pit_bytes",
    "package_summary",
]

PYTHON_ROOT = Path(__file__).resolve().parents[2]
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

#: What a firmware package is called. `.tar.md5` is a tar with the digest
#: appended as raw bytes, and `.tar` is the same file without one - a package
#: assembled by hand rather than by the vendor's own tooling.
PACKAGE_SUFFIXES = (".tar.md5", ".tar")

_native: Any = None
_import_error: str | None = None


def _module() -> Any:
    """The native module, or None when it could not be imported.

    A missing build is reported by the caller as "not available" rather than
    raising here: this module is imported by the UI, and a UI that will not load
    because the extension is missing is a worse failure than a tab that says so.
    """
    global _native, _import_error
    if _native is None and _import_error is None:
        try:
            import huaxin_core

            _native = huaxin_core
        except ImportError as exc:  # pragma: no cover - only without a build
            _import_error = str(exc)
    return _native


def available() -> bool:
    """True when the native parsers can be used."""
    return _module() is not None


def read_pac(path: str | Path) -> Any:
    """Reads a Unisoc package's header and entry table.

    Raises RuntimeError when the build has no parsers, and the native exception
    types otherwise - a damaged package is a ProtocolError, which the error layer
    classifies as a file error.
    """
    native = _module()
    if native is None:
        raise RuntimeError(f"the native parsers are unavailable ({_import_error})")
    # The header-only reader, deliberately: a listing needs the first few
    # kilobytes of a file that may be gigabytes, and `load_pac` would read all
    # of it. The payload CRC is left unchecked as a result, and the view says so.
    return native.read_pac_header(str(path))


def parse_pit_bytes(data: bytes) -> Any:
    """Parses a PIT from bytes. Small enough that the whole file is fine."""
    native = _module()
    if native is None:
        raise RuntimeError(f"the native parsers are unavailable ({_import_error})")
    return native.parse_pit(data)


def read_pit(path: str | Path) -> Any:
    """Parses a Samsung PIT file.

    A PIT is a few kilobytes - 132 bytes per partition - so reading it whole is
    the right thing here, unlike a firmware package.
    """
    target = Path(path)
    return parse_pit_bytes(target.read_bytes())


def read_package(path: str | Path) -> Any:
    """Lists a Samsung package's members by streaming its headers.

    Streamed rather than read: a package is two to six gigabytes and a listing
    needs the member names, not the images. The appended .md5 digest is skipped
    rather than parsed - it is raw binary, and reading it as a tar header reports
    a good package as corrupt.
    """
    native = _module()
    if native is None:
        raise RuntimeError(f"the native parsers are unavailable ({_import_error})")
    return native.list_tar_file(str(path))


def package_summary(path: str | Path, archive: Any = None) -> dict[str, Any]:
    """A few facts about a package file, for a caption.

    Returns what is known from the filesystem alone when the archive could not be
    read, so a caller can still show the operator which file failed.
    """
    target = Path(path)
    try:
        size = target.stat().st_size
    except OSError:
        size = 0

    summary: dict[str, Any] = {
        "name": target.name,
        "path": str(target),
        "size": size,
        "members": 0,
        "payload": 0,
    }
    if archive is not None:
        summary["members"] = len(archive.files())
        summary["payload"] = archive.total_file_bytes
    return summary
