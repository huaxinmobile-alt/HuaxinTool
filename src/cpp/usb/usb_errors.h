#pragma once

// =============================================================================
//  Shared USB failure classification.
//
//  Every transfer already has a bounded timeout, so nothing can hang forever.
//  What this adds is *identification*: pulling the cable mid-transfer returns
//  LIBUSB_ERROR_NO_DEVICE from libusb, and reporting that as "USB read failed:
//  LIBUSB_ERROR_NO_DEVICE (-4)" is technically correct and practically useless.
//  It is the single most common failure an operator will hit, and it needs to
//  read like a cable problem.
//
//  It also lets a transport mark itself dead. Without that, every later call
//  retries against a handle whose device is gone, producing a cascade of
//  identical errors instead of one clear one.
// =============================================================================

#include <libusb.h>

#include <string>

#include "protocols/qualcomm/sahara.h"

namespace huaxin::usb {

using protocols::qualcomm::ProtocolError;

/// The device went away: unplugged, reset, or powered off mid-operation.
///
/// Derives from ProtocolError so existing handlers keep working unchanged; catch
/// this type specifically when the difference matters (offering to rescan rather
/// than reporting a protocol fault).
class UsbDisconnectedError : public ProtocolError {
public:
    explicit UsbDisconnectedError(const std::string& message) : ProtocolError(message) {}

    /// Tells core::classify() this is a cable problem, not a protocol one.
    const char* error_kind() const noexcept override { return "disconnect"; }
};

/// libusb error codes that mean the device is no longer usable.
inline bool is_disconnect(int libusb_code) {
    return libusb_code == LIBUSB_ERROR_NO_DEVICE || libusb_code == LIBUSB_ERROR_IO
           || libusb_code == LIBUSB_ERROR_PIPE;
}

/// Turns a libusb transfer failure into the right exception.
///
/// `what` names the operation ("reading from", "writing to") so the message says
/// which half of the conversation died.
[[noreturn]] inline void throw_transfer_error(int libusb_code, const std::string& what) {
    const char* name = libusb_error_name(libusb_code);
    const std::string text = name != nullptr ? name : std::to_string(libusb_code);

    if (is_disconnect(libusb_code)) {
        throw UsbDisconnectedError(
            "the device disconnected while " + what
            + " (" + text
            + "). Check the cable, and reconnect the device before retrying - any operation "
              "that was in progress has to be restarted from the beginning.");
    }
    throw ProtocolError("USB failure while " + what + ": " + text);
}

}  // namespace huaxin::usb
