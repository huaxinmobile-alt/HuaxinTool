#pragma once

#include <cstddef>
#include <mutex>
#include <string>
#include <vector>

#include "core/device_info.h"
#include "usb/usb_manager.h"

namespace huaxin::core {

/// Facade over the native backend: owns the libusb context and, from Phase 4 on,
/// the per-vendor protocol sessions (Sahara/Firehose, MTK BROM, Unisoc Research
/// Download, Samsung Odin, ...).
///
/// Threading contract
/// ------------------
/// Every public method is safe to call from any thread and serialises on an
/// internal mutex, which is also what keeps the non-thread-safe UsbManager
/// single-threaded. That is *not* a substitute for the project rule that long
/// operations run off the UI thread: these calls block their caller.
///
/// Error contract
/// --------------
/// `init()` reports failure through its return value with the reason available
/// from `last_error()`. Transport failures and programming errors throw
/// std::runtime_error (pybind11 surfaces them as RuntimeError).
class HardwareBridge {
public:
    HardwareBridge();
    ~HardwareBridge();

    // Owns OS handles - copying or moving it would defeat the mutex and the
    // RAII ownership of the libusb context.
    HardwareBridge(const HardwareBridge&) = delete;
    HardwareBridge& operator=(const HardwareBridge&) = delete;
    HardwareBridge(HardwareBridge&&) = delete;
    HardwareBridge& operator=(HardwareBridge&&) = delete;

    /// Creates the libusb context. Idempotent: calling it twice is not an error.
    /// Returns false on failure, with the reason in last_error().
    bool init();

    /// Releases the libusb context. Safe to call repeatedly, and from the
    /// destructor, so a crashed pipeline still cleans up.
    void shutdown();

    /// True between a successful init() and the matching shutdown().
    bool is_initialized() const;

    /// Every USB device libusb can see, on every bus.
    ///
    /// `read_string_descriptors` also fetches manufacturer/product/serial.
    /// That needs the device to be openable (a WinUSB-class driver on Windows)
    /// and costs up to the per-transfer timeout per device, so callers that
    /// only need identities can turn it off.
    ///
    /// `include_root_hubs` exposes the host controllers' own hubs, which are
    /// part of the machine rather than devices plugged into it.
    ///
    /// Throws std::runtime_error if init() has not run.
    std::vector<DeviceInfo> get_device_list(bool read_string_descriptors = true,
                                           bool include_root_hubs = false);

    /// Reason the last init() failed, empty when it succeeded.
    std::string last_error() const;

    /// Version of the native backend, for logs and the About dialog.
    std::string backend_version() const;

    /// Version of the libusb that is actually linked in.
    std::string libusb_version() const;

    /// Number of VID:PID pairings the catalogue knows about.
    std::size_t known_target_count() const;

private:
    mutable std::mutex m_mutex;
    usb::UsbManager m_usb;
    std::string m_last_error;
    bool m_initialized{false};
};

}  // namespace huaxin::core
