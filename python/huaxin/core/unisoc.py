"""Unisoc / Spreadtrum Research Download - protocol done, UI not wired.

What this module is: a device check, and an honest status message. What it is
not: a flasher. Nothing here touches a device beyond enumerating USB.

WHERE IT STANDS
---------------
The protocol layer is implemented and tested in C++ (`protocols/spd/`): the .pac
container in both layouts, the four checksum algorithms against their published
check values, the HDLC/BSL framing, the command and response tables, checksum
detection for both chip families, and a three-chunk payload download - 122 native
checks against a scripted device, in `tests/cpp/test_spd.cpp`.

What is missing is the wiring: `protocols/spd/` is not exposed through pybind11,
so this module has nothing to call and the tab's buttons say so. That is a
packaging gap, not an unknown.

This module originally reported a *blocker* rather than a gap, and the history is
worth keeping because the reasoning was wrong in an instructive way. The claim
was that no open-source implementation existed to transcribe. A second search
with different terms found four: the vendor's own `BinPack.h` in
`Mani-Sadhasivam/unisoc-dloader`, three independent clients that agree with it
byte for byte, and a complete command enum in `uwpflash`. "No implementation
exists" is a claim about a search, not about the world.

WHAT IS STILL UNKNOWN
---------------------
Listed in full in `docs/spd-status.md`; in short: the fields inside
`READ_FLASH_INFO`'s reply, the baud-rate renegotiation sequence, NV CRC refresh on
the write path, and the PDL pre-stage the RDA8910 family uses to reach BSL.

And the caveat that applies to every vendor in this tool: none of it has been run
against real hardware. The tests prove the encoder, not that a particular phone
accepts the result.
"""


from __future__ import annotations

from typing import Any

__all__ = [
    "BLOCKED_REASON",
    "PENDING_REASON",
    "RESEARCH_DOWNLOAD_USB_ID",
    "UNISOC_VID",
    "devices_present",
    "verify_toolchain",
]

UNISOC_VID = 0x1782
#: The PID is in the catalogue but marked unverified; it is repeated here so the
#: UI can look for it without importing the C++ catalogue.
RESEARCH_DOWNLOAD_USB_ID = "1782:4d00"

#: The .pac container and the Research Download framing are now implemented and
#: verified in C++ (protocols/spd/), against the vendor's own BinPack header and
#: three independent open-source clients. What is still missing is this module's
#: wrapper and the tab's actions: the protocol exists and is tested, the UI is not
#: wired to it yet. That is what this message says, because "blocked" is no
#: longer true and "implemented" would be.
PENDING_REASON = (
    "Unisoc Research Download is implemented and tested in the C++ layer "
    "(protocols/spd/: the .pac parser and the BSL protocol, with 122 native checks "
    "against a scripted device), but the Python wrapper and the buttons in this tab "
    "are not wired to it yet. See docs/spd-status.md for exactly what is done and "
    "what remains. Nothing here touches a device."
)

#: Kept so an existing import does not break; the name is now historical.
BLOCKED_REASON = PENDING_REASON


def _native() -> Any:
    import sys
    from pathlib import Path

    if str(Path(__file__).resolve().parents[2]) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import huaxin_core  # noqa: PLC0415

    return huaxin_core


def devices_present() -> list[str]:
    """Unisoc USB IDs currently on the bus.

    This much is real: it enumerates USB and looks for the vendor ID. It says
    nothing about whether the device is in Research Download mode, because that
    would need the protocol this module cannot yet speak.
    """
    native = _native()
    bridge = native.HardwareBridge()
    try:
        if not bridge.init():
            return []
        return [
            f"{info.vid:04x}:{info.pid:04x}"
            for info in bridge.get_device_list(False)
            if info.vid == UNISOC_VID
        ]
    finally:
        bridge.shutdown()


def verify_toolchain(ctx: Any) -> None:
    """Report the state and whether anything with the Unisoc VID is attached."""
    found = devices_present()
    if found:
        ctx.log(f"found Unisoc device(s): {', '.join(found)}", "ok")
    else:
        ctx.log("no device with the Unisoc vendor ID (1782) is attached", "info")

    ctx.log(PENDING_REASON, "warn")
