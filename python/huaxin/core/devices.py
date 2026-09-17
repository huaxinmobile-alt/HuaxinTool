"""The device database: what each known target is, and what tends to go wrong.

The VID/PID catalogue itself lives in C++ (`core/device_catalog.cpp`) and is
deliberately single-sourced - the same table drives enumeration, the UI and
protocol routing, and a second copy here would drift out of step with it. What
this module adds is everything that is *not* a fact about the USB bus:

* which aliases and marketing names a model is known by, so a search box finds
  it,
* the flash settings that work for it, so an operator does not have to know that
  a given MediaTek chip needs a particular DA,
* and the known issues, each with the workaround that actually helps.

HOW THE KNOWN ISSUES ARE WRITTEN. Every entry states the symptom as the operator
sees it, then the cause, then the workaround. An entry that cannot name a
workaround says so rather than suggesting something that might brick the device.
Nothing here is a guess dressed as a fact: where a claim is unverified it says
`verified=False` and the UI shows it as unconfirmed.

PROVENANCE. `verified` means this project has seen it on real hardware. False
means it is reported in the wild or derived from a specification but has not
been confirmed here. That distinction is carried into the UI, because an
operator acting on an unverified workaround during a flash deserves to know that
is what they are doing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

__all__ = [
    "KnownIssue",
    "DeviceProfile",
    "FLASH_SETTINGS_KEYS",
    "DEVICE_PROFILES",
    "profiles_for_vendor",
    "profile_for",
    "known_issues_for",
    "search",
    "vendor_summary",
]


@dataclass(frozen=True)
class KnownIssue:
    """Something that goes wrong with a model, and what to do about it."""

    #: What the operator sees. Written as they would describe it.
    symptom: str
    #: Why it happens, when the cause is known.
    cause: str
    #: What to do. Empty only when nothing safe can be recommended.
    workaround: str
    #: True when this project has reproduced it; False when it is reported.
    verified: bool = False
    #: A page in the docs with more detail, if there is one.
    reference: str = ""

    @property
    def actionable(self) -> bool:
        """True when there is something the operator can actually do."""
        return bool(self.workaround)


@dataclass(frozen=True)
class DeviceProfile:
    """A family of devices, what it needs, and what tends to go wrong."""

    #: Stable identifier used in code, e.g. "qcom-msm8998".
    key: str
    #: What the UI calls it, e.g. "Qualcomm Snapdragon 835 (MSM8998)".
    model: str
    #: Vendor key matching the catalogue: "qualcomm", "mediatek", "unisoc",
    #: "samsung", "google", "xiaomi", ...
    vendor: str
    #: Modes this device can be flashed in, as the catalogue names them.
    modes: tuple[str, ...] = ()
    #: Other names the same thing is sold or referred to as.
    aliases: tuple[str, ...] = ()
    #: Default flash settings for this family. Keys are documented in
    #: FLASH_SETTINGS_KEYS; a setting that does not apply is simply absent, which
    #: is different from being set to a default value.
    flash_settings: dict[str, str] = field(default_factory=dict)
    #: What tends to go wrong.
    issues: tuple[KnownIssue, ...] = ()
    #: Only offered behind the "show unverified targets" toggle in the UI.
    verified: bool = False
    #: Free text shown next to the profile: what it is, and what it is for.
    notes: str = ""

    @property
    def searchable(self) -> str:
        """Everything a search box should match against, lower-cased."""
        parts = [self.key, self.model, self.vendor, *self.aliases, *self.modes]
        return " ".join(parts).lower()


#: The settings a profile may carry, and what each one means. Documented here so
#: the UI can label them and so a typo in a profile is obvious.
FLASH_SETTINGS_KEYS: dict[str, str] = {
    "programmer": "The Firehose programmer (ELF/MBN) this chip needs, as a file name.",
    "storage_type": "ufs or emmc. Auto-detected when the programmer can report it.",
    "sector_size": "Bytes per sector for the rawprogram XML. 512 for UFS and eMMC.",
    "da_file": "The MediaTek Download Agent this chip needs, as a file name.",
    "nand_extension": "Extra words for the MediaTek region parameter on NAND parts.",
    "pac_type": "PAC generation to expect: v1 or v2.",
    "fdl1": "The Unisoc first-stage loader, as a file name.",
    "fdl2": "The Unisoc second-stage loader, as a file name.",
    "pit_file": "A PIT to flash before the archives, when the device needs repartitioning.",
    "bootloader": "Which Samsung bootloader generation this model uses.",
    "erase_before_write": "true when the partition must be erased before it is written.",
}


# -----------------------------------------------------------------------------
#  The database
#
#  Every entry states its provenance. Nothing here was invented to fill a row:
#  the chipset facts are from the vendors' own documentation, and the issues are
#  the ones this project hit or that are documented in the open-source tools. An
#  entry with verified=False is one this project has not confirmed itself, and
#  the UI says so.
# -----------------------------------------------------------------------------

DEVICE_PROFILES: tuple[DeviceProfile, ...] = (
    # -------------------------------------------------------------------------
    #  Qualcomm EDL
    # -------------------------------------------------------------------------
    DeviceProfile(
        key="qcom-generic-edl",
        model="Qualcomm device in EDL (9008)",
        vendor="qualcomm",
        modes=("EDL (Emergency Download)",),
        aliases=("9008", "emergency download", "firehose", "sahara", "qdloader"),
        flash_settings={
            "programmer": "prog_firehose_ddr.elf",
            "storage_type": "auto",
            "sector_size": "512",
            "erase_before_write": "false",
        },
        issues=(
            KnownIssue(
                symptom="The device appears as 9008, the Sahara handshake completes, "
                        "and then the Firehose configure command times out.",
                cause="The programmer does not match the chip, or it was loaded into "
                      "the wrong region. Some programmers only work from a specific "
                      "load address.",
                workaround="Use the programmer that shipped with the firmware package "
                           "for that exact model rather than a generic one. If the "
                           "package's own programmer also fails, the chip is not the "
                           "one the package claims.",
                verified=True,
            ),
            KnownIssue(
                symptom="Reset after flashing leaves the device in EDL again instead "
                        "of booting.",
                cause="A partition the boot chain needs was written with the wrong "
                      "image, or the flash was interrupted before the last partition.",
                workaround="Flash the complete package from the beginning, including "
                           "the boot chain, rather than the single partition that was "
                           "being written when it failed.",
                verified=True,
            ),
            KnownIssue(
                symptom="Reading the GPT returns no partitions, or a table full of "
                        "unreadable names.",
                cause="The GPT is stored as UTF-16LE, and some tools decode it as "
                      "UTF-8 or the other way round.",
                workaround="None needed in this tool: the GPT reader decodes UTF-16LE "
                           "as the specification requires. If names are still wrong, "
                           "the table itself is damaged and the device needs a full "
                           "reflash.",
                verified=True,
            ),
            KnownIssue(
                symptom="Windows shows the device as 'QHSUSB_DLOAD' or "
                        "'Qualcomm HS-USB QDLoader 9008' with a warning triangle.",
                cause="No WinUSB driver is bound to the interface.",
                workaround="Install the Qualcomm driver from drivers/, or bind WinUSB "
                           "with Zadig. See drivers/README.md.",
                verified=True,
                reference="drivers/README.md",
            ),
        ),
        verified=True,
        notes="EDL is the recovery mode every Qualcomm chip exposes. Anything that "
              "reaches it can be written; nothing that reaches it will boot until a "
              "complete package has been flashed.",
    ),
    DeviceProfile(
        key="qcom-sdm660",
        model="Qualcomm Snapdragon 660 (SDM660)",
        vendor="qualcomm",
        modes=("EDL (Emergency Download)",),
        aliases=("sdm660", "msm8976 plus", "660"),
        flash_settings={"programmer": "prog_firehose_ddr.elf", "sector_size": "512"},
        issues=(
            KnownIssue(
                symptom="The Sahara handshake ends with an 'unexpected end of packet' "
                        "error.",
                cause="The device reset out of EDL mid-handshake, which usually means "
                      "the cable or the port cannot hold the link.",
                workaround="Use a rear USB port directly on the motherboard, with the "
                           "original cable. Hubs and front-panel ports are the most "
                           "common cause.",
                verified=False,
            ),
        ),
        verified=False,
    ),
    DeviceProfile(
        key="qcom-msm8998",
        model="Qualcomm Snapdragon 835 (MSM8998)",
        vendor="qualcomm",
        modes=("EDL (Emergency Download)",),
        aliases=("msm8998", "835"),
        flash_settings={"programmer": "prog_firehose_ddr.elf", "sector_size": "512"},
        issues=(),
        verified=False,
    ),
    DeviceProfile(
        key="qcom-sm8250",
        model="Qualcomm Snapdragon 865 (SM8250)",
        vendor="qualcomm",
        modes=("EDL (Emergency Download)",),
        aliases=("sm8250", "865"),
        flash_settings={
            "programmer": "prog_firehose_ddr.elf",
            "storage_type": "ufs",
            "sector_size": "4096",
        },
        issues=(
            KnownIssue(
                symptom="The partition table reads back with the correct partitions but "
                        "the sector size does not match what the firmware expects.",
                cause="UFS logical block size is usually 4096, while eMMC is 512, and "
                      "a rawprogram XML written for one will not fit the other.",
                workaround="Let the tool read the storage geometry first: it asks the "
                           "device for the sector size rather than assuming it.",
                verified=True,
            ),
        ),
        verified=False,
    ),
    # -------------------------------------------------------------------------
    #  MediaTek
    # -------------------------------------------------------------------------
    DeviceProfile(
        key="mtk-brom-generic",
        model="MediaTek device in BROM or Preloader mode",
        vendor="mediatek",
        modes=("BROM", "Preloader"),
        aliases=("mtk", "brom", "preloader", "download agent", "da", "sp flash tool"),
        flash_settings={
            "da_file": "MTK_AllInOne_DA.bin",
            "storage_type": "emmc",
            "erase_before_write": "true",
        },
        issues=(
            KnownIssue(
                symptom="The handshake fails immediately, before any data is sent.",
                cause="BROM is protected by a read-back check the bootrom performs "
                      "before it answers: it echoes a value and expects the bitwise "
                      "complement back. A device that does not echo is in Preloader "
                      "mode rather than BROM, or the port is not the one the bootrom "
                      "uses.",
                workaround="Check which mode the device actually enumerated as, and "
                           "reconnect with the correct button combination. A BROM "
                           "connection uses the bootrom VID/PID; a Preloader connection "
                           "uses a different one.",
                verified=True,
            ),
            KnownIssue(
                symptom="The Download Agent upload starts and then stalls with no error.",
                cause="A secure-boot device rejects a DA that is not signed for it, and "
                      "the bootrom stops answering rather than reporting a refusal.",
                workaround="Use the DA from the device's own firmware package. There is "
                           "no generic DA that works on a secure-boot device, and this "
                           "tool reports the stall as an authentication failure rather "
                           "than retrying it.",
                verified=True,
            ),
            KnownIssue(
                symptom="After a partial flash the device is not detected in either "
                        "mode.",
                cause="The preloader partition was overwritten with an image for a "
                      "different board, so the boot ROM has nothing valid to load.",
                workaround="This is recoverable only through BROM: hold the BROM entry "
                           "combination while connecting, and flash the correct "
                           "preloader for the exact board. Do not retry the same package.",
                verified=True,
            ),
            KnownIssue(
                symptom="Scatter file loads but every partition is marked as not to be "
                        "downloaded.",
                cause="The `is_download` field is false, or absent and defaulted "
                      "wrongly. Modern scatter files state it explicitly; older ones "
                      "rely on the absence meaning 'download'.",
                workaround="None needed here: the parser keeps 'stated' and 'absent' "
                           "distinct rather than treating an absent field as a value. "
                           "If a partition really is marked false in the file, edit the "
                           "scatter rather than the tool.",
                verified=True,
            ),
        ),
        verified=True,
        notes="MediaTek parts have no single recovery mode: BROM is the boot ROM "
              "itself and is always present, while Preloader is a small loader on "
              "flash that can be overwritten. BROM is what makes a bricked MediaTek "
              "device recoverable.",
    ),
    DeviceProfile(
        key="mtk-mt6765",
        model="MediaTek Helio P22 (MT6765)",
        vendor="mediatek",
        modes=("BROM", "Preloader"),
        aliases=("mt6765", "helio p22", "p22"),
        flash_settings={"da_file": "MTK_AllInOne_DA.bin", "storage_type": "emmc"},
        issues=(),
        verified=False,
    ),
    DeviceProfile(
        key="mtk-mt6785",
        model="MediaTek Helio G95 (MT6785)",
        vendor="mediatek",
        modes=("BROM", "Preloader"),
        aliases=("mt6785", "helio g95", "g95"),
        flash_settings={"da_file": "MTK_AllInOne_DA.bin", "storage_type": "ufs"},
        issues=(),
        verified=False,
    ),
    # -------------------------------------------------------------------------
    #  Unisoc
    # -------------------------------------------------------------------------
    DeviceProfile(
        key="unisoc-sc9863",
        model="Unisoc SC9863A",
        vendor="unisoc",
        modes=("Research Download",),
        aliases=("sc9863", "sc9863a", "spreadtrum", "sprd", "research download"),
        flash_settings={
            "pac_type": "v2",
            "fdl1": "fdl1-sign.bin",
            "fdl2": "fdl2-sign.bin",
        },
        issues=(
            KnownIssue(
                symptom="The PAC opens but every entry is listed as zero bytes.",
                cause="The PAC generation was read wrongly. PAC5 and PAC6 differ in how "
                      "the header's size fields are split, and a parser for one reports "
                      "nonsense for the other rather than failing.",
                workaround="None needed here: the size fields are read according to the "
                           "file's own magic rather than assumed, and the entry count is "
                           "checked against the file length before anything is read.",
                verified=True,
            ),
            KnownIssue(
                symptom="The device is not detected in Research Download mode at all.",
                cause="Unisoc devices need the correct key combination held while "
                      "connecting, and on many boards the mode is only entered from a "
                      "powered-off state.",
                workaround="Power the device off completely, hold the volume keys the "
                           "board requires, then connect. If the host has no Unisoc "
                           "driver bound, install it from drivers/ first.",
                verified=False,
                reference="drivers/README.md",
            ),
            KnownIssue(
                symptom="Flashing stops partway through with a checksum error.",
                cause="The BSL packet checksum differs by chip family: some use CRC-16 "
                      "and others a ones-complement sum, and a host that guesses wrong "
                      "is rejected from the first packet on.",
                workaround="None needed here: the checksum is detected from the device's "
                           "own first reply rather than assumed from the chip model.",
                verified=True,
            ),
        ),
        verified=False,
        notes="Research Download is Unisoc's recovery mode. Unlike Qualcomm and "
              "MediaTek, its packet protocol has no vendor-published specification, "
              "so the implementation here is derived from the open-source tools and "
              "the vendor's own header files; see docs/spd-bsl-protocol.md for what "
              "is and is not confirmed.",
    ),
    DeviceProfile(
        key="unisoc-sc7731",
        model="Unisoc SC7731",
        vendor="unisoc",
        modes=("Research Download",),
        aliases=("sc7731", "spreadtrum 7731"),
        flash_settings={"pac_type": "v1"},
        issues=(),
        verified=False,
    ),
    # -------------------------------------------------------------------------
    #  Samsung
    # -------------------------------------------------------------------------
    DeviceProfile(
        key="samsung-odin-generic",
        model="Samsung device in Download (Odin) mode",
        vendor="samsung",
        modes=("Samsung Download (Odin)",),
        aliases=("odin", "download mode", "heimdall", "tar.md5", "pit"),
        flash_settings={
            "storage_type": "emmc",
            "sector_size": "512",
            "erase_before_write": "false",
        },
        issues=(
            KnownIssue(
                symptom="The flash stops on the first file with 'the bootloader sent "
                        "something other than a handshake reply'.",
                cause="The device is in Download mode but the host opened the interface "
                      "with the wrong endpoints, or something else on the machine is "
                      "holding the port - Samsung's own software, most often.",
                workaround="Close any other flashing or phone-management software, "
                           "reconnect, and try again. Only one program can hold the "
                           "Odin interface at a time.",
                verified=True,
            ),
            KnownIssue(
                symptom="An integrity check on a .tar.md5 fails even though the file "
                        "was downloaded from the firmware source.",
                cause="The MD5 is not a sidecar file: it is appended to the archive as "
                      "sixteen raw bytes after the two terminating zero blocks. A tool "
                      "that looks for a separate .md5 file will not find one.",
                workaround="None needed here: the appended digest is read from the end "
                           "of the archive and checked before anything is written.",
                verified=True,
            ),
            KnownIssue(
                symptom="A repartition empties the partition table.",
                cause="The entry count is at offset 4 of the PIT header, immediately "
                      "after the magic, not after the two reserved words that the "
                      "struct declares next. Writing the fields in declaration order "
                      "puts the count where the parser looks for a reserved word, and "
                      "the table parses as empty.",
                workaround="None needed here: this tool writes the count where the "
                           "parser reads it, and a round-trip test covers it. A device "
                           "already repartitioned wrongly needs its PIT reflashed from "
                           "the firmware package.",
                verified=True,
            ),
            KnownIssue(
                symptom="The flash fails near the end of a large file, after most of it "
                        "has been written.",
                cause="The transfer is sent in sequence-sized blocks and each one is "
                      "acknowledged. A host that times out on one acknowledgement "
                      "abandons a device that is mid-write.",
                workaround="Reconnect and flash the whole package again rather than the "
                           "file that failed: the partition it was writing is in an "
                           "unknown state.",
                verified=True,
            ),
            KnownIssue(
                symptom="Flashing an older firmware over a newer one is refused, or the "
                        "device boot-loops afterwards.",
                cause="Samsung's bootloader enforces an anti-rollback counter, and the "
                      "fuse it is checked against cannot be lowered.",
                workaround="Use firmware at least as new as what the device is running. "
                           "There is no safe way to defeat the counter; a device that "
                           "has already tripped it needs a service centre.",
                verified=True,
            ),
        ),
        verified=True,
        notes="Odin mode accepts images without any host-side authentication, which "
              "is why a PIT flash can repartition a device but also why a wrong PIT "
              "can leave it unable to boot. The PIT is the file to be careful with.",
    ),
)


def _by_key() -> dict[str, DeviceProfile]:
    return {profile.key: profile for profile in DEVICE_PROFILES}


def profiles_for_vendor(vendor: str) -> list[DeviceProfile]:
    """Every profile for a vendor key, in the order they are declared."""
    wanted = vendor.strip().lower()
    return [profile for profile in DEVICE_PROFILES if profile.vendor == wanted]


def profile_for(key: str) -> DeviceProfile | None:
    """The profile with this key, or None."""
    return _by_key().get(key.strip().lower())


def known_issues_for(key: str) -> list[KnownIssue]:
    """The issues recorded for a profile, newest declaration order preserved."""
    profile = profile_for(key)
    return list(profile.issues) if profile is not None else []


def search(term: str, *, include_unverified: bool = True) -> list[DeviceProfile]:
    """Profiles matching `term` in their key, model, aliases or modes.

    An empty term returns everything, which is what the UI wants when the search
    box is cleared.
    """
    needle = term.strip().lower()
    results: Iterable[DeviceProfile] = DEVICE_PROFILES
    if not include_unverified:
        results = [profile for profile in results if profile.verified]
    if not needle:
        return list(results)
    return [profile for profile in results if needle in profile.searchable]


def vendor_summary() -> dict[str, int]:
    """How many profiles each vendor has, for a chooser or a test."""
    summary: dict[str, int] = {}
    for profile in DEVICE_PROFILES:
        summary[profile.vendor] = summary.get(profile.vendor, 0) + 1
    return summary
