#include "core/logger.h"

#include <chrono>
#include <cstdio>
#include <ctime>
#include <system_error>
#include <thread>

namespace huaxin::core {

namespace {

/// Local time, down to milliseconds. A flash produces bursts of lines within the
/// same second, and second resolution makes the ordering unreadable.
std::string format_now() {
    const auto now = std::chrono::system_clock::now();
    const std::time_t seconds = std::chrono::system_clock::to_time_t(now);
    const auto millis = std::chrono::duration_cast<std::chrono::milliseconds>(
                            now.time_since_epoch())
                            .count()
                        % 1000;

    std::tm local{};
#if defined(_WIN32)
    localtime_s(&local, &seconds);
#else
    localtime_r(&seconds, &local);
#endif

    char buffer[32];
    std::snprintf(buffer, sizeof(buffer), "%04d-%02d-%02d %02d:%02d:%02d.%03d",
                  local.tm_year + 1900, local.tm_mon + 1, local.tm_mday, local.tm_hour,
                  local.tm_min, local.tm_sec, static_cast<int>(millis));
    return buffer;
}

/// A short tag so interleaved worker and UI lines can be told apart.
std::string thread_tag() {
    const auto id = std::hash<std::thread::id>{}(std::this_thread::get_id());
    char buffer[16];
    std::snprintf(buffer, sizeof(buffer), "t%04x", static_cast<unsigned>(id & 0xFFFF));
    return buffer;
}

/// Size of a file, or 0 when it does not exist or cannot be measured.
std::uintmax_t size_of(const std::string& path) {
    std::error_code error;
    const std::uintmax_t size = std::filesystem::file_size(path, error);
    return error ? 0 : size;
}

}  // namespace

Logger& Logger::instance() {
    static Logger logger;
    return logger;
}

Logger::~Logger() {
    close();
}

std::string Logger::timestamp() {
    return format_now();
}

const char* Logger::level_name(Level level) noexcept {
    switch (level) {
        case Level::Debug:    return "DEBUG";
        case Level::Info:     return "INFO ";
        case Level::Warning:  return "WARN ";
        case Level::Error:    return "ERROR";
        case Level::Critical: return "CRIT ";
    }
    return "INFO ";
}

const char* Logger::colour_code(Level level) noexcept {
    // 90 grey, 33 yellow, 31 red, 1;31 bold red. Info is left alone so the
    // default terminal foreground is used, which is what reads best for the
    // overwhelming majority of lines.
    switch (level) {
        case Level::Debug:    return "\x1b[90m";
        case Level::Info:     return "";
        case Level::Warning:  return "\x1b[33m";
        case Level::Error:    return "\x1b[31m";
        case Level::Critical: return "\x1b[1;31m";
    }
    return "";
}

std::string Logger::colourise(Level level, const std::string& text, bool enabled) {
    if (!enabled) {
        return text;
    }
    const char* code = colour_code(level);
    if (code == nullptr || code[0] == '\0') {
        return text;
    }
    return std::string(code) + text + "\x1b[0m";
}

Logger::Level Logger::parse_level(const std::string& name) {
    if (name == "debug") return Level::Debug;
    if (name == "info") return Level::Info;
    if (name == "warn" || name == "warning") return Level::Warning;
    if (name == "error") return Level::Error;
    if (name == "critical" || name == "crit" || name == "fatal") return Level::Critical;
    return Level::Info;
}

std::string Logger::default_path() {
    // Written next to the executable when that is writable, which is what an
    // operator looking for it after a failure will try first.
    std::error_code error;
    const std::filesystem::path exe_dir =
        std::filesystem::absolute(std::filesystem::path("."), error);
    if (!error) {
        return (exe_dir / "flash_log.txt").string();
    }
    return "flash_log.txt";
}

std::string Logger::rotated_path_for(const std::string& path) {
    // flash_log.txt -> flash_log.1.txt. Keeping the extension means the backup
    // still opens in whatever the operator associates with .txt, and keeping the
    // stem means the two files sort next to each other.
    const std::filesystem::path original(path);
    const std::string stem = original.stem().string();
    const std::string extension = original.extension().string();
    std::filesystem::path rotated = original.parent_path() / (stem + ".1" + extension);
    return rotated.string();
}

void Logger::set_file_enabled(bool enabled) {
    std::lock_guard<std::mutex> lock(m_mutex);
    if (m_file_enabled == enabled) {
        return;
    }
    m_file_enabled = enabled;
    if (!enabled) {
        if (m_stream.is_open()) {
            m_stream.flush();
            m_stream.close();
        }
        m_open_size = 0;
        m_written_since_open = 0;
    } else {
        reopen_locked();
    }
}

bool Logger::file_enabled() const {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_file_enabled;
}

void Logger::set_console_enabled(bool enabled) {
    std::lock_guard<std::mutex> lock(m_mutex);
    m_console_enabled = enabled;
}

bool Logger::console_enabled() const {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_console_enabled;
}

void Logger::set_min_level(Level level) {
    std::lock_guard<std::mutex> lock(m_mutex);
    m_min_level = level;
}

Logger::Level Logger::min_level() const {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_min_level;
}

void Logger::set_max_file_bytes(std::uintmax_t bytes) {
    std::lock_guard<std::mutex> lock(m_mutex);
    m_max_file_bytes = bytes;
}

std::uintmax_t Logger::max_file_bytes() const {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_max_file_bytes;
}

void Logger::write_line_locked(const std::string& line) {
    if (!m_stream.is_open()) {
        return;
    }
    m_stream << line << '\n';
    // Flushed per line on purpose: a crash mid-flash must not lose the last
    // thing that happened, which is the whole point of the file.
    m_stream.flush();
    m_written_since_open += line.size() + 1;
}

bool Logger::over_limit_locked() const {
    return m_max_file_bytes != 0 && m_open_size + m_written_since_open >= m_max_file_bytes;
}

void Logger::reopen_locked() {
    if (m_path.empty() || !m_file_enabled) {
        return;
    }

    // Rotate before opening, so the new file starts empty rather than inheriting
    // the size that tripped the limit.
    if (m_max_file_bytes != 0 && size_of(m_path) >= m_max_file_bytes) {
        std::error_code error;
        const std::string rotated = rotated_path_for(m_path);
        std::filesystem::remove(rotated, error);  // keep exactly one backup
        error.clear();
        std::filesystem::rename(m_path, rotated, error);
        if (error) {
            // Could not rotate - the file is open elsewhere, or the directory is
            // read-only. Truncating is better than growing without bound.
            std::filesystem::remove(m_path, error);
        }
    }

    // Appended, not truncated: a second run after a failure should not erase the
    // evidence of the first.
    m_stream.open(m_path, std::ios::out | std::ios::app);
    if (!m_stream.is_open()) {
        m_stream.clear();
        m_open_size = 0;
        m_written_since_open = 0;
        return;
    }
    // Measured after opening and before writing, so the two counters together
    // are the file's true size at every point.
    m_open_size = size_of(m_path);
    m_written_since_open = 0;
}

bool Logger::open(const std::string& path) {
    std::lock_guard<std::mutex> lock(m_mutex);

    if (m_stream.is_open()) {
        m_stream.flush();
        m_stream.close();
    }
    m_path = path;

    if (!m_file_enabled) {
        return true;  // remembered for when file output is switched back on
    }
    reopen_locked();
    if (!m_stream.is_open()) {
        m_path.clear();
        return false;
    }

    write_line_locked(format_now() + " [" + level_name(Level::Info) + "] [" + thread_tag()
                      + "] --- log opened ---");
    return true;
}

void Logger::close() {
    std::lock_guard<std::mutex> lock(m_mutex);
    if (!m_stream.is_open()) {
        return;
    }
    write_line_locked(format_now() + " [" + level_name(Level::Info) + "] [" + thread_tag()
                      + "] --- log closed ---");
    m_stream.close();
    m_path.clear();
    m_open_size = 0;
    m_written_since_open = 0;
}

bool Logger::is_open() const {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_stream.is_open();
}

std::string Logger::path() const {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_path;
}

void Logger::set_sink(std::function<void(Level, const std::string&)> sink) {
    std::lock_guard<std::mutex> lock(m_mutex);
    m_sink = std::move(sink);
}

void Logger::rotate_if_needed() {
    std::lock_guard<std::mutex> lock(m_mutex);
    if (!m_stream.is_open() || m_max_file_bytes == 0) {
        return;
    }
    if (!over_limit_locked()) {
        return;
    }
    m_stream.flush();
    m_stream.close();
    reopen_locked();
    write_line_locked(format_now() + " [" + level_name(Level::Info) + "] [" + thread_tag()
                      + "] --- log rotated ---");
}

void Logger::log(Level level, const std::string& message) {
    std::string line;
    std::function<void(Level, const std::string&)> sink;
    bool rotate = false;

    {
        std::lock_guard<std::mutex> lock(m_mutex);

        // Filtered here rather than at the call site: a caller should not have to
        // know what the current minimum is. The filter applies to both
        // destinations, because a minimum level is about how much detail the
        // operator wants, not about which medium they want it on.
        if (level < m_min_level) {
            return;
        }

        line = format_now() + " [" + level_name(level) + "] [" + thread_tag() + "] " + message;
        if (m_console_enabled && m_sink) {
            sink = m_sink;
        }

        if (m_stream.is_open()) {
            write_line_locked(line);
            rotate = over_limit_locked();
        } else if (!sink) {
            return;  // nowhere to put it
        }
    }

    if (rotate) {
        // Outside the lock: rotate_if_needed takes it again.
        rotate_if_needed();
    }

    // Outside the lock: a sink that logs would otherwise self-deadlock.
    if (sink) {
        sink(level, line);
    }
}

void Logger::log(Level level, Vendor vendor, const std::string& message) {
    if (vendor == Vendor::Unknown) {
        log(level, message);
        return;
    }
    log(level, std::string("[") + to_string(vendor) + "] " + message);
}

void Logger::log_exception(const FlashException& error) {
    // Severity follows the classification: a bug in this tool is more urgent
    // than a cable the operator can replug, and the log should say so without
    // anyone having to read the message first.
    Level level = Level::Error;
    switch (error.error()) {
        case FlashError::InternalError:
            level = Level::Critical;
            break;
        case FlashError::Cancelled:
            level = Level::Warning;
            break;
        default:
            level = Level::Error;
            break;
    }
    log(level, error.vendor(), error.report());
}

}  // namespace huaxin::core
