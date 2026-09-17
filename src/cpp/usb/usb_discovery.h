#pragma once

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <string>
#include "usb/usb_errors.h"

namespace huaxin::usb {

/// Discovery failed before absence/presence could be determined.
class UsbDiscoveryError : public ProtocolError {
public:
    using ProtocolError::ProtocolError;
};

inline std::string discovery_message(int code, const std::string& stage,
                                     const std::string& target = "USB devices") {
    const char* name = libusb_error_name(code);
    return stage + " failed while checking " + target + ": "
        + (name ? std::string(name) : std::to_string(code))
        + ". USB discovery is unavailable; device presence is unknown. "
          "On Linux, check that /dev/bus/usb is available and accessible; "
          "a container or VM needs USB passthrough. On Windows, check the "
          "libusb runtime and USB services. Device-driver binding is a separate "
          "step after enumeration succeeds. See docs/usb-initialization.md.";
}

/// Injectable libusb entry points for hardware-free discovery tests.
struct UsbProbeApi {
    decltype(&libusb_init) init = &libusb_init;
    decltype(&libusb_get_device_list) list = &libusb_get_device_list;
    decltype(&libusb_get_device_descriptor) descriptor = &libusb_get_device_descriptor;
    decltype(&libusb_free_device_list) free_list = &libusb_free_device_list;
    decltype(&libusb_exit) exit = &libusb_exit;
};

inline bool probe_usb_device(std::uint16_t vid, std::uint16_t pid,
                             const UsbProbeApi& api = {}) {
    char id[16];
    std::snprintf(id, sizeof(id), "%04x:%04x", vid, pid);
    libusb_context* context = nullptr;
    const int initialized = api.init(&context);
    if (initialized != LIBUSB_SUCCESS) {
        throw UsbDiscoveryError(discovery_message(initialized, "libusb_init", id));
    }
    struct ContextGuard {
        libusb_context* context;
        const UsbProbeApi& api;
        ~ContextGuard() { api.exit(context); }
    } context_guard{context, api};
    libusb_device** devices = nullptr;
    const auto count = api.list(context, &devices);
    if (count < 0) {
        throw UsbDiscoveryError(discovery_message(static_cast<int>(count),
                                                  "libusb_get_device_list", id));
    }
    struct ListGuard {
        libusb_device** devices;
        const UsbProbeApi& api;
        ~ListGuard() { if (devices) api.free_list(devices, 1); }
    } list_guard{devices, api};
    for (std::ptrdiff_t index = 0; index < count; ++index) {
        libusb_device_descriptor descriptor{};
        const int result = api.descriptor(devices[index], &descriptor);
        if (result != LIBUSB_SUCCESS) {
            throw UsbDiscoveryError(discovery_message(result, "libusb_get_device_descriptor", id));
        }
        if (descriptor.idVendor == vid && descriptor.idProduct == pid) return true;
    }
    return false;
}

}  // namespace huaxin::usb
