#pragma once

#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

#include "core/device_info.h"

// libusb.h is deliberately not included here: it drags in <windows.h> on
// Windows, and nothing outside usb_manager.cpp needs it.
struct libusb_context;

namespace huaxin::usb {

/// A libusb call returned an error.
class UsbError : public std::runtime_error {
public:
    UsbError(const std::string& message, int libusb_code);

    /// The raw LIBUSB_ERROR_* value, or 0 when the failure was ours not libusb's.
    int code() const noexcept { return m_code; }

private:
    int m_code;
};

/// Knobs for a scan.
struct EnumerateOptions {
    /// Read manufacturer/product/serial strings.
    ///
    /// This is not free: reading a string descriptor is a control transfer, and
    /// a device that does not answer one costs the full timeout. With the
    /// libusb helper that would be 1000 ms per transfer, twice per string and
    /// three strings per device - six seconds of dead time for one unresponsive
    /// device. We issue the transfers ourselves with a much shorter timeout.
    bool read_string_descriptors{true};

    /// Per-transfer timeout for string descriptor reads.
    unsigned int string_timeout_ms{250};

    /// Include USB root hubs (host-controller hubs) in the result.
    ///
    /// libusb lists them on Windows and they look like ordinary devices with a
    /// vendor VID, but they are part of the host controller and cannot be
    /// flashed. Off by default.
    bool include_root_hubs{false};
};

/// Owns the libusb context and enumerates devices.
///
/// Threading: NOT internally synchronised. Access is serialised by the owner
/// (HardwareBridge holds a mutex around every call). libusb contexts are not
/// meant to be driven concurrently anyway, so keeping the lock one level up
/// avoids two locks with a single ordering.
class UsbManager {
public:
    UsbManager() = default;
    ~UsbManager();

    UsbManager(const UsbManager&) = delete;
    UsbManager& operator=(const UsbManager&) = delete;
    UsbManager(UsbManager&&) = delete;
    UsbManager& operator=(UsbManager&&) = delete;

    /// Creates the libusb context. Idempotent. Throws UsbError on failure.
    void open();

    /// Destroys the context. Safe to call repeatedly; never throws.
    void close() noexcept;

    bool is_open() const noexcept { return m_context != nullptr; }

    /// Every USB device libusb can see, in bus order.
    ///
    /// A device that cannot be read is skipped rather than failing the whole
    /// scan: one flaky device must not hide the other twenty. Throws UsbError
    /// only when the context itself is unusable.
    std::vector<core::DeviceInfo> enumerate(const EnumerateOptions& options = {});

    /// libusb version string, e.g. "1.0.30.11849".
    static std::string libusb_version();

private:
    libusb_context* m_context{nullptr};
};

}  // namespace huaxin::usb
