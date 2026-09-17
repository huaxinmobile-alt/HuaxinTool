#include "core/device_catalog.h"

#include <array>
#include <cstddef>

namespace huaxin::core {

namespace {

// -----------------------------------------------------------------------------
//  VID/PID catalogue
//
//  `verified` distinguishes two very different things:
//
//    true  - the pairing is documented and reproduced consistently across
//            independent sources and real hardware. Safe to act on.
//    false - the pairing is reported in the wild but has not been confirmed by
//            this project against a real device. The UI labels these so an
//            operator is never told a guess is a fact.
//
//  Every entry that is not verified carries a TODO with what would settle it.
// -----------------------------------------------------------------------------
constexpr std::array<KnownTarget, 10> kTargets{{
    // --- Google / standard Android -------------------------------------------
    // ADB and Fastboot interfaces enumerated on the Google VID. The PIDs are
    // stable across Pixel/Nexus hardware.
    {0x18D1, 0x4EE7, TargetKind::AdbInterface, "Google / Android", "ADB interface", "Phase 4", true},
    {0x18D1, 0x4EE0, TargetKind::FastbootInterface, "Google / Android", "Fastboot interface", "Phase 4", true},

    // --- Qualcomm -------------------------------------------------------------
    // 05c6:9008 is the QDLoader emergency-download identity and is the single
    // most reliably documented entry in this table.
    {0x05C6, 0x9008, TargetKind::QualcommEdl, "Qualcomm", "EDL (Emergency Download)", "Phase 5", true},
    // TODO: confirm 05c6:900e against a real device. It is reported as a second
    // Qualcomm download/loader PID, but this project has not verified it.
    {0x05C6, 0x900E, TargetKind::QualcommEdl, "Qualcomm", "EDL (alternate loader PID)", "Phase 5", false},

    // --- MediaTek -------------------------------------------------------------
    // 0e8d:0003 is the Boot ROM (BROM) identity exposed by the chip itself
    // before any firmware runs, so it is chipset-independent.
    {0x0E8D, 0x0003, TargetKind::MediaTekBootRom, "MediaTek", "Boot ROM (BROM)", "Phase 6", true},
    // TODO: confirm the preloader PIDs against real hardware. These vary by
    // chipset and by which firmware build is on the device; 0x2000 and 0x2001
    // are both commonly reported but neither is confirmed here. Note the
    // preloader runs *after* the BROM, so it must not be labelled BROM.
    {0x0E8D, 0x2000, TargetKind::MediaTekPreloader, "MediaTek", "Preloader", "Phase 6", false},
    {0x0E8D, 0x2001, TargetKind::MediaTekPreloader, "MediaTek", "Preloader (alternate)", "Phase 6", false},

    // --- Unisoc / Spreadtrum --------------------------------------------------
    // TODO: confirm 1782:4d00 against a real device. The 1782 VID is
    // Spreadtrum's; the research-download PID could not be verified here.
    {0x1782, 0x4D00, TargetKind::UnisocResearchDownload, "Unisoc / SPD", "Research Download", "Phase 7", false},

    // --- Samsung --------------------------------------------------------------
    // 04e8:685d is Samsung's long-standing download-mode (Odin) identity.
    {0x04E8, 0x685D, TargetKind::SamsungDownload, "Samsung", "Download mode (Odin)", "Phase 8", true},
    // TODO: Older and some regional Samsung devices are reported to use other
    // download-mode PIDs (0x6860 among them). Not verified - do not trust the
    // mode label on this one until it is checked against hardware.
    {0x04E8, 0x6860, TargetKind::SamsungDownload, "Samsung", "Download mode (Odin, alternate PID)", "Phase 8", false},
}};

/// Vendor IDs, independent of the mode. These are the registered assignments.
struct KnownVendor {
    std::uint16_t vid;
    const char* name;
};

constexpr std::array<KnownVendor, 5> kVendors{{
    {0x18D1, "Google / Android"},
    {0x05C6, "Qualcomm"},
    {0x0E8D, "MediaTek"},
    {0x1782, "Unisoc / SPD"},
    {0x04E8, "Samsung"},
}};

}  // namespace

const char* to_string(TargetKind kind) noexcept {
    switch (kind) {
        case TargetKind::Unknown:                return "unknown";
        case TargetKind::AdbInterface:           return "adb";
        case TargetKind::FastbootInterface:      return "fastboot";
        case TargetKind::QualcommEdl:            return "qualcomm-edl";
        case TargetKind::MediaTekBootRom:        return "mediatek-brom";
        case TargetKind::MediaTekPreloader:      return "mediatek-preloader";
        case TargetKind::UnisocResearchDownload: return "unisoc-research-download";
        case TargetKind::SamsungDownload:        return "samsung-download";
    }
    return "unknown";
}

const KnownTarget* find_target(std::uint16_t vid, std::uint16_t pid) noexcept {
    for (const KnownTarget& target : kTargets) {
        if (target.vid == vid && target.pid == pid) {
            return &target;
        }
    }
    return nullptr;
}

const char* vendor_for_vid(std::uint16_t vid) noexcept {
    for (const KnownVendor& vendor : kVendors) {
        if (vendor.vid == vid) {
            return vendor.name;
        }
    }
    return nullptr;
}

std::size_t known_target_count() noexcept {
    return kTargets.size();
}

}  // namespace huaxin::core
