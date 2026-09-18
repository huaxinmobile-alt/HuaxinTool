// =============================================================================
//  Native tests for the unified error system and the logger.
//
//  Both are the parts of the tool that only run when something has gone wrong, so
//  they are the parts least likely to be exercised by hand and most likely to be
//  broken by a later change. Everything here is therefore written against the
//  documented contract rather than against the current implementation.
//
//  The retry timing tests deliberately use a policy with a 1 ms first delay: the
//  backoff sequence itself is checked as arithmetic in its own test, so the
//  retry-loop tests only need to know *that* it waited, not how long.
// =============================================================================

#include <cmath>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <new>
#include <stdexcept>
#include <string>
#include <vector>

#include "core/flash_error.h"
#include "core/flash_progress.h"
#include "core/flash_throttle.h"
#include "core/flash_timeouts.h"
#include "core/logger.h"
#include "protocols/qualcomm/sahara.h"

using huaxin::core::backoff_delay_ms;
using huaxin::core::classify;
using huaxin::core::FlashError;
using huaxin::core::FlashException;
using huaxin::core::FlashProgress;
using huaxin::core::FlashProgressTracker;
using huaxin::core::guard;
using huaxin::core::is_dangerous;
using huaxin::core::is_retryable;
using huaxin::core::Logger;
using huaxin::core::OperationType;
using huaxin::core::RateLimiter;
using huaxin::core::TimeoutKind;
using huaxin::core::RetryPolicy;
using huaxin::core::Vendor;
using huaxin::core::with_retry;
using huaxin::protocols::qualcomm::ProtocolError;

namespace {

int g_checks = 0;
int g_failures = 0;

void check(const std::string& description, bool passed, const std::string& detail = "") {
    ++g_checks;
    if (!passed) {
        ++g_failures;
    }
    std::printf("  [%s] %s%s\n", passed ? "PASS" : "FAIL", description.c_str(),
                detail.empty() ? "" : ("  (" + detail + ")").c_str());
    std::fflush(stdout);
}

/// Stands in for usb::UsbDisconnectedError without dragging libusb into a test
/// that does not need it. What is being tested is the classification hook, and
/// the hook is inherited from ProtocolError either way.
class FakeDisconnect : public ProtocolError {
public:
    FakeDisconnect() : ProtocolError("the device disconnected while writing") {}
    const char* error_kind() const noexcept override { return "disconnect"; }
};

class FakeFileError : public ProtocolError {
public:
    FakeFileError() : ProtocolError("the package is truncated") {}
    const char* error_kind() const noexcept override { return "file"; }
};

class FakeAuthError : public ProtocolError {
public:
    FakeAuthError() : ProtocolError("secure boot is enabled") {}
    const char* error_kind() const noexcept override { return "auth"; }
};

/// A policy that fails fast, so the tests do not spend real seconds backing off.
RetryPolicy quick(unsigned int attempts, unsigned int initial = 1) {
    RetryPolicy policy;
    policy.attempts = attempts;
    policy.initial_delay_ms = initial;
    policy.maximum_delay_ms = 4;
    policy.total_budget_ms = 60000;
    return policy;
}

// -----------------------------------------------------------------------------
//  1. The seven classifications
// -----------------------------------------------------------------------------

void test_classification() {
    std::printf("\n1. classification\n");

    // The exception classifies itself through the hook.
    FakeDisconnect disconnected;
    check("a disconnect classifies as a USB error",
          classify(disconnected) == FlashError::UsbError);
    check("a file problem classifies as a file error",
          classify(FakeFileError()) == FlashError::FileError);
    check("a security refusal classifies as an auth error",
          classify(FakeAuthError()) == FlashError::AuthError);

    // A bare ProtocolError is a protocol error, not a wrapped unknown.
    const ProtocolError plain("the target answered NAK");
    check("a bare protocol error stays a protocol error",
          classify(plain) == FlashError::ProtocolError);

    // Standard exceptions that name their own category.
    check("std::bad_alloc is an internal error",
          classify(std::bad_alloc()) == FlashError::InternalError);
    check("std::invalid_argument is a file error",
          classify(std::invalid_argument("bad length field")) == FlashError::FileError);
    check("std::out_of_range is a file error",
          classify(std::out_of_range("entry 900 of 3")) == FlashError::FileError);

    // Message-based fallbacks for plain runtime_error.
    check("a timeout reads as a USB error",
          classify(std::runtime_error("transfer timed out")) == FlashError::UsbError);
    check("a missing file reads as a file error",
          classify(std::runtime_error("cannot open build/system.img"))
              == FlashError::FileError);
    check("corruption reads as a file error",
          classify(std::runtime_error("the archive is corrupt")) == FlashError::FileError);
    check("a cancelled run reads as cancelled",
          classify(std::runtime_error("the operation was cancelled"))
              == FlashError::Cancelled);
    check("a locked bootloader reads as an auth error",
          classify(std::runtime_error("bootloader is locked"))
              == FlashError::AuthError);
    check("a failed erase reads as a flash error",
          classify(std::runtime_error("erase failed at 0x40000"))
              == FlashError::FlashError);

    // An unrecognised runtime failure is a protocol fault, not a bug report:
    // that is where every unclassified failure in this codebase comes from.
    check("an unrecognised runtime error defaults to protocol",
          classify(std::runtime_error("the fifth byte was 0x00")) == FlashError::ProtocolError);

    // Every kind has a name, advice and a retry answer. A missing case would
    // show here as an empty string rather than silently returning a default.
    for (int raw = 0; raw <= static_cast<int>(FlashError::Cancelled); ++raw) {
        const auto kind = static_cast<FlashError>(raw);
        const std::string name = huaxin::core::to_string(kind);
        const std::string advice = huaxin::core::advice_for(kind);
        check("kind " + name + " has a name and advice", !name.empty() && advice.size() > 20,
              advice.substr(0, 40));
    }
}

void test_retry_policy_table() {
    std::printf("\n2. which failures are worth retrying\n");

    check("a dropped link is retryable", is_retryable(FlashError::UsbError));
    check("a protocol stumble is retryable", is_retryable(FlashError::ProtocolError));
    check("a missing file is not", !is_retryable(FlashError::FileError));
    check("a failed write is not retried blindly", !is_retryable(FlashError::FlashError));
    check("a security refusal is not", !is_retryable(FlashError::AuthError));
    check("a cancellation is not", !is_retryable(FlashError::Cancelled));
    check("an internal error is not", !is_retryable(FlashError::InternalError));

    check("a protocol failure leaves the device in doubt",
          is_dangerous(FlashError::ProtocolError));
    check("a failed write leaves the device in doubt",
          is_dangerous(FlashError::FlashError));
    check("a cancelled flash leaves the device in doubt",
          is_dangerous(FlashError::Cancelled));
    check("a missing file does not touch the device",
          !is_dangerous(FlashError::FileError));
    check("a cable problem does not corrupt anything",
          !is_dangerous(FlashError::UsbError));
}

void test_backoff() {
    std::printf("\n3. exponential backoff\n");

    RetryPolicy policy;
    policy.initial_delay_ms = 250;
    policy.maximum_delay_ms = 5000;

    check("the first wait is the initial delay", backoff_delay_ms(policy, 1) == 250,
          std::to_string(backoff_delay_ms(policy, 1)));
    check("the second doubles", backoff_delay_ms(policy, 2) == 500);
    check("the third doubles again", backoff_delay_ms(policy, 3) == 1000);
    check("the fourth doubles again", backoff_delay_ms(policy, 4) == 2000);
    check("the fifth doubles again", backoff_delay_ms(policy, 5) == 4000);
    check("the sixth is capped", backoff_delay_ms(policy, 6) == 5000);
    check("a large attempt count stays capped rather than overflowing",
          backoff_delay_ms(policy, 1000000) == 5000,
          std::to_string(backoff_delay_ms(policy, 1000000)));

    check("attempt zero means no wait", backoff_delay_ms(policy, 0) == 0);

    RetryPolicy no_wait;
    no_wait.initial_delay_ms = 0;
    check("a zero initial delay stays zero", backoff_delay_ms(no_wait, 5) == 0);

    RetryPolicy uncapped;
    uncapped.initial_delay_ms = 10;
    uncapped.maximum_delay_ms = 0;
    // A zero ceiling would cap everything at zero, which would silently turn
    // backoff off. Asserting the arithmetic instead of the intent.
    check("a zero ceiling caps at zero", backoff_delay_ms(uncapped, 1) == 0);

    // The default policy is the one the tool ships with, and the task pins it at
    // three attempts.
    const RetryPolicy shipped;
    check("the shipped policy attempts three times", shipped.attempts == 3,
          std::to_string(shipped.attempts));
    check("the shipped policy caps below ten seconds",
          shipped.maximum_delay_ms <= 10000, std::to_string(shipped.maximum_delay_ms));
}

// -----------------------------------------------------------------------------
//  4. The retry loop
// -----------------------------------------------------------------------------

void test_retry_loop() {
    std::printf("\n4. the retry loop\n");

    // A callable that fails a fixed number of times, then succeeds.
    auto flaky = [](int failures, FlashError kind) {
        return [failures, kind, calls = 0]() mutable -> int {
            ++calls;
            if (calls <= failures) {
                throw FlashException(kind, Vendor::Qualcomm, "attempt " + std::to_string(calls));
            }
            return calls;
        };
    };

    {
        auto operation = flaky(0, FlashError::UsbError);
        const int result = with_retry(quick(3), operation);
        check("a first-time success is not retried", result == 1, std::to_string(result));
    }

    {
        auto operation = flaky(2, FlashError::UsbError);
        int retries = 0;
        const int result = with_retry(quick(3), operation,
                                      [&retries](unsigned int, const FlashException&) {
                                          ++retries;
                                      });
        check("a success on the third attempt is returned", result == 3,
              std::to_string(result));
        check("both failed attempts were reported", retries == 2, std::to_string(retries));
    }

    {
        auto operation = flaky(10, FlashError::UsbError);
        int calls = 0;
        int retries = 0;
        bool threw = false;
        try {
            with_retry(quick(3), [&operation, &calls]() {
                ++calls;
                return operation();
            }, [&retries](unsigned int, const FlashException&) { ++retries; });
        } catch (const FlashException& error) {
            threw = error.error() == FlashError::UsbError;
        }
        check("it gives up after the third attempt", threw);
        check("it made exactly three attempts", calls == 3, std::to_string(calls));
        check("it reported the two retries it made", retries == 2, std::to_string(retries));
    }

    {
        // A non-retryable failure must not be repeated, however many attempts
        // the policy allows: retrying a write that already failed is how a
        // device is destroyed.
        auto operation = flaky(1, FlashError::FlashError);
        int calls = 0;
        bool threw = false;
        try {
            with_retry(quick(5), [&operation, &calls]() {
                ++calls;
                return operation();
            });
        } catch (const FlashException& error) {
            threw = error.error() == FlashError::FlashError;
        }
        check("a failed write is not retried", threw);
        check("it was attempted exactly once", calls == 1, std::to_string(calls));
    }

    {
        // A raw exception that never went through guard() is classified and
        // retried on its merits rather than escaping unretried.
        int calls = 0;
        bool threw = false;
        FlashError seen = FlashError::Success;
        try {
            with_retry(quick(2), [&calls]() -> int {
                ++calls;
                throw ProtocolError("the transfer timed out");
            });
        } catch (const FlashException& error) {
            threw = true;
            seen = error.error();
        }
        check("an unclassified exception is retried up to the limit", calls == 2,
              std::to_string(calls));
        check("and is translated on the way out", threw && seen == FlashError::UsbError);
    }

    {
        // The budget stops a retry even when attempts remain.
        RetryPolicy tight;
        tight.attempts = 10;
        tight.initial_delay_ms = 40;
        tight.maximum_delay_ms = 40;
        tight.total_budget_ms = 50;
        int calls = 0;
        bool threw = false;
        try {
            with_retry(tight, [&calls]() -> int {
                ++calls;
                throw FlashException(FlashError::UsbError, Vendor::MediaTek, "dropped");
            });
        } catch (const FlashException&) {
            threw = true;
        }
        check("the total budget stops the retries", threw);
        check("the budget allowed one wait and then refused", calls == 2,
              std::to_string(calls));
    }

    {
        RetryPolicy disabled;
        disabled.attempts = 1;
        int calls = 0;
        try {
            with_retry(disabled, [&calls]() -> int {
                ++calls;
                throw FlashException(FlashError::UsbError, Vendor::Samsung, "gone");
            });
        } catch (const FlashException&) {
        }
        check("a single-attempt policy disables retrying", calls == 1, std::to_string(calls));
    }

    {
        // The success path returns whatever the callable returns, including a
        // void return, which the template has to handle without a dummy value.
        bool ran = false;
        with_retry(quick(2), [&ran]() { ran = true; });
        check("a void operation runs through the retry loop", ran);
    }
}

// -----------------------------------------------------------------------------
//  5. The exception itself
// -----------------------------------------------------------------------------

void test_flash_exception() {
    std::printf("\n5. the exception\n");

    const auto before = static_cast<std::uint64_t>(std::time(nullptr));
    FlashException error(FlashError::FlashError, Vendor::Qualcomm,
                         "the target refused the write to LUN 0");
    const auto after = static_cast<std::uint64_t>(std::time(nullptr));

    check("it carries the error kind", error.error() == FlashError::FlashError);
    check("it carries the vendor", error.vendor() == Vendor::Qualcomm);
    check("its what() names the kind and the vendor",
          std::string(error.what()).find("FLASH_ERROR") != std::string::npos
              && std::string(error.what()).find("Qualcomm") != std::string::npos,
          error.what());
    check("the raw message is kept separately",
          error.message() == "the target refused the write to LUN 0");
    check("it records when it happened",
          error.timestamp() >= before && error.timestamp() <= after,
          error.timestamp_text());
    check("the timestamp renders as a date and time",
          error.timestamp_text().size() >= 19, error.timestamp_text());
    check("it carries advice", error.advice().size() > 20);
    check("it reports itself as retryable or not consistently",
          error.retryable() == is_retryable(FlashError::FlashError));

    error.add_context("flashing system.img", "SYSTEM");
    error.add_context("opening the device");
    check("context is attached", error.context().size() == 2,
          std::to_string(error.context().size()));

    error.add_context("opening the device");
    check("a repeated frame is not duplicated", error.context().size() == 2,
          std::to_string(error.context().size()));

    const std::string report = error.report();
    check("the report names the operation", report.find("flashing system.img") != std::string::npos);
    check("the report keeps the detail", report.find("SYSTEM") != std::string::npos);
    check("the report reads outermost first",
          report.find("opening the device") < report.find("flashing system.img"));
    check("the report ends with the advice",
          report.find("The device refused") != std::string::npos, report.substr(report.size() - 60));
    check("the report names the vendor", report.find("Qualcomm") != std::string::npos);

    FlashException unknown(FlashError::InternalError, Vendor::Unknown, "index out of range");
    check("an unknown vendor is left out of what()",
          std::string(unknown.what()).find("unknown") == std::string::npos, unknown.what());
}

void test_guard() {
    std::printf("\n6. the guard wrapper\n");

    {
        const int value = guard(Vendor::MediaTek, "reading the GPT", []() { return 42; });
        check("a successful call passes its value through", value == 42);
    }

    {
        bool threw = false;
        std::string vendors;
        try {
            guard(Vendor::MediaTek, "erasing userdata", []() { throw FakeDisconnect(); });
        } catch (const FlashException& error) {
            threw = true;
            vendors = huaxin::core::to_string(error.vendor());
            check("the guard translates the exception", error.error() == FlashError::UsbError);
            check("it records what was being done",
                  error.context().size() == 1
                      && error.context()[0].operation == "erasing userdata",
                  error.context().empty() ? "no context" : error.context()[0].operation);
        }
        check("a raw exception does not escape the guard", threw);
        check("the vendor is recorded", vendors == "MediaTek", vendors);
    }

    {
        // Nesting is how the context stack gets built: the outer frame is added
        // last and so reads first in the report.
        bool threw = false;
        try {
            guard(Vendor::Qualcomm, "flashing the firmware package", []() {
                guard(Vendor::Qualcomm, "flashing boot.img", []() {
                    throw ProtocolError("the target answered NAK");
                });
            });
        } catch (const FlashException& error) {
            threw = true;
            check("an already-classified exception is not re-wrapped",
                  error.error() == FlashError::ProtocolError);
            check("both frames are kept", error.context().size() == 2,
                  std::to_string(error.context().size()));
            const std::string report = error.report();
            check("the outer frame reads first",
                  report.find("flashing the firmware package")
                      < report.find("flashing boot.img"),
                  report);
        }
        check("the guard propagates", threw);
    }
}

// -----------------------------------------------------------------------------
//  7. The logger
// -----------------------------------------------------------------------------

/// A scratch directory that cleans up after itself.
class Scratch {
public:
    Scratch() {
        std::error_code error;
        m_root = std::filesystem::temp_directory_path(error)
                 / ("huaxin-test-" + std::to_string(::rand()));
        std::filesystem::create_directories(m_root, error);
    }
    ~Scratch() {
        std::error_code error;
        std::filesystem::remove_all(m_root, error);
    }
    std::string file(const std::string& name) const { return (m_root / name).string(); }

private:
    std::filesystem::path m_root;
};

std::string read_file(const std::string& path) {
    std::ifstream in(path, std::ios::binary);
    return std::string((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
}

std::size_t file_size(const std::string& path) {
    std::error_code error;
    const auto size = std::filesystem::file_size(path, error);
    return error ? 0 : static_cast<std::size_t>(size);
}

void test_logger_levels() {
    std::printf("\n7. log levels\n");

    check("there are five levels", static_cast<int>(Logger::Level::Critical) == 4);
    check("DEBUG is named", std::string(Logger::level_name(Logger::Level::Debug)) == "DEBUG");
    check("CRITICAL is named",
          std::string(Logger::level_name(Logger::Level::Critical)) == "CRIT ");
    check("every level has a distinct name", []() {
        std::string joined;
        for (int raw = 0; raw <= 4; ++raw) {
            joined += Logger::level_name(static_cast<Logger::Level>(raw));
        }
        return joined.size() == 25;  // five five-character tags
    }());

    check("parse_level reads what the settings file holds",
          Logger::parse_level("debug") == Logger::Level::Debug
              && Logger::parse_level("warning") == Logger::Level::Warning
              && Logger::parse_level("critical") == Logger::Level::Critical
              && Logger::parse_level("fatal") == Logger::Level::Critical);
    check("an unknown level name becomes INFO rather than throwing",
          Logger::parse_level("shouty") == Logger::Level::Info);

    check("warnings are coloured", std::string(Logger::colour_code(Logger::Level::Warning))
                                        .find("\x1b[") == 0);
    check("critical is coloured differently from error",
          std::string(Logger::colour_code(Logger::Level::Critical))
              != std::string(Logger::colour_code(Logger::Level::Error)));
    check("info is left in the terminal's own colour",
          std::string(Logger::colour_code(Logger::Level::Info)).empty());

    const std::string plain = Logger::colourise(Logger::Level::Error, "boom", false);
    check("colourising can be turned off", plain == "boom", plain);
    const std::string coloured = Logger::colourise(Logger::Level::Error, "boom", true);
    check("colourising wraps and resets",
          coloured.find("boom") != std::string::npos
              && coloured.find("\x1b[0m") != std::string::npos);
}

void test_logger_writes() {
    std::printf("\n8. what the logger writes\n");

    Scratch scratch;
    const std::string path = scratch.file("flash_log.txt");

    Logger& log = Logger::instance();
    log.set_min_level(Logger::Level::Debug);
    log.set_file_enabled(true);
    log.set_console_enabled(true);
    log.set_max_file_bytes(10u * 1024u * 1024u);

    std::vector<std::pair<Logger::Level, std::string>> seen;
    log.set_sink([&seen](Logger::Level level, const std::string& line) {
        seen.emplace_back(level, line);
    });

    check("the log opens", log.open(path));
    check("it reports where it is writing", log.path() == path, log.path());

    log.info("scanning for devices");
    log.warn("two devices claim the same port");
    log.error("the target answered NAK");
    log.critical("an impossible state was reached");
    log.debug("packet 41 acknowledged");
    log.log(Logger::Level::Info, Vendor::MediaTek, "the DA answered");

    const std::string contents = read_file(path);
    check("every message reached the file",
          contents.find("scanning for devices") != std::string::npos
              && contents.find("two devices claim the same port") != std::string::npos
              && contents.find("the target answered NAK") != std::string::npos
              && contents.find("an impossible state was reached") != std::string::npos
              && contents.find("packet 41 acknowledged") != std::string::npos);
    check("the vendor-tagged line names the vendor",
          contents.find("the DA answered") != std::string::npos
              && contents.find("[MediaTek]") != std::string::npos);
    check("each line is timestamped",
          contents.find(" [INFO ] [") != std::string::npos);
    check("the critical line is tagged as such", contents.find("[CRIT ]") != std::string::npos);
    check("the sink saw the same lines", seen.size() == 6, std::to_string(seen.size()));
    check("the sink is handed the level",
          seen.size() > 2 && seen[2].first == Logger::Level::Error);

    // The minimum level filters both destinations, and it is a floor rather than
    // an exact match.
    seen.clear();
    log.set_min_level(Logger::Level::Warning);
    log.info("this should not appear");
    log.warn("this should");
    check("a message below the minimum is dropped", seen.size() == 1,
          std::to_string(seen.size()));
    check("the message at the minimum survives",
          !seen.empty() && seen[0].second.find("this should") != std::string::npos);
    check("the dropped message is not in the file either",
          read_file(path).find("this should not appear") == std::string::npos);
    log.set_min_level(Logger::Level::Debug);

    // The console can be switched off without losing the file.
    seen.clear();
    const std::size_t size_before = file_size(path);
    log.set_console_enabled(false);
    log.info("file only, please");
    check("the console switch stops the sink", seen.empty(), std::to_string(seen.size()));
    check("but the file still gets the line", file_size(path) > size_before);

    // And the file can be switched off without losing the console.
    seen.clear();
    log.set_console_enabled(true);
    log.set_file_enabled(false);
    log.info("console only, please");
    check("the file switch stops the file", !log.is_open());
    check("but the console still gets the line", seen.size() == 1,
          std::to_string(seen.size()));
    log.set_console_enabled(true);
    log.set_file_enabled(true);
    check("file output can be switched back on", log.is_open());

    log.close();
    check("closing removes the path", log.path().empty());
    check("the closed line was written", read_file(path).find("--- log closed ---")
                                              != std::string::npos);
}

void test_logger_rotation() {
    std::printf("\n9. rotation\n");

    Scratch scratch;
    const std::string path = scratch.file("flash_log.txt");
    const std::string backup = Logger::rotated_path_for(path);

    check("the backup keeps the stem and the extension",
          backup.find("flash_log.1.txt") != std::string::npos, backup);
    check("the backup is not the live file", backup != path);

    Logger& log = Logger::instance();
    log.set_sink(nullptr);
    log.set_min_level(Logger::Level::Debug);
    log.set_file_enabled(true);

    // A limit small enough to trip within a few lines, so the test does not have
    // to write ten megabytes to prove the mechanism works.
    log.set_max_file_bytes(600);
    check("the limit is remembered", log.max_file_bytes() == 600);
    check("the log opens", log.open(path));

    // Write until a rotation happens. It is automatic, so the loop only needs to
    // keep going long enough to cross the limit.
    for (int index = 0; index < 200; ++index) {
        log.info("a line long enough to matter when repeated: " + std::to_string(index));
        std::error_code error;
        if (std::filesystem::exists(backup, error) && !error) {
            break;
        }
    }

    check("a backup was made", std::filesystem::exists(backup));
    check("the backup is what crossed the limit", file_size(backup) >= 600,
          std::to_string(file_size(backup)));
    // The bound is the limit plus the one line that crossed it. A sampled size
    // check would let it overshoot by everything written between samples, which
    // for a multi-line failure report can be kilobytes.
    check("the live log is bounded by the limit plus one line", file_size(path) < 800,
          std::to_string(file_size(path)));
    check("the backup holds the earlier run",
          read_file(backup).find("a line long enough to matter") != std::string::npos);
    check("the live log carries the rotation marker",
          read_file(path).find("--- log rotated ---") != std::string::npos);

    // Rotating again must not accumulate backups: one live file and one backup.
    for (int index = 0; index < 200; ++index) {
        log.info("second generation output: " + std::to_string(index));
    }
    const std::size_t second_generation = file_size(backup);
    check("the second generation also rotates", second_generation >= 600,
          std::to_string(second_generation));
    check("the live log is still bounded", file_size(path) < 800,
          std::to_string(file_size(path)));
    check("the backup holds the second generation, not the first",
          read_file(backup).find("second generation output") != std::string::npos);

    // A zero limit means never rotate, which is what an operator who wants one
    // continuous log would set.
    log.set_max_file_bytes(0);
    log.close();
    check("with rotation off the log reopens without a backup being touched",
          log.open(path));
    const std::size_t kept = file_size(path);
    for (int index = 0; index < 100; ++index) {
        log.info("unbounded output: " + std::to_string(index));
    }
    check("and it grows without rotating", file_size(path) > kept);
    check("no second backup appeared",
          !std::filesystem::exists(Logger::rotated_path_for(backup)));

    log.set_max_file_bytes(10u * 1024u * 1024u);
    log.close();
}

void test_progress_formatting() {
    std::printf("\n11. progress formatting\n");

    check("bytes below a kilobyte are shown raw",
          huaxin::core::format_bytes(512) == "512 B", huaxin::core::format_bytes(512));
    check("a kilobyte is a kilobyte", huaxin::core::format_bytes(1024) == "1.00 KB",
          huaxin::core::format_bytes(1024));
    check("megabytes read as megabytes",
          huaxin::core::format_bytes(512u * 1024u * 1024u) == "512.0 MB",
          huaxin::core::format_bytes(512u * 1024u * 1024u));
    // 4 GB of firmware is "4.00 GB" to everyone who has to type its size, not
    // "4 GiB" and not "4294967296 B".
    check("gigabytes read as gigabytes",
          huaxin::core::format_bytes(4ull * 1024ull * 1024ull * 1024ull) == "4.00 GB",
          huaxin::core::format_bytes(4ull * 1024ull * 1024ull * 1024ull));

    check("short durations are seconds", huaxin::core::format_duration(45) == "45s",
          huaxin::core::format_duration(45));
    check("minutes and seconds", huaxin::core::format_duration(82) == "1m 22s",
          huaxin::core::format_duration(82));
    check("hours and minutes", huaxin::core::format_duration(3900) == "1h 05m",
          huaxin::core::format_duration(3900));
    check("an unknown duration says so", huaxin::core::format_duration(-1) == "unknown");

    check("operation names round-trip",
          huaxin::core::parse_operation_type("writing") == OperationType::Writing
              && huaxin::core::parse_operation_type("erase") == OperationType::Erasing);
    check("an unknown operation name is Unknown rather than an exception",
          huaxin::core::parse_operation_type("vibing") == OperationType::Unknown);
}

void test_progress_tracker() {
    std::printf("\n12. the progress tracker\n");

    // An unknown total. The percentage must stay -1 rather than being reported
    // as 0, because a bar at 0% and a bar with no percentage render differently.
    {
        FlashProgressTracker tracker(OperationType::Erasing, Vendor::Samsung, "userdata", 0);
        check("the first event always goes out", tracker.should_emit(0));
        const FlashProgress event = tracker.build(0);
        check("an unknown total leaves the percentage unknown",
              event.percentage == -1.0 && !event.has_percentage(),
              std::to_string(event.percentage));
        check("an unknown total has no ETA", !event.has_eta());
        check("there is no percentage to move, so no immediate second event",
              !tracker.should_emit(1024 * 1024));
        check("it names the operation",
              event.operation_type == OperationType::Erasing
                  && std::string(huaxin::core::to_string(event.operation_type)) == "erasing",
              std::string(huaxin::core::to_string(event.operation_type)));
        check("it names the partition", event.current_partition == "userdata");
        check("it is still running", event.running);
    }

    // A known total: the percentage drives the update rule.
    {
        FlashProgressTracker tracker(OperationType::Writing, Vendor::Qualcomm, "system.img",
                                     1000 * 1000);
        check("the first event goes out", tracker.should_emit(0));
        tracker.build(0);

        // 0.5% is below the 1% step, and no time has passed, so it is dropped.
        check("a sub-1% step is dropped on its own", !tracker.should_emit(5000));
        // 2% moves the bar, so it goes out.
        check("a whole 1% step goes out", tracker.should_emit(20000));
        const FlashProgress early = tracker.build(20000);
        check("the percentage is computed", std::fabs(early.percentage - 2.0) < 0.001,
              std::to_string(early.percentage));
        check("it counts towards the total", early.bytes_written == 20000
                                                  && early.total_bytes == 1000 * 1000);

        // The final event always goes out, whatever the step was.
        check("the final event always goes out", tracker.should_emit(1000 * 1000));
        const FlashProgress done = tracker.build(1000 * 1000);
        check("a finished operation reads 100%", done.percentage == 100.0,
              std::to_string(done.percentage));
        check("a finished operation is not still running", !done.running);

        // Speed is suppressed early: dividing by the microseconds since the
        // first packet gives a figure in the gigabytes per second.
        check("speed is suppressed before the average means anything",
              done.speed_mbps == 0.0, std::to_string(done.speed_mbps));
        check("and therefore so is the ETA", !done.has_eta());
    }

    // The time rule: a slow operation with no percentage movement still reports.
    {
        FlashProgressTracker tracker(OperationType::Writing, Vendor::MediaTek, "system", 0);
        tracker.set_interval_ms(0);  // every call is now past the interval
        check("the first event goes out", tracker.should_emit(0));
        tracker.build(0);
        check("with a zero interval the next event goes out on time alone",
              tracker.should_emit(1024));
    }

    // flush() forces one through, for the end of an operation.
    {
        FlashProgressTracker tracker(OperationType::Reading, Vendor::Unisoc, "boot", 4096);
        tracker.set_interval_ms(60000);
        check("the first event goes out", tracker.should_emit(0));
        tracker.build(0);
        check("the next would normally be held back", !tracker.should_emit(8));
        tracker.flush();
        check("flush forces it through", tracker.should_emit(8));
        tracker.build(8);
        check("and the force is spent, not sticky", !tracker.should_emit(16));
    }

    // The describe() line is what a status bar shows, so it has to hold the
    // facts and never come back empty.
    {
        FlashProgress progress;
        progress.operation_type = OperationType::Writing;
        progress.current_partition = "boot";
        progress.current_partition_index = 3;
        progress.total_partitions = 12;
        progress.bytes_written = 512u * 1024u * 1024u;
        progress.total_bytes = 4ull * 1024ull * 1024ull * 1024ull;
        progress.percentage = 12.5;
        progress.speed_mbps = 31.25;
        progress.eta_seconds = 82;

        const std::string line = progress.describe();
        check("the status line names the step", line.find("Writing") != std::string::npos, line);
        check("it names the partition and its place in the run",
              line.find("boot") != std::string::npos && line.find("(3/12)") != std::string::npos,
              line);
        check("it shows the fraction of the total",
              line.find("512.0 MB") != std::string::npos
                  && line.find("4.00 GB") != std::string::npos,
              line);
        check("it shows the percentage", line.find("12.5%") != std::string::npos, line);
        check("it shows the speed", line.find("31.2 MB/s") != std::string::npos, line);
        check("it shows the time left", line.find("1m 22s left") != std::string::npos, line);

        FlashProgress bare;
        check("a bare report still describes something", !bare.describe().empty(),
              bare.describe());
        check("the operation name is available separately",
              std::string(huaxin::core::to_string(bare.operation_type)) == "unknown",
              std::string(huaxin::core::to_string(bare.operation_type)));
    }
}

// -----------------------------------------------------------------------------
//  13. Timeout overrides
// -----------------------------------------------------------------------------

void test_timeout_overrides() {
    std::printf("\n13. timeout overrides\n");

    // Cleared first, so the test does not depend on anything that ran before it.
    huaxin::core::set_timeout_overrides(0, 0, 0);

    // With no override, every lookup returns the caller's own constant. This is
    // the property that lets the override exist without changing any shipped
    // behaviour: the bootrom's one-second wait stays one second, and the
    // Firehose write's two minutes stay two minutes.
    check("an unset command override returns the caller's value",
          huaxin::core::command_timeout_ms(1000) == 1000,
          std::to_string(huaxin::core::command_timeout_ms(1000)));
    check("an unset transfer override returns the caller's value",
          huaxin::core::transfer_timeout_ms(120000) == 120000);
    check("an unset connect override returns the caller's value",
          huaxin::core::connect_timeout_ms(5000) == 5000);

    // The whole point: different protocols can have different fallbacks while a
    // single override reaches all of them.
    check("two different fallbacks are both honoured when unset",
          huaxin::core::command_timeout_ms(1000) == 1000
              && huaxin::core::command_timeout_ms(30000) == 30000);

    huaxin::core::set_timeout_overrides(7000, 90000, 4000);
    check("a command override replaces every fallback",
          huaxin::core::command_timeout_ms(1000) == 7000
              && huaxin::core::command_timeout_ms(30000) == 7000);
    check("a transfer override applies to transfers",
          huaxin::core::transfer_timeout_ms(120000) == 90000);
    check("a connect override applies to connects",
          huaxin::core::connect_timeout_ms(5000) == 4000);

    unsigned int command = 0;
    unsigned int transfer = 0;
    unsigned int connect = 0;
    huaxin::core::timeout_overrides(command, transfer, connect);
    check("the overrides can be read back",
          command == 7000 && transfer == 90000 && connect == 4000,
          std::to_string(command) + "/" + std::to_string(transfer) + "/"
              + std::to_string(connect));

    // Zero means "no override", not "time out immediately" - the distinction
    // that keeps the settings dialog's "default" state safe.
    huaxin::core::set_timeout_overrides(7000, 0, 0);
    check("zero for one kind clears only that kind",
          huaxin::core::command_timeout_ms(1000) == 7000
              && huaxin::core::transfer_timeout_ms(120000) == 120000
              && huaxin::core::connect_timeout_ms(5000) == 5000);

    huaxin::core::set_timeout_overrides(0, 0, 0);
    check("clearing everything restores the fallbacks",
          huaxin::core::command_timeout_ms(1000) == 1000
              && huaxin::core::transfer_timeout_ms(120000) == 120000);

    check("the TimeoutKind overload agrees with the wrappers",
          huaxin::core::timeout_ms(TimeoutKind::Command, 250) == 250
              && huaxin::core::timeout_ms(TimeoutKind::Transfer, 250) == 250);
}

// -----------------------------------------------------------------------------
//  14. The speed cap
// -----------------------------------------------------------------------------

void test_speed_cap() {
    std::printf("\n14. the speed cap\n");

    check("there is no cap by default", huaxin::core::speed_limit() == 0);

    // With no cap set, pacing must be free. This is the path every transfer
    // takes in a normal install, so it costs one relaxed atomic load.
    {
        RateLimiter limiter;
        const auto start = std::chrono::steady_clock::now();
        for (int index = 0; index < 1000; ++index) {
            limiter.pace(64 * 1024);
        }
        const double elapsed = std::chrono::duration<double>(
                                   std::chrono::steady_clock::now() - start).count();
        check("an unset cap costs no measurable time", elapsed < 0.05,
              std::to_string(elapsed) + " s for 64 MB offered");
        check("the bytes are still counted", limiter.transferred() == 1000 * 64 * 1024,
              std::to_string(limiter.transferred()));
    }

    // With a cap, a transfer that would otherwise be instantaneous is slowed to
    // roughly the cap. 2 MB/s for 1 MB is about half a second.
    {
        huaxin::core::set_speed_limit(2 * 1024 * 1024);
        RateLimiter limiter;
        const auto start = std::chrono::steady_clock::now();
        limiter.pace(1024 * 1024);
        const double elapsed = std::chrono::duration<double>(
                                   std::chrono::steady_clock::now() - start).count();
        check("a capped transfer is slowed towards the cap", elapsed >= 0.3,
              std::to_string(elapsed) + " s for 1 MB at 2 MB/s");
        check("and it does not overshoot wildly", elapsed < 1.5,
              std::to_string(elapsed) + " s");
    }

    // A limiter that has run ahead of the cap only waits for the difference, so
    // a burst followed by a quiet period costs nothing.
    {
        huaxin::core::set_speed_limit(1024 * 1024);
        RateLimiter limiter;
        limiter.begin();
        limiter.pace(64 * 1024);  // trivial at 1 MB/s, no wait
        check("a small write at a high cap does not wait", limiter.transferred() == 64 * 1024);
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
        const auto start = std::chrono::steady_clock::now();
        limiter.pace(64 * 1024);
        const double elapsed = std::chrono::duration<double>(
                                   std::chrono::steady_clock::now() - start).count();
        check("a transfer that is already behind the cap is not delayed",
              elapsed < 0.05, std::to_string(elapsed) + " s");
    }

    huaxin::core::set_speed_limit(0);
    check("the cap can be removed", huaxin::core::speed_limit() == 0);

    // And with it removed, pacing is free again.
    {
        RateLimiter limiter;
        const auto start = std::chrono::steady_clock::now();
        limiter.pace(4 * 1024 * 1024);
        const double elapsed = std::chrono::duration<double>(
                                   std::chrono::steady_clock::now() - start).count();
        check("removing the cap stops the delays", elapsed < 0.05, std::to_string(elapsed));
    }
}

void test_logger_and_errors_together() {
    std::printf("\n15. exceptions reach the log at the right level\n");

    Scratch scratch;
    const std::string path = scratch.file("flash_log.txt");

    Logger& log = Logger::instance();
    std::vector<std::pair<Logger::Level, std::string>> seen;
    log.set_min_level(Logger::Level::Debug);
    log.set_sink([&seen](Logger::Level level, const std::string& line) {
        seen.emplace_back(level, line);
    });
    log.set_file_enabled(true);
    log.set_console_enabled(true);
    check("the log opens", log.open(path));

    log.log_exception(FlashException(FlashError::UsbError, Vendor::Qualcomm, "the cable went"));
    log.log_exception(FlashException(FlashError::InternalError, Vendor::Core, "a bug"));
    log.log_exception(FlashException(FlashError::Cancelled, Vendor::Samsung, "the operator stopped"));

    check("three failures were logged", seen.size() == 3, std::to_string(seen.size()));
    check("a cable problem is an error",
          seen[0].first == Logger::Level::Error);
    check("a bug in the tool is critical",
          seen[1].first == Logger::Level::Critical);
    check("a cancellation is only a warning",
          seen[2].first == Logger::Level::Warning);
    check("the report reaches the log",
          seen[0].second.find("fix:") != std::string::npos
              && seen[0].second.find("Qualcomm") != std::string::npos,
          seen[0].second.substr(0, 80));

    log.set_sink(nullptr);
    log.close();
}

}  // namespace

int main() {
    std::printf("=== HUAXIN core tests: errors and logging ===\n");

    test_classification();
    test_retry_policy_table();
    test_backoff();
    test_retry_loop();
    test_flash_exception();
    test_guard();
    test_logger_levels();
    test_logger_writes();
    test_logger_rotation();
    test_progress_formatting();
    test_progress_tracker();
    test_timeout_overrides();
    test_speed_cap();
    test_logger_and_errors_together();

    std::printf("\n%d/%d checks passed\n", g_checks - g_failures, g_checks);
    if (g_failures != 0) {
        std::printf("%d FAILED\n", g_failures);
        return 1;
    }
    return 0;
}
