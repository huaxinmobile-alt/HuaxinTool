#pragma once

// =============================================================================
//  Centralised log.
//
//  The UI already shows everything live, but a flash that ends badly is exactly
//  the moment the operator closes the window - so the record has to survive the
//  process. Every line is timestamped and written to a file, and optionally
//  mirrored to a sink so the same text can appear in the UI console without the
//  UI having to poll.
//
//  Thread safety: one mutex around the stream and the sink. This is called from
//  the worker thread during a flash and from the UI thread for its own messages,
//  so it has to be safe from both. The sink is invoked *outside* the lock,
//  because a sink that calls back into logging would otherwise deadlock on a
//  non-recursive mutex.
//
//  Rotation: a flash produces thousands of lines and an operator who hits the
//  same failure twice wants the first run's log as well as the second's. Past
//  10 MB the live file is renamed to a single `.1` backup and a fresh one
//  started, so there is always a current log and one previous, and neither grows
//  without bound. The limit is checked against a running byte count rather than
//  a stat, so a file never overshoots by more than the one line that crossed it.
// =============================================================================

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <functional>
#include <mutex>
#include <string>

#include "core/flash_error.h"  // Vendor, FlashException

namespace huaxin::core {

class Logger {
public:
    enum class Level : std::uint8_t {
        Debug = 0,
        Info,
        Warning,
        Error,
        Critical,
    };

    /// Process-wide instance.
    static Logger& instance();

    /// Opens (or creates) the log file at `path`, appending if it exists.
    ///
    /// Returns false and leaves the logger closed when the file cannot be
    /// opened: a tool that refuses to run because it cannot write a log would be
    /// worse than one that runs without one.
    bool open(const std::string& path);

    /// Flushes and closes. Safe to call when already closed.
    void close();

    bool is_open() const;

    /// The file currently being written, or empty.
    std::string path() const;

    /// Optional second destination, e.g. the UI console. Called with the level
    /// and the already-formatted line, without the lock held.
    void set_sink(std::function<void(Level, const std::string&)> sink);

    /// Writes one entry. Does nothing when the logger is closed, so callers
    /// never have to check first.
    void log(Level level, const std::string& message);

    /// Writes one entry tagged with the vendor whose protocol was speaking.
    void log(Level level, Vendor vendor, const std::string& message);

    /// Writes a classified failure: its report goes out at the level its
    /// severity deserves, so an internal error lands in the log as CRITICAL and
    /// a cable problem as ERROR.
    void log_exception(const FlashException& error);

    void debug(const std::string& message) { log(Level::Debug, message); }
    void info(const std::string& message) { log(Level::Info, message); }
    void warn(const std::string& message) { log(Level::Warning, message); }
    void error(const std::string& message) { log(Level::Error, message); }
    void critical(const std::string& message) { log(Level::Critical, message); }

    // --- Destination switches ------------------------------------------------
    // Both default to on. The settings dialog exposes them because an operator
    // running a batch of identical devices does not want a log file per device,
    // and one debugging a driver problem does not want the console noise.

    /// Turn file output on or off without forgetting the path.
    void set_file_enabled(bool enabled);
    bool file_enabled() const;

    /// Turn the sink on or off without forgetting it.
    void set_console_enabled(bool enabled);
    bool console_enabled() const;

    /// Drop everything below `level`. A minimum of Warning on a busy flash cuts
    /// the per-packet chatter that would otherwise bury the one line that
    /// matters.
    void set_min_level(Level level);
    Level min_level() const;

    /// Size past which the log rotates, in bytes. 0 disables rotation.
    void set_max_file_bytes(std::uintmax_t bytes);
    std::uintmax_t max_file_bytes() const;

    /// Rotates now if the file has grown past the limit. Called automatically;
    /// exposed so a test can drive it without writing 10 MB.
    void rotate_if_needed();

    // --- Formatting ---------------------------------------------------------

    /// "2026-09-17 10:04:22.481" in local time.
    static std::string timestamp();

    /// "DEBUG" / "INFO " / "WARN " / "ERROR" / "CRIT".
    static const char* level_name(Level level) noexcept;

    /// Parses the level names the Python side uses ("debug", "warn", ...).
    /// Unknown names become Info rather than throwing.
    static Level parse_level(const std::string& name);

    /// Wraps `text` in an ANSI colour for a terminal, or returns it unchanged
    /// when `enabled` is false. The UI console does its own colouring from the
    /// level it is handed, so this is for the case where the log is being read
    /// on a terminal rather than through Qt.
    static std::string colourise(Level level, const std::string& text, bool enabled);

    /// The ANSI sequence for a level, or empty when colours are off.
    static const char* colour_code(Level level) noexcept;

    /// Default location: alongside the executable, or the working directory.
    static std::string default_path();

    /// Where the previous log is put when the current one rotates.
    static std::string rotated_path_for(const std::string& path);

    // Public so pybind11 can reason about the type; the singleton is still
    // enforced by the private constructor and the deleted copy operations.
    ~Logger();

private:
    Logger() = default;
    Logger(const Logger&) = delete;
    Logger& operator=(const Logger&) = delete;

    /// Reopens the file, rotating first when it is already past the limit.
    /// Expects the lock to be held.
    void reopen_locked();

    /// Writes one already-composed line and accounts for its size.
    /// Expects the lock to be held.
    void write_line_locked(const std::string& line);

    /// True when the file has reached the rotation limit.
    /// Expects the lock to be held.
    bool over_limit_locked() const;

    mutable std::mutex m_mutex;
    std::ofstream m_stream;
    std::function<void(Level, const std::string&)> m_sink;
    std::string m_path;

    bool m_file_enabled{true};
    bool m_console_enabled{true};
    Level m_min_level{Level::Debug};
    std::uintmax_t m_max_file_bytes{10u * 1024u * 1024u};

    /// The file's size when it was opened, and how much has been appended since.
    ///
    /// Tracked rather than measured because measuring is a syscall per check and
    /// because a sampled size lets the file overshoot by however much is written
    /// between two samples - which, for a multi-line exception report, can be
    /// kilobytes. The sum of the two is exact and free.
    std::uintmax_t m_open_size{0};
    std::uintmax_t m_written_since_open{0};
};

}  // namespace huaxin::core
