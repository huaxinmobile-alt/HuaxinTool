#include "core/flash_progress.h"

#include <cctype>
#include <cmath>
#include <cstdio>

namespace huaxin::core {

namespace {

/// Below this many bytes, or below a quarter of a second, an average speed is
/// dominated by the latency of the first transfer and reads as a wildly wrong
/// number. Both thresholds have to be cleared before a speed is reported.
constexpr double kMinimumSecondsForSpeed = 0.25;
constexpr std::uint64_t kMinimumBytesForSpeed = 64 * 1024;

}  // namespace

const char* to_string(OperationType type) noexcept {
    switch (type) {
        case OperationType::Unknown:    return "unknown";
        case OperationType::Preparing:  return "preparing";
        case OperationType::Connecting: return "connecting";
        case OperationType::Uploading:  return "uploading";
        case OperationType::Reading:    return "reading";
        case OperationType::Writing:    return "writing";
        case OperationType::Erasing:    return "erasing";
        case OperationType::Verifying:  return "verifying";
        case OperationType::Resetting:  return "resetting";
    }
    return "unknown";
}

OperationType parse_operation_type(const std::string& name) {
    if (name == "preparing" || name == "prepare") return OperationType::Preparing;
    if (name == "connecting" || name == "connect") return OperationType::Connecting;
    if (name == "uploading" || name == "upload") return OperationType::Uploading;
    if (name == "reading" || name == "read") return OperationType::Reading;
    if (name == "writing" || name == "write" || name == "flashing") return OperationType::Writing;
    if (name == "erasing" || name == "erase") return OperationType::Erasing;
    if (name == "verifying" || name == "verify") return OperationType::Verifying;
    if (name == "resetting" || name == "reset" || name == "rebooting")
        return OperationType::Resetting;
    return OperationType::Unknown;
}

std::string format_bytes(std::uint64_t bytes) {
    // Binary units labelled the way people say them. A firmware image of
    // 4,294,967,296 bytes is "4.0 GB" to everyone who has to type its size.
    static const char* const units[] = {"B", "KB", "MB", "GB", "TB"};
    double value = static_cast<double>(bytes);
    std::size_t unit = 0;
    while (value >= 1024.0 && unit + 1 < sizeof(units) / sizeof(units[0])) {
        value /= 1024.0;
        ++unit;
    }

    char buffer[48];
    if (unit == 0) {
        std::snprintf(buffer, sizeof(buffer), "%llu B", static_cast<unsigned long long>(bytes));
    } else if (value < 10.0) {
        std::snprintf(buffer, sizeof(buffer), "%.2f %s", value, units[unit]);
    } else {
        std::snprintf(buffer, sizeof(buffer), "%.1f %s", value, units[unit]);
    }
    return buffer;
}

std::string format_duration(double seconds) {
    if (seconds < 0.0) {
        return "unknown";
    }
    const unsigned long long whole = static_cast<unsigned long long>(seconds + 0.5);
    char buffer[48];
    if (whole < 60) {
        std::snprintf(buffer, sizeof(buffer), "%llus", whole);
    } else if (whole < 3600) {
        std::snprintf(buffer, sizeof(buffer), "%llum %02llus", whole / 60, whole % 60);
    } else {
        std::snprintf(buffer, sizeof(buffer), "%lluh %02llum", whole / 3600, (whole % 3600) / 60);
    }
    return buffer;
}

std::string FlashProgress::describe() const {
    std::string text = huaxin::core::to_string(operation_type);
    if (text == "unknown") {
        text = "working";
    }
    // Capitalise the first letter: these strings go straight into a status bar.
    text[0] = static_cast<char>(std::toupper(static_cast<unsigned char>(text[0])));

    if (!current_partition.empty()) {
        text += " ";
        text += current_partition;
        if (current_partition_index != 0 && total_partitions != 0) {
            text += " (" + std::to_string(current_partition_index) + "/"
                    + std::to_string(total_partitions) + ")";
        }
    } else if (current_partition_index != 0 && total_partitions != 0) {
        text += " " + std::to_string(current_partition_index) + "/"
                + std::to_string(total_partitions);
    }

    if (has_percentage()) {
        char percent[32];
        std::snprintf(percent, sizeof(percent), "  -  %.1f%%", percentage);
        text += percent;
    }

    if (total_bytes != 0) {
        text += "  -  " + format_bytes(bytes_written) + " / " + format_bytes(total_bytes);
    } else if (bytes_written != 0) {
        text += "  -  " + format_bytes(bytes_written);
    }

    if (speed_mbps > 0.0) {
        char speed[48];
        std::snprintf(speed, sizeof(speed), "  -  %.1f MB/s", speed_mbps);
        text += speed;
    }

    if (has_eta()) {
        text += "  -  " + format_duration(eta_seconds) + " left";
    }

    if (!status_message.empty()) {
        text += "  -  " + status_message;
    }
    return text;
}

// -----------------------------------------------------------------------------
//  Tracker
// -----------------------------------------------------------------------------

FlashProgressTracker::FlashProgressTracker(OperationType type, Vendor vendor,
                                           std::string partition, std::uint64_t total_bytes,
                                           std::size_t index, std::size_t count)
    : m_type(type),
      m_vendor(vendor),
      m_partition(std::move(partition)),
      m_total(total_bytes),
      m_index(index),
      m_count(count),
      m_start(std::chrono::steady_clock::now()),
      m_last_emit(m_start) {}

bool FlashProgressTracker::should_emit(std::uint64_t bytes) const {
    if (m_force) {
        return true;
    }
    // The first event establishes the baseline, and the operator should see
    // something the moment the operation starts rather than a second later.
    if (!m_emitted_anything) {
        return true;
    }
    // The last event, always. A progress bar that stops one tick short looks
    // like a hang, and this is the one event a UI must never miss.
    if (m_total != 0 && bytes >= m_total) {
        return true;
    }

    const auto now = std::chrono::steady_clock::now();
    if (std::chrono::duration_cast<std::chrono::milliseconds>(now - m_last_emit).count()
        >= static_cast<long long>(m_interval_ms)) {
        return true;
    }

    if (m_total == 0) {
        return false;  // no percentage to compare, so only the timer can fire
    }
    const double percent = 100.0 * static_cast<double>(bytes) / static_cast<double>(m_total);
    return std::fabs(percent - m_last_percent) >= m_percent_step;
}

FlashProgress FlashProgressTracker::build(std::uint64_t bytes, const std::string& status) {
    const auto now = std::chrono::steady_clock::now();
    const double seconds = std::chrono::duration<double>(now - m_start).count();

    FlashProgress event;
    event.operation_type = m_type;
    event.vendor = m_vendor;
    event.current_partition = m_partition;
    event.current_partition_index = m_index;
    event.total_partitions = m_count;
    event.bytes_written = bytes;
    event.total_bytes = m_total;
    event.status_message = status;
    event.running = m_total == 0 || bytes < m_total;

    if (m_total != 0) {
        event.percentage = 100.0 * static_cast<double>(bytes) / static_cast<double>(m_total);
        if (event.percentage > 100.0) {
            event.percentage = 100.0;
        }
    } else {
        event.percentage = -1.0;
    }

    if (seconds >= kMinimumSecondsForSpeed && bytes >= kMinimumBytesForSpeed) {
        const double bytes_per_second = static_cast<double>(bytes) / seconds;
        event.speed_mbps = bytes_per_second / (1024.0 * 1024.0);

        if (m_total > bytes && event.speed_mbps > 0.0) {
            event.eta_seconds = static_cast<double>(m_total - bytes) / bytes_per_second;
        } else if (m_total != 0 && bytes >= m_total) {
            event.eta_seconds = 0.0;
        }
    }

    m_last_emit = now;
    m_last_percent = event.percentage;
    m_emitted_anything = true;
    m_force = false;
    m_last = event;
    return event;
}

void FlashProgressTracker::flush() {
    m_force = true;
}

}  // namespace huaxin::core
