#include "usb/spd_transport.h"

#include <cstdio>

#include <libusb.h>

#include "protocols/spd/bsl.h"

namespace huaxin::usb {

using protocols::spd::kResearchDownloadPid;
using protocols::spd::kUnisocVid;

std::vector<UsbTarget> unisoc_targets() {
    return {
        {kUnisocVid, kResearchDownloadPid, "Research Download"},
    };
}

void SpdTransport::open() {
    open(Options{});
}

void SpdTransport::open(const Options& options) {
    MtkTransport::Options inner;
    inner.read_timeout_ms = options.read_timeout_ms;
    inner.interface_number = options.interface_number;
    inner.targets = unisoc_targets();
    inner.vendor_name = "Unisoc";
    inner.absent_hint =
        "Hold the volume-down key (or the key this device uses for download mode) while "
        "connecting USB, and make sure the interface has a WinUSB-class driver bound with "
        "Zadig - the stock driver will not open.";
    MtkTransport::open(inner);
}

std::vector<std::string> SpdTransport::devices_present() {
    std::vector<std::string> found;
    libusb_context* context = nullptr;
    if (libusb_init(&context) != LIBUSB_SUCCESS) {
        return found;
    }
    libusb_device** list = nullptr;
    const ssize_t count = libusb_get_device_list(context, &list);
    if (count > 0) {
        for (ssize_t index = 0; index < count; ++index) {
            libusb_device_descriptor descriptor{};
            if (libusb_get_device_descriptor(list[index], &descriptor) != LIBUSB_SUCCESS) {
                continue;
            }
            // The whole vendor id, not just the one PID: a device in a mode this
            // build does not speak is still worth reporting as present, because
            // "nothing found" and "found but not in download mode" send the
            // operator to different places.
            if (descriptor.idVendor != kUnisocVid) {
                continue;
            }
            char id[16];
            std::snprintf(id, sizeof(id), "%04x:%04x", descriptor.idVendor, descriptor.idProduct);
            found.emplace_back(id);
        }
        libusb_free_device_list(list, 1);
    }
    libusb_exit(context);
    return found;
}

}  // namespace huaxin::usb
