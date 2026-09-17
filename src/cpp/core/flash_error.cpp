// =============================================================================
//  The unified error system. See core/flash_error.h for why it exists.
// =============================================================================

#include "core/flash_error.h"

#include <algorithm>
#include <cctype>
#include <cstddef>
#include <ctime>
#include <mutex>
#include <new>
#include <stdexcept>

#include "core/device_catalog.h"

namespace huaxin::core {
namespace {

/// The vendor IDs this tool talks to, so a failure that happened before a
/// protocol was chosen can still name the vendor. Kept next to the catalogue's
/// own list rather than duplicating its contents: this is a lookup by identity
/// alone, and the catalogue is the authority on what each target *is*.
constexpr std::uint16_t kQualcommVid = 0x05C6;
constexpr std::uint16_t kMediaTekVid = 0x0E8D;
constexpr std::uint16_t kSamsungVid = 0x04E8;
constexpr std::uint16_t kUnisocVid = 0x1782;

/// Case-insensitive substring search, for the message-based fallbacks below.
bool contains(const std::string& haystack, const char* needle) {
    const std::size_t length = std::char_traits<char>::length(needle);
    if (length == 0 || haystack.size() < length) {
        return false;
    }
    auto lower = [](char c) {
        return static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    };
    const std::string folded(haystack.begin(), haystack.end());
    std::string lowered(folded.size(), '\0');
    std::transform(folded.begin(), folded.end(), lowered.begin(), lower);
    std::string target(needle, needle + length);
    std::transform(target.begin(), target.end(), target.begin(), lower);
    return lowered.find(target) != std::string::npos;
}

/// Local time as "YYYY-MM-DD HH:MM:SS", the shape a person reading a log wants.
std::string format_time(std::time_t when) {
    std::tm parts{};
#if defined(_WIN32)
    if (localtime_s(&parts, &when) != 0) {
        return "unknown time";
    }
#else
    if (localtime_r(&when, &parts) == nullptr) {
        return "unknown time";
    }
#endif
    char buffer[32] = {};
    if (std::strftime(buffer, sizeof(buffer), "%Y-%m-%d %H:%M:%S", &parts) == 0) {
        return "unknown time";
    }
    return buffer;
}

}  // namespace

const char* to_string(FlashError error) noexcept {
    switch (error) {
        case FlashError::Success: return "SUCCESS";
        case FlashError::UsbError: return "USB_ERROR";
        case FlashError::ProtocolError: return "PROTOCOL_ERROR";
        case FlashError::FileError: return "FILE_ERROR";
        case FlashError::FlashError: return "FLASH_ERROR";
        case FlashError::AuthError: return "AUTH_ERROR";
        case FlashError::InternalError: return "INTERNAL_ERROR";
        case FlashError::Cancelled: return "CANCELLED";
    }
    return "INTERNAL_ERROR";
}

const char* advice_for(FlashError error) noexcept {
    // Each of these is a thing the operator can actually do. Where the honest
    // answer is "there is nothing to do but start over", it says that instead of
    // inventing a step.
    switch (error) {
        case FlashError::Success:
            return "Nothing to do - the operation succeeded.";
        case FlashError::UsbError:
            return "Reconnect the device with a known-good cable, into a rear USB port "
                   "directly on the motherboard, then scan again. If it keeps happening, "
                   "check the driver as described in drivers/README.md.";
        case FlashError::ProtocolError:
            return "The device stopped speaking the protocol it started with. Disconnect it, "
                   "put it back into the correct mode, and start the operation again from the "
                   "beginning - a partially written device must be reflashed in full.";
        case FlashError::FileError:
            return "Check the firmware package: the file may be missing, truncated, or in a "
                   "format this build does not read. Verify the download and its checksum "
                   "before retrying.";
        case FlashError::FlashError:
            return "The device refused or failed the write. Do not unplug it. Retry once; if "
                   "it fails again, stop and reflash the full package rather than a single "
                   "partition, because the partition is now in an unknown state.";
        case FlashError::AuthError:
            return "This device's security policy rejected the operation. A signed firmware "
                   "build or an authorised account is required; a different package will not "
                   "help while the bootloader stays locked.";
        case FlashError::InternalError:
            return "This is a bug in the tool, not a problem with the device. Save the log "
                   "from Help > Open log folder and report it with the device model.";
        case FlashError::Cancelled:
            return "The operation was cancelled. The device may be left in a partial state - "
                   "rescan it and reflash before using it.";
    }
    return "Unknown failure.";
}

bool is_retryable(FlashError error) noexcept {
    switch (error) {
        // Worth another go: the device is there but the conversation stumbled,
        // and a fresh attempt starts from a known point.
        case FlashError::UsbError:
        case FlashError::ProtocolError:
            return true;
        // Not worth another go. A missing file will still be missing, a locked
        // bootloader will still be locked, and a failed write retried blindly is
        // how a recoverable device becomes an unrecoverable one.
        case FlashError::Success:
        case FlashError::FileError:
        case FlashError::FlashError:
        case FlashError::AuthError:
        case FlashError::Cancelled:
        case FlashError::InternalError:
            return false;
    }
    return false;
}

bool is_dangerous(FlashError error) noexcept {
    switch (error) {
        case FlashError::ProtocolError:
        case FlashError::FlashError:
        case FlashError::Cancelled:
            return true;
        default:
            return false;
    }
}

const char* to_string(Vendor vendor) noexcept {
    switch (vendor) {
        case Vendor::Unknown: return "unknown";
        case Vendor::Core: return "core";
        case Vendor::Adb: return "adb";
        case Vendor::Fastboot: return "fastboot";
        case Vendor::Qualcomm: return "Qualcomm";
        case Vendor::MediaTek: return "MediaTek";
        case Vendor::Unisoc: return "Unisoc";
        case Vendor::Samsung: return "Samsung";
    }
    return "unknown";
}

Vendor vendor_from_usb_id(std::uint16_t vid) {
    switch (vid) {
        case kQualcommVid: return Vendor::Qualcomm;
        case kMediaTekVid: return Vendor::MediaTek;
        case kSamsungVid: return Vendor::Samsung;
        case kUnisocVid: return Vendor::Unisoc;
        default: return Vendor::Unknown;
    }
}

// -----------------------------------------------------------------------------
//  FlashException
// -----------------------------------------------------------------------------

FlashException::FlashException(FlashError error, Vendor vendor, const std::string& message)
    : std::runtime_error(message),
      m_error(error),
      m_vendor(vendor),
      m_message(message),
      m_timestamp(static_cast<std::uint64_t>(std::time(nullptr))) {
    rebuild_what();
}

void FlashException::rebuild_what() {
    m_what = std::string("[") + to_string(m_error) + "] ";
    if (m_vendor != Vendor::Unknown) {
        m_what += std::string("[") + to_string(m_vendor) + "] ";
    }
    m_what += m_message;
}

std::string FlashException::timestamp_text() const {
    const std::time_t when = static_cast<std::time_t>(m_timestamp);
    return format_time(when);
}

FlashException& FlashException::add_context(const std::string& operation,
                                            const std::string& detail) {
    if (operation.empty()) {
        return *this;
    }
    // Innermost first as the stack is pushed from the outside in, but a repeated
    // frame adds nothing and would make a log line read as a stutter.
    for (const FlashContext& existing : m_context) {
        if (existing.operation == operation && existing.detail == detail) {
            return *this;
        }
    }
    m_context.push_back({operation, detail});
    return *this;
}

std::string FlashException::report() const {
    std::string text;
    text += timestamp_text();
    text += "  ";
    text += to_string(m_error);
    text += "  ";
    text += to_string(m_vendor);
    text += "\n  what: ";
    text += m_message;

    if (!m_context.empty()) {
        text += "\n  while: ";
        // The stack was pushed outermost-last, so reading it backwards gives the
        // order a person narrates: the big operation first, then the step, then
        // the packet.
        for (std::size_t index = m_context.size(); index-- > 0;) {
            if (index != m_context.size() - 1) {
                text += " > ";
            }
            text += m_context[index].operation;
            if (!m_context[index].detail.empty()) {
                text += " (";
                text += m_context[index].detail;
                text += ")";
            }
        }
    }

    text += "\n  fix:  ";
    text += advice_for(m_error);
    return text;
}

// -----------------------------------------------------------------------------
//  Classification
// -----------------------------------------------------------------------------

FlashError classify(const std::exception& error) {
    // 1. The exception classifies itself, if it knows something specific.
    //
    //    The generic "protocol" tag is deliberately *not* a short circuit. A
    //    ProtocolError whose message is "USB failure while writing:
    //    LIBUSB_ERROR_TIMEOUT" is a pipe problem, and reporting it as a protocol
    //    fault would tell the operator the device stopped speaking the protocol
    //    when the truth is that the transfer timed out. So a class that has no
    //    better answer than "protocol" falls through to the message checks
    //    below, which can still place it correctly.
    if (const auto* protocol = dynamic_cast<const protocols::qualcomm::ProtocolError*>(&error)) {
        const std::string kind = protocol->error_kind() != nullptr ? protocol->error_kind() : "";
        if (kind == "disconnect" || kind == "usb") {
            return FlashError::UsbError;
        }
        if (kind == "file") {
            return FlashError::FileError;
        }
        if (kind == "flash") {
            return FlashError::FlashError;
        }
        if (kind == "auth") {
            return FlashError::AuthError;
        }
        if (kind == "cancelled") {
            return FlashError::Cancelled;
        }
        if (kind == "internal") {
            return FlashError::InternalError;
        }
    } else if (dynamic_cast<const std::bad_alloc*>(&error) != nullptr) {
        // 2. Standard exceptions that name their own category. Only reached for
        //    exceptions outside the protocol tree, which is where they arise.
        return FlashError::InternalError;
    } else if (dynamic_cast<const std::invalid_argument*>(&error) != nullptr
               || dynamic_cast<const std::out_of_range*>(&error) != nullptr) {
        // In this codebase these come from validating a package or a user's
        // input, not from a protocol conversation.
        return FlashError::FileError;
    } else if (dynamic_cast<const std::logic_error*>(&error) != nullptr) {
        return FlashError::InternalError;
    }

    // 3. By message, for the exceptions that carry no category of their own.
    //    Ordered most specific first.
    const std::string what = error.what();
    if (contains(what, "disconnect") || contains(what, "no device")
        || contains(what, "device is not") || contains(what, "libusb")
        || contains(what, "usb failure") || contains(what, "cable")) {
        return FlashError::UsbError;
    }
    if (contains(what, "timed out") || contains(what, "timeout")) {
        return FlashError::UsbError;
    }
    if (contains(what, "cancel")) {
        return FlashError::Cancelled;
    }
    if (contains(what, "no such file") || contains(what, "cannot open")
        || contains(what, "not found") || contains(what, "corrupt")
        || contains(what, "truncated") || contains(what, "unexpected end")
        || contains(what, "bad magic") || contains(what, "unsupported")) {
        return FlashError::FileError;
    }
    if (contains(what, "secure boot") || contains(what, "authentication")
        || contains(what, "not authorised") || contains(what, "not authorized")
        || contains(what, "sla") || contains(what, "locked")) {
        return FlashError::AuthError;
    }
    if (contains(what, "erase failed") || contains(what, "write failed")
        || contains(what, "verify failed")) {
        return FlashError::FlashError;
    }

    // 4. Anything left is an unclassified runtime failure, which in this
    //    codebase means a protocol layer said something this function has not
    //    seen before. Calling it a protocol error is the honest answer: it keeps
    //    the failure retryable and marks the device as possibly mid-write, which
    //    is safer than declaring a bug and skipping the retry.
    return FlashError::ProtocolError;
}

namespace {

/// The process-wide default policy, behind a mutex because a settings change
/// arrives on one thread while another may be reading it.
std::mutex g_policy_mutex;
RetryPolicy g_default_policy;

}  // namespace

void set_default_retry_policy(const RetryPolicy& policy) {
    std::lock_guard<std::mutex> lock(g_policy_mutex);
    g_default_policy = policy;
}

RetryPolicy default_retry_policy() {
    std::lock_guard<std::mutex> lock(g_policy_mutex);
    return g_default_policy;
}

unsigned int backoff_delay_ms(const RetryPolicy& policy, unsigned int attempt) {
    if (attempt == 0 || policy.initial_delay_ms == 0) {
        return 0;
    }
    // Doubling per failed attempt, capped. The shift is bounded before it is
    // applied so a large attempt count cannot overflow into a tiny delay.
    const unsigned int steps = std::min<unsigned int>(attempt - 1, 20);
    const unsigned long long base = static_cast<unsigned long long>(policy.initial_delay_ms);
    const unsigned long long grown = base << steps;
    const unsigned long long capped =
        std::min<unsigned long long>(grown, static_cast<unsigned long long>(policy.maximum_delay_ms));
    return static_cast<unsigned int>(capped);
}

}  // namespace huaxin::core
