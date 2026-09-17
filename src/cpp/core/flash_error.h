#pragma once

// =============================================================================
//  The unified error system.
//
//  Every vendor protocol has its own idea of what went wrong. A Qualcomm target
//  answers NAK, a MediaTek bootrom stops echoing, an Odin bootloader returns a
//  non-zero status word, a Unisoc agent reports a code out of a table of
//  hundreds. An operator does not want to know which of those happened; they
//  want to know whether to replug the cable, pick a different file, or stop
//  because this device will not accept what they have.
//
//  So every failure in this tool is classified into one of seven kinds, and the
//  classification is what drives the advice, the retry decision and the UI.
//
//  WHY A NEW TYPE RATHER THAN MORE ProtocolError SUBCLASSES. ProtocolError is
//  thrown from deep inside packet code and carries only a message. It cannot say
//  whether a failure is worth retrying, and it cannot say which vendor was
//  talking. FlashException carries both, along with when it happened and what
//  the tool was doing, so a log line names the four things a person needs.
//
//  The existing exceptions are not replaced: ProtocolError, UsbDisconnectedError
//  and the rest still fly out of the protocol layers unchanged, and `classify()`
//  maps them. That keeps the verified protocol code untouched and puts the
//  translation in one place.
// =============================================================================

#include <chrono>
#include <cstdint>
#include <exception>
#include <functional>
#include <string>
#include <thread>
#include <vector>

#include "protocols/qualcomm/sahara.h"  // ProtocolError, UsbDisconnectedError

namespace huaxin::core {

/// What kind of thing went wrong. Seven values, and the advice differs for each.
enum class FlashError : std::uint8_t {
    Success = 0,
    /// The device is not there, went away, or stopped answering in time.
    UsbError,
    /// The device answered, and the answer did not fit the protocol: a bad
    /// checksum, a frame that will not parse, a response to something else.
    ProtocolError,
    /// The input is wrong: a missing file, a corrupt package, a format this
    /// build does not read.
    FileError,
    /// The write, erase or verification itself failed.
    FlashError,
    /// The device's security policy refused: secure boot, SLA, a locked
    /// bootloader, an anti-rollback check.
    AuthError,
    /// A bug, an allocation failure, or an impossible state.
    InternalError,
    /// The operator cancelled.
    Cancelled,
};

/// A short name for the log.
const char* to_string(FlashError error) noexcept;
/// What the operator should do about it. One sentence, imperative.
const char* advice_for(FlashError error) noexcept;
/// True when trying the same operation again could plausibly work.
///
/// Deliberately narrow. A timeout or a dropped link is worth another go; a
/// corrupt package, a refused write and a locked bootloader are not, and
/// retrying them wastes the operator's time and, on a flash, risks writing
/// twice.
bool is_retryable(FlashError error) noexcept;
/// True for the errors that mean the device may be left in an unknown state.
bool is_dangerous(FlashError error) noexcept;

/// Which vendor's protocol was talking when something failed.
enum class Vendor : std::uint8_t {
    Unknown = 0,
    Core,
    Adb,
    Fastboot,
    Qualcomm,
    MediaTek,
    Unisoc,
    Samsung,
};

const char* to_string(Vendor vendor) noexcept;
/// The vendor a USB identity belongs to, for classifying a failure that
/// happened before a protocol was chosen.
Vendor vendor_from_usb_id(std::uint16_t vid);

/// One frame of the context stack: what the tool was doing.
struct FlashContext {
    std::string operation;
    std::string detail;
};

/// The exception everything in this tool is reported through.
class FlashException : public std::runtime_error {
public:
    FlashException(FlashError error, Vendor vendor, const std::string& message);

    /// What went wrong, as one of seven kinds.
    FlashError error() const noexcept { return m_error; }
    /// Which vendor's protocol was talking.
    Vendor vendor() const noexcept { return m_vendor; }
    /// The error kind, the vendor and the original message, in one line.
    const char* what() const noexcept override { return m_what.c_str(); }
    /// The original message without the prefix, for a UI that shows the kind
    /// separately.
    const std::string& message() const noexcept { return m_message; }

    /// When it happened, as seconds since the epoch.
    std::uint64_t timestamp() const noexcept { return m_timestamp; }
    /// The timestamp as local time text.
    std::string timestamp_text() const;

    /// What the operator should do. Never empty.
    std::string advice() const { return advice_for(m_error); }
    /// True when retrying could plausibly work.
    bool retryable() const noexcept { return is_retryable(m_error); }

    /// Pushes a frame onto the context stack, outermost first.
    ///
    /// The stack is what turns "checksum mismatch" into "while flashing
    /// system.img to the SYSTEM partition: checksum mismatch". Without it a
    /// deep protocol failure reads as a fact about nothing.
    FlashException& add_context(const std::string& operation, const std::string& detail = "");
    const std::vector<FlashContext>& context() const noexcept { return m_context; }

    /// The whole chain: the frames, then the message, then the advice.
    std::string report() const;

private:
    void rebuild_what();

    FlashError m_error;
    Vendor m_vendor;
    std::string m_message;
    std::string m_what;
    std::uint64_t m_timestamp{0};
    std::vector<FlashContext> m_context;
};

/// Classifies an exception that has already escaped a protocol layer.
///
/// For anything derived from ProtocolError the answer comes from the exception's
/// own `error_kind()` tag, so a type added later classifies itself. For the rest
/// it is read off the type and, failing that, off the message.
///
/// The two fallbacks are chosen in opposite directions on purpose. An
/// unrecognised std::logic_error or allocation failure is InternalError, a bug
/// report. An unrecognised std::runtime_error is ProtocolError, because that is
/// where every unclassified runtime failure in this codebase comes from, and
/// treating one as a bug would hide a real device fault behind the wrong advice.
FlashError classify(const std::exception& error);

/// Runs `operation`, translating anything it throws into a FlashException with
/// the given vendor and context.
///
/// This is the one place the translation happens, so every vendor layer gets the
/// same classification without any of them having to know about it.
template <typename Callable>
auto guard(Vendor vendor, const std::string& operation,
           Callable&& callable) -> decltype(callable()) {
    try {
        return callable();
    } catch (const FlashException& error) {
        // Already classified; it only needs to say what it was doing.
        FlashException copy = error;
        copy.add_context(operation);
        throw copy;
    } catch (const std::exception& error) {
        FlashException translated(classify(error), vendor, error.what());
        translated.add_context(operation);
        throw translated;
    }
}

/// How a retry should be paced.
struct RetryPolicy {
    /// How many goes in total, including the first. 1 means no retry.
    unsigned int attempts{3};
    /// The wait before the second attempt. Each attempt after doubles it.
    unsigned int initial_delay_ms{250};
    /// The ceiling, so a long backoff cannot park the worker thread.
    unsigned int maximum_delay_ms{5000};
    /// Give up entirely after this, whatever the attempt count says. 0 disables.
    unsigned int total_budget_ms{60000};
};

/// The delay before attempt `attempt` (1-based), with exponential backoff.
unsigned int backoff_delay_ms(const RetryPolicy& policy, unsigned int attempt);

/// Replaces the process-wide default policy.
///
/// Called once from the settings when they are applied. Thread-safe: a settings
/// change can arrive while a worker thread is reading the policy.
void set_default_retry_policy(const RetryPolicy& policy);

/// A copy of the process-wide default policy.
RetryPolicy default_retry_policy();

/// Runs `operation` under a retry policy.
///
/// Only retries failures `is_retryable` says are worth another go, and only
/// while the budget lasts. Every abandoned attempt is reported through
/// `on_retry`, so the log shows the retries instead of hiding them: a flash that
/// succeeded on the third attempt is something the operator wants to know.
///
/// A FlashException is rethrown as-is. Anything else is classified first, so a
/// raw ProtocolError from a caller that skipped `guard` is still retried on its
/// merits rather than escaping unretried.
template <typename Callable>
auto with_retry(const RetryPolicy& policy, Callable&& callable,
                const std::function<void(unsigned int, const FlashException&)>& on_retry = {})
    -> decltype(callable()) {
    unsigned int spent_ms = 0;
    for (unsigned int attempt = 1;; ++attempt) {
        try {
            return callable();
        } catch (const FlashException& error) {
            const unsigned int delay = backoff_delay_ms(policy, attempt);
            const bool last = attempt >= policy.attempts;
            const bool affordable =
                policy.total_budget_ms == 0 || spent_ms + delay <= policy.total_budget_ms;
            if (last || !error.retryable() || !affordable) {
                throw;
            }
            if (on_retry) {
                on_retry(attempt, error);
            }
            spent_ms += delay;
            std::this_thread::sleep_for(std::chrono::milliseconds(delay));
        } catch (const std::exception& error) {
            // Classify, then decide. Doing it this way means the retry rule
            // lives in one place instead of being restated per catch clause.
            FlashException translated(classify(error), Vendor::Unknown, error.what());
            const unsigned int delay = backoff_delay_ms(policy, attempt);
            const bool last = attempt >= policy.attempts;
            const bool affordable =
                policy.total_budget_ms == 0 || spent_ms + delay <= policy.total_budget_ms;
            if (last || !translated.retryable() || !affordable) {
                throw translated;
            }
            if (on_retry) {
                on_retry(attempt, translated);
            }
            spent_ms += delay;
            std::this_thread::sleep_for(std::chrono::milliseconds(delay));
        }
    }
}

}  // namespace huaxin::core
