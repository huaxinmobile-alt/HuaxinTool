"""Verify the VID/PID -> vendor/mode mapping.

This is the part of Phase 3 that can be tested without a phone on the desk: the
catalogue is queried directly rather than through a scan, so the test proves the
mapping itself rather than whatever happens to be plugged into this machine.

The `verified` flag is checked as deliberately as the names are. An entry that
this project has not confirmed against real hardware must say so, because the UI
renders that distinction and an operator will act on it.

    python tests/test_device_catalog.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

import huaxin_core  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(description: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((bool(passed), description, detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {description}" + (f"  ({detail})" if detail else ""), flush=True)


def expect(vid: int, pid: int, *, kind: str, verified: bool, mode_contains: str = "") -> None:
    """Assert one catalogue entry maps to the expected backend and confidence."""
    target = huaxin_core.lookup_target(vid, pid)
    label = f"{vid:04x}:{pid:04x}"
    if target is None:
        check(f"{label} is a known target", False, "lookup returned None")
        return
    check(f"{label} -> {kind}", target.kind.name == kind, target.kind.name)
    check(f"{label} vendor/mode describe the target", bool(target.vendor and target.mode),
          f"{target.vendor} / {target.mode}")
    check(f"{label} verification flag is {verified}", target.verified is verified,
          f"got {target.verified}")
    if mode_contains:
        check(f"{label} mode mentions '{mode_contains}'", mode_contains.lower() in target.mode.lower(),
              target.mode)
    check(f"{label} names the phase that implements it", target.phase.startswith("Phase"), target.phase)


def main() -> int:
    print(f"catalogue holds {huaxin_core.known_target_count()} entries")
    print(f"libusb linked: {huaxin_core.libusb_version()}\n")

    print("1. documented, hardware-confirmed pairings")
    expect(0x05C6, 0x9008, kind="QualcommEdl", verified=True, mode_contains="EDL")
    expect(0x0E8D, 0x0003, kind="MediaTekBootRom", verified=True, mode_contains="Boot ROM")
    expect(0x04E8, 0x685D, kind="SamsungDownload", verified=True, mode_contains="Download")
    expect(0x18D1, 0x4EE7, kind="AdbInterface", verified=True, mode_contains="ADB")
    expect(0x18D1, 0x4EE0, kind="FastbootInterface", verified=True, mode_contains="Fastboot")

    print("\n2. entries this project has NOT confirmed against hardware")
    # These must be flagged. If someone later confirms them, they flip to True
    # and this test starts failing on purpose - that is the signal to update it.
    expect(0x0E8D, 0x2000, kind="MediaTekPreloader", verified=False)
    expect(0x0E8D, 0x2001, kind="MediaTekPreloader", verified=False)
    expect(0x1782, 0x4D00, kind="UnisocResearchDownload", verified=False)
    expect(0x05C6, 0x900E, kind="QualcommEdl", verified=False)
    expect(0x04E8, 0x6860, kind="SamsungDownload", verified=False)

    print("\n3. the BROM/preloader distinction")
    brom = huaxin_core.lookup_target(0x0E8D, 0x0003)
    preloader = huaxin_core.lookup_target(0x0E8D, 0x2000)
    check("BROM and preloader are different backends",
          brom is not None and preloader is not None and brom.kind != preloader.kind,
          f"{brom.kind.name if brom else None} vs {preloader.kind.name if preloader else None}")

    print("\n4. unknown devices")
    check("an unlisted VID:PID returns None", huaxin_core.lookup_target(0x1234, 0x5678) is None)
    check("a known VID with an unknown PID returns None",
          huaxin_core.lookup_target(0x05C6, 0xFFFF) is None)
    check("that VID still resolves to its vendor",
          huaxin_core.vendor_for_vid(0x05C6) == "Qualcomm", str(huaxin_core.vendor_for_vid(0x05C6)))
    check("an unknown VID has no vendor", huaxin_core.vendor_for_vid(0x1234) is None)

    print("\n5. catalogue integrity")
    seen: set[tuple[int, int]] = set()
    duplicates = []
    for vid, pid in (
        (0x05C6, 0x9008), (0x0E8D, 0x0003), (0x0E8D, 0x2000), (0x04E8, 0x685D), (0x18D1, 0x4EE7),
    ):
        key = (vid, pid)
        if key in seen:
            duplicates.append(key)
        seen.add(key)
    check("no duplicate VID:PID entries", not duplicates, str(duplicates))
    check("every entry resolves to a distinct kind", len({
        huaxin_core.lookup_target(*key).kind.name for key in seen
    }) == len(seen))

    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("\nFailures:")
        for _, description, detail in failed:
            print(f"  - {description} {detail}")
        return 1
    print("Device catalogue OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
