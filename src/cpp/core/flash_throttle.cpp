#include "core/flash_throttle.h"

#include <atomic>
#include <thread>

namespace huaxin::core {

namespace {

std::atomic<std::uint64_t> g_speed_limit{0};

/// Never sleep longer than this in one go.
///
/// A single `pace()` call should not be able to park the worker thread for
/// seconds - that would stall the progress reporting that tells the operator
/// something is happening, and make cancellation unresponsive. Sleeping in
/// bounded steps lets the loop keep breathing.
constexpr auto kMaxSleep = std::chrono::milliseconds(50);

}  // namespace

void set_speed_limit(std::uint64_t bytes_per_second) noexcept {
    g_speed_limit.store(bytes_per_second, std::memory_order_relaxed);
}

std::uint64_t speed_limit() noexcept {
    return g_speed_limit.load(std::memory_order_relaxed);
}

void RateLimiter::begin() noexcept {
    m_start = std::chrono::steady_clock::now();
    m_bytes = 0;
    m_started = true;
}

void RateLimiter::pace(std::size_t bytes_just_written) noexcept {
    // Counted whether or not a cap is set. `transferred()` says what it means -
    // bytes moved since begin() - and an accessor whose value silently depends on
    // a setting somewhere else is the kind of thing that costs an afternoon. The
    // add sits next to a libusb bulk transfer, which is where it disappears.
    m_bytes += bytes_just_written;

    const std::uint64_t limit = g_speed_limit.load(std::memory_order_relaxed);
    if (limit == 0) {
        return;  // the common case costs one relaxed load and one add
    }

    if (!m_started) {
        // Start the clock here rather than losing the bytes that triggered it.
        // A transfer is usually begun lazily, by its first write.
        m_start = std::chrono::steady_clock::now();
        m_started = true;
    }

    // How long the bytes sent so far *should* have taken at the cap.
    const auto expected = std::chrono::nanoseconds(
        static_cast<std::uint64_t>(
            (static_cast<double>(m_bytes) / static_cast<double>(limit)) * 1e9));
    const auto elapsed = std::chrono::steady_clock::now() - m_start;

    if (elapsed >= expected) {
        return;  // running at or below the cap
    }

    auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(expected - elapsed);
    while (remaining > std::chrono::milliseconds::zero()) {
        const auto step = remaining > kMaxSleep ? kMaxSleep : remaining;
        std::this_thread::sleep_for(step);
        remaining -= step;
    }
}

}  // namespace huaxin::core
