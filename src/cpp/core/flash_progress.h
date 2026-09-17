#pragma once

// =============================================================================
//  The one progress type everything reports through.
//
//  Each vendor layer already emits its own progress event, and they are shaped
//  by how their protocol is paced: a Firehose write reports per packet, an Odin
//  write per 1 MiB block, an erase reports nothing at all until it is done. A
//  progress bar that has to understand four event shapes is a progress bar that
//  will get one of them wrong.
//
//  So the UI sees exactly this struct, and each vendor layer fills it in. The
//  fields are the ones a person reads off the screen: which step, which
//  partition, how much of it, how fast, and how long left.
//
//  SPEED AND ETA. Both are derived from the wall clock against the *bytes of the
//  current operation*, and both are suppressed until enough time has passed for
//  the number to mean anything - the first packet of a transfer arrives in
//  microseconds, and dividing by that gives a figure in the gigabytes per second
//  which is worse than showing nothing.
//
//  THROTTLING. The task asks for an update every 1% or every second, whichever
//  comes first. A write of a 4 GB image in 1 MiB blocks is 4000 events; the UI
//  cannot usefully redraw a progress bar 4000 times, and the traffic across the
//  thread boundary is not free. `FlashProgressTracker` applies that rule in one
//  place so no vendor layer has to.
// =============================================================================

#include <chrono>
#include <cstdint>
#include <string>

#include "core/flash_error.h"  // Vendor

namespace huaxin::core {

/// What the tool is doing, in the operator's words rather than the protocol's.
enum class OperationType : std::uint8_t {
    Unknown = 0,
    Preparing,
    Connecting,
    Uploading,
    Reading,
    Writing,
    Erasing,
    Verifying,
    Resetting,
};

const char* to_string(OperationType type) noexcept;
/// Parses the operation names the UI and the config file use.
OperationType parse_operation_type(const std::string& name);

/// One progress report. Everything the UI needs, and nothing it has to compute.
struct FlashProgress {
    /// Which step this is.
    OperationType operation_type{OperationType::Unknown};
    /// The partition being worked on. Empty between partitions, and for
    /// operations that are not about one partition.
    std::string current_partition;
    /// How many partitions the run covers. 0 when the run is not partition-based.
    std::size_t total_partitions{0};
    /// Which of them, 1-based. 0 outside a run.
    std::size_t current_partition_index{0};
    /// Bytes completed for the current operation.
    std::uint64_t bytes_written{0};
    /// Bytes the current operation covers. 0 when the total is unknown, which is
    /// the honest value for an erase: the chip decides how long it takes.
    std::uint64_t total_bytes{0};
    /// 0-100, or -1 when the total is unknown. Deliberately not clamped to 0:
    /// "we do not know" and "nothing done" are different states and a progress
    /// bar has to render them differently.
    double percentage{-1.0};
    /// Megabytes per second over this operation so far. 0 until the average is
    /// worth reporting.
    double speed_mbps{0.0};
    /// Seconds remaining, or -1 when it cannot be estimated.
    double eta_seconds{-1.0};
    /// A short human sentence for the status line.
    std::string status_message;

    /// Which vendor's protocol is running, so the UI can colour or filter.
    Vendor vendor{Vendor::Unknown};
    /// True when the tool is still working; the UI uses it to know when to stop
    /// asking for repaints.
    bool running{true};

    /// True when `percentage` is a real number rather than "unknown".
    bool has_percentage() const noexcept { return percentage >= 0.0; }
    /// True when an estimate is available.
    bool has_eta() const noexcept { return eta_seconds >= 0.0; }

    /// "1.4 GB / 4.0 GB  -  31.2 MB/s  -  1m 22s left" or as much of it as is
    /// known. Never empty.
    std::string describe() const;
};

/// Formats a byte count the way a person reads it: "512 MB", "1.4 GB".
std::string format_bytes(std::uint64_t bytes);
/// Formats a duration as "1m 22s", "45s" or "1h 04m".
std::string format_duration(double seconds);

/// Computes speed and ETA, and applies the update rule.
///
/// One tracker per operation. It is deliberately not thread-safe: a progress
/// callback runs on the worker thread that owns the flash, and sharing a tracker
/// between two flashers would interleave two different operations into one
/// nonsensical speed.
class FlashProgressTracker {
public:
    /// `total_bytes` may be 0, meaning unknown.
    FlashProgressTracker(OperationType type, Vendor vendor, std::string partition,
                         std::uint64_t total_bytes, std::size_t index = 0,
                         std::size_t count = 0);

    /// True when this event should be sent on.
    ///
    /// Sends the first event and the last one unconditionally - a UI that misses
    /// the final event leaves a progress bar one tick short forever - and
    /// otherwise sends when the percentage has moved by at least `percent_step`
    /// or `interval_ms` has passed since the last send.
    bool should_emit(std::uint64_t bytes) const;

    /// Builds the progress report for `bytes`. Also updates the running clock,
    /// so call it only when the event is actually going out.
    FlashProgress build(std::uint64_t bytes, const std::string& status = "");

    /// Forces the next event through regardless of the rules, for the end of an
    /// operation.
    void flush();

    /// Percent change that triggers an update. Default 1.
    void set_percent_step(double step) { m_percent_step = step; }
    /// Time that triggers an update. Default 1000 ms.
    void set_interval_ms(unsigned int millis) { m_interval_ms = millis; }

    const FlashProgress& last() const noexcept { return m_last; }

private:
    OperationType m_type;
    Vendor m_vendor;
    std::string m_partition;
    std::uint64_t m_total{0};
    std::size_t m_index{0};
    std::size_t m_count{0};

    std::chrono::steady_clock::time_point m_start;
    std::chrono::steady_clock::time_point m_last_emit;
    double m_last_percent{-1.0};
    bool m_emitted_anything{false};
    bool m_force{false};

    double m_percent_step{1.0};
    unsigned int m_interval_ms{1000};

    FlashProgress m_last;
};

}  // namespace huaxin::core
