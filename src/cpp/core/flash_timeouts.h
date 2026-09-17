#pragma once

// =============================================================================
//  Configurable timeouts.
//
//  Every protocol has its own idea of how long to wait: the MediaTek bootrom
//  answers in about a millisecond and a 30-second wait on a dead link is 30
//  seconds wasted, while a Firehose write of a 4 GB image legitimately takes
//  minutes. Those defaults are baked into each layer as constants, and they are
//  right for most hardware.
//
//  What they cannot express is a host that needs different ones: a virtual
//  machine that is slow to enumerate, a hub that adds latency, a bench with a
//  device whose link is marginal. So the settings offer three numbers, and this
//  is where they land.
//
//  THE SHAPE MATTERS. The override is not a replacement for the per-protocol
//  constants; it is a *fallback-aware* lookup:
//
//      read_exact(buffer, length, core::command_timeout_ms(1000));
//
//  With no override set, `command_timeout_ms(1000)` returns 1000 - so the
//  shipped behaviour is bit-for-bit what it was before this existed, and the
//  verified protocol tests keep testing the same thing. Set an override and
//  every default argument in every protocol picks it up, without a single call
//  site changing.
//
//  Doing it the other way round - one global number replacing all constants -
//  would have silently raised the MediaTek bootrom's 1-second wait to whatever
//  the command timeout is, which makes a dead cable hang for thirty seconds
//  instead of one. That is a worse tool, not a more configurable one.
// =============================================================================

#include <cstdint>

namespace huaxin::core {

/// Which of the three numbers an override applies to.
enum class TimeoutKind : std::uint8_t {
    /// Waiting for a device to answer a command. The fallback is the caller's
    /// own constant, because the right value differs by protocol.
    Command = 0,
    /// Waiting for a bulk transfer that is moving data.
    Transfer,
    /// Opening the device, or waiting for a link to come up.
    Connect,
};

/// The timeout to use, given the caller's own default.
///
/// Returns `fallback` when no override has been set - which is the normal case,
/// and the reason this is a function rather than a variable.
unsigned int timeout_ms(TimeoutKind kind, unsigned int fallback) noexcept;

/// Convenience wrappers, so a call site reads as what it means.
inline unsigned int command_timeout_ms(unsigned int fallback = 5000) noexcept {
    return timeout_ms(TimeoutKind::Command, fallback);
}
inline unsigned int transfer_timeout_ms(unsigned int fallback = 120000) noexcept {
    return timeout_ms(TimeoutKind::Transfer, fallback);
}
inline unsigned int connect_timeout_ms(unsigned int fallback = 10000) noexcept {
    return timeout_ms(TimeoutKind::Connect, fallback);
}

/// Sets the overrides, in milliseconds. Zero means "no override" for that kind,
/// which restores the per-protocol constants.
///
/// Applied from the settings when they change. Thread-safe: a settings change
/// arrives on one thread while a worker thread may be mid-transfer.
void set_timeout_overrides(unsigned int command_ms, unsigned int transfer_ms,
                           unsigned int connect_ms) noexcept;

/// The current overrides, zero where none is set.
void timeout_overrides(unsigned int& command_ms, unsigned int& transfer_ms,
                       unsigned int& connect_ms) noexcept;

}  // namespace huaxin::core
