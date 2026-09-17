#pragma once

// =============================================================================
//  An optional cap on how fast data is sent.
//
//  Why this exists at all, given that faster is better: some links enumerate
//  and then fail under sustained load. A hub, a long extension cable, a virtual
//  machine with a shared USB controller, a device whose port is marginal - they
//  all present the same way, which is a device that works until it is asked to
//  move several gigabytes and then stops answering partway through. Slowing the
//  transfer down is sometimes the difference between a flash that finishes and
//  one that leaves a partition half-written.
//
//  WHERE IT SITS. Every transport already funnels its writes through
//  `write`/`write_all`, so the rate limiter goes there. Throttling at that level
//  rather than in each protocol means one implementation, no protocol has to
//  know about it, and a command frame is throttled alongside the data - which is
//  correct, since the goal is to slow the whole conversation down, not just its
//  bulk half.
//
//  It is off by default. With no limit set, `pace()` returns immediately and
//  costs one relaxed atomic load per write.
// =============================================================================

#include <chrono>
#include <cstddef>
#include <cstdint>

namespace huaxin::core {

/// Sets the cap in bytes per second. Zero means no cap, which is the default.
void set_speed_limit(std::uint64_t bytes_per_second) noexcept;

/// The current cap in bytes per second, or zero.
std::uint64_t speed_limit() noexcept;

/// A per-transfer rate limiter.
///
/// One of these belongs to one transport. It is deliberately not thread-safe:
/// a transport is driven by the single worker thread that opened it, and sharing
/// a limiter between two devices would let one device's traffic pay for the
/// other's.
class RateLimiter {
public:
    /// Starts the clock. Call once, when a transfer begins.
    void begin() noexcept;

    /// Sleeps if the bytes sent so far have run ahead of the cap.
    ///
    /// Called after each write with the number of bytes that write moved. The
    /// sleep is computed against the running total, not per write, so the
    /// average rate converges on the cap rather than on however the writes
    /// happened to be sized.
    void pace(std::size_t bytes_just_written) noexcept;

    /// Bytes moved since begin().
    std::uint64_t transferred() const noexcept { return m_bytes; }

private:
    std::chrono::steady_clock::time_point m_start{};
    std::uint64_t m_bytes{0};
    bool m_started{false};
};

}  // namespace huaxin::core
