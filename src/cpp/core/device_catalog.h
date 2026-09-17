#pragma once

#include <cstddef>
#include <cstdint>

#include "core/device_info.h"

namespace huaxin::core {

/// A VID:PID pairing this tool knows how to talk to.
struct KnownTarget {
    std::uint16_t vid;
    std::uint16_t pid;
    TargetKind kind;
    const char* vendor;
    const char* mode;
    const char* phase;

    /// False means the pairing is plausible but has NOT been confirmed against
    /// real hardware. The UI shows those devices as "(unverified)" rather than
    /// presenting a guess as a fact.
    bool verified;
};

/// Looks up an exact VID:PID pairing. Returns nullptr when unknown.
const KnownTarget* find_target(std::uint16_t vid, std::uint16_t pid) noexcept;

/// Vendor name for a VID alone, or nullptr. Used for devices whose PID is not
/// one of the modes we implement - we can still say who made them.
const char* vendor_for_vid(std::uint16_t vid) noexcept;

/// Number of entries in the target table, for diagnostics.
std::size_t known_target_count() noexcept;

}  // namespace huaxin::core
