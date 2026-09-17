#include "protocols/mediatek/mediatek_flash.h"

#include <chrono>
#include <cstdio>
#include <string>
#include <utility>

namespace huaxin::protocols::mediatek {

std::string format_bytes(std::uint64_t count) {
    char buffer[48];
    const double value = static_cast<double>(count);
    if (value >= 1024.0 * 1024.0 * 1024.0) {
        std::snprintf(buffer, sizeof(buffer), "%.2f GiB", value / (1024.0 * 1024.0 * 1024.0));
    } else if (value >= 1024.0 * 1024.0) {
        std::snprintf(buffer, sizeof(buffer), "%.1f MiB", value / (1024.0 * 1024.0));
    } else if (value >= 1024.0) {
        std::snprintf(buffer, sizeof(buffer), "%.1f KiB", value / 1024.0);
    } else {
        std::snprintf(buffer, sizeof(buffer), "%llu B", static_cast<unsigned long long>(count));
    }
    return buffer;
}

std::string FlashInfo::storage_label() const {
    return have_storage ? to_string(storage) : std::string("unknown storage");
}

std::string FlashInfo::describe() const {
    std::string text;
    if (!da_version.empty()) {
        text += "DA " + da_version + ", ";
    }
    if (!connection_agent.empty()) {
        text += "connected over " + connection_agent + ", ";
    }
    text += storage_label();
    if (total_size != 0) {
        text += " " + format_bytes(total_size);
    }
    if (block_size != 0) {
        text += ", " + std::to_string(block_size) + "-byte blocks";
    }
    if (sla_enabled) {
        text += ", SLA enabled";
    }
    return text;
}

std::size_t FlashResult::written_count() const {
    std::size_t count = 0;
    for (const PartitionOutcome& outcome : partitions) {
        if (outcome.succeeded) {
            ++count;
        }
    }
    return count;
}

std::size_t FlashResult::failed_count() const {
    std::size_t count = 0;
    for (const PartitionOutcome& outcome : partitions) {
        if (!outcome.succeeded && !outcome.skipped) {
            ++count;
        }
    }
    return count;
}

std::uint64_t FlashResult::total_bytes() const {
    std::uint64_t total = 0;
    for (const PartitionOutcome& outcome : partitions) {
        if (outcome.succeeded) {
            total += outcome.bytes;
        }
    }
    return total;
}

std::vector<std::string> FlashResult::written_names() const {
    std::vector<std::string> names;
    for (const PartitionOutcome& outcome : partitions) {
        if (outcome.succeeded) {
            names.push_back(outcome.name);
        }
    }
    return names;
}

std::string FlashResult::summary() const {
    std::string text = std::to_string(written_count()) + " written";
    if (failed_count() != 0) {
        text += ", " + std::to_string(failed_count()) + " failed";
    }
    if (total_bytes() != 0) {
        text += ", " + format_bytes(total_bytes());
    }
    text += completed ? " — the run finished" : " — the run was stopped early";
    return text;
}

/// Emits progress at a rate the UI can keep up with, and computes the transfer
/// speed from the wall clock.
///
/// A write reports per packet, which on a large image is hundreds of events a
/// second; the UI would spend all its time redrawing. 100 ms is the same
/// interval the Python side uses, chosen so a progress bar looks continuous.
ProgressPump::ProgressPump(FlashProgressCallback callback, std::string phase,
                           std::string partition, std::uint64_t total, std::size_t index,
                           std::size_t count)
    : m_callback(std::move(callback)),
      m_phase(std::move(phase)),
      m_partition(std::move(partition)),
      m_total(total),
      m_index(index),
      m_count(count),
      m_start(std::chrono::steady_clock::now()),
      m_last(m_start) {}

void ProgressPump::update(std::uint64_t done, bool force) {
    if (!m_callback) {
        return;
    }
    const auto now = std::chrono::steady_clock::now();
    const bool complete = m_total != 0 && done >= m_total;
    if (!force && !complete
        && std::chrono::duration_cast<std::chrono::milliseconds>(now - m_last).count() < 100) {
        return;
    }
    m_last = now;

    FlashProgress event;
    event.phase = m_phase;
    event.partition = m_partition;
    event.done = done;
    event.total = m_total;
    event.percent = m_total == 0 ? -1 : static_cast<int>((done * 100) / m_total);
    event.index = m_index;
    event.count = m_count;
    const double seconds = std::chrono::duration<double>(now - m_start).count();
    // Below a quarter of a second the average is dominated by the first packet's
    // latency and reads as a wildly wrong number.
    event.bytes_per_second = seconds > 0.25 ? static_cast<double>(done) / seconds : 0.0;
    m_callback(event);
}

}  // namespace huaxin::protocols::mediatek
