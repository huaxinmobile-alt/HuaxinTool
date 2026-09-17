#pragma once

// =============================================================================
//  The value types a MediaTek flash run produces.
//
//  Kept out of mediatek_brom.h on purpose: the facade that owns the USB link
//  cannot be linked without libusb, and these are plain data. Splitting them out
//  is what lets the native tests check the reporting and geometry parsing with
//  no hardware and no libusb in the link.
// =============================================================================

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include "protocols/mediatek/brom.h"
#include "protocols/mediatek/da.h"

namespace huaxin::protocols::mediatek {

/// Bytes as text, for logs and the UI. Shared so a size reads the same
/// everywhere it is shown.
std::string format_bytes(std::uint64_t count);

/// What the running agent reports about the flash it is driving.
struct FlashInfo {
    /// The agent's own name for the storage, filled in only when one of the
    /// geometry queries answered.
    DaStorage storage{DaStorage::Emmc};
    bool have_storage{false};

    std::uint64_t total_size{0};
    std::uint64_t block_size{0};

    EmmcInfo emmc;
    NandInfo nand;
    NorInfo nor;
    RamInfo ram;

    /// From GET_DA_VERSION. Empty when the agent does not implement it.
    std::string da_version;
    /// "brom" or "preloader", from GET_CONNECTION_AGENT.
    std::string connection_agent;
    /// The agent's own statement that SLA is enabled on this device.
    bool sla_enabled{false};

    /// What the bootrom said, kept alongside so one object describes the whole
    /// device rather than only the second half of the conversation.
    TargetConfig target_config;

    /// Human-readable storage label for the UI.
    std::string storage_label() const;
    /// One-line summary for the log.
    std::string describe() const;
};

/// One partition's fate in a flash run.
struct PartitionOutcome {
    std::string name;
    std::string file_name;
    std::uint64_t bytes{0};
    bool skipped{false};
    bool succeeded{false};
    /// Empty on success. On failure, the reason as reported by the agent.
    std::string error;
};

/// The result of a whole flash run.
struct FlashResult {
    std::vector<PartitionOutcome> partitions;
    bool completed{false};

    std::size_t written_count() const;
    std::size_t failed_count() const;
    std::uint64_t total_bytes() const;
    /// Partitions that were written. After a failure this is what says how far
    /// the run got, which is the first thing anyone asks.
    std::vector<std::string> written_names() const;
    std::string summary() const;
};

/// Progress for one flash operation, richer than the percent-only callback the
/// other protocols use: the UI shows which partition, how far, and how fast.
struct FlashProgress {
    /// "writing", "reading", "erasing" or "preparing".
    std::string phase;
    /// The partition being worked on. Empty between partitions.
    std::string partition;
    std::uint64_t done{0};
    std::uint64_t total{0};
    int percent{0};
    /// Bytes per second over the operation so far; 0 until enough time has
    /// passed for the average to mean anything.
    double bytes_per_second{0.0};
    /// Which partition of how many in the run, 1-based. 0 outside a run.
    std::size_t index{0};
    std::size_t count{0};
};

using FlashProgressCallback = std::function<void(const FlashProgress&)>;

/// Emits progress at a rate the UI can keep up with, and computes the transfer
/// speed from the wall clock.
///
/// A write reports per packet, which on a large image is hundreds of events a
/// second; the UI would spend all its time redrawing. 100 ms is the same
/// interval the Python side uses, chosen so a progress bar looks continuous.
class ProgressPump {
public:
    ProgressPump(FlashProgressCallback callback, std::string phase, std::string partition,
                 std::uint64_t total, std::size_t index, std::size_t count);

    /// Reports `done` bytes. `force` bypasses the interval, for the first and
    /// last event, which are the two a UI must not miss.
    void update(std::uint64_t done, bool force = false);

private:
    FlashProgressCallback m_callback;
    std::string m_phase;
    std::string m_partition;
    std::uint64_t m_total{0};
    std::size_t m_index{0};
    std::size_t m_count{0};
    std::chrono::steady_clock::time_point m_start;
    std::chrono::steady_clock::time_point m_last;
};

}  // namespace huaxin::protocols::mediatek
