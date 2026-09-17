#include "usb/mtk_transport.h"

#include "core/flash_throttle.h"

#include "core/logger.h"
#include "usb/usb_errors.h"

#include <libusb.h>

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <string>

#include "protocols/mediatek/brom.h"

namespace huaxin::usb {

using protocols::mediatek::kBootRomPid;
using protocols::mediatek::kMediatekVid;
using protocols::mediatek::kPreloaderPid;
using protocols::mediatek::kPreloaderPidAlt;
using protocols::qualcomm::ProtocolError;

const std::vector<UsbTarget>& kMediatekTargets() {
    static const std::vector<UsbTarget> targets = {
        {kMediatekVid, kBootRomPid, "boot ROM"},
        {kMediatekVid, kPreloaderPid, "preloader"},
        {kMediatekVid, kPreloaderPidAlt, "preloader (alt id)"},
    };
    return targets;
}

namespace {

constexpr std::size_t kWriteChunk = 64 * 1024;

std::string libusb_error_text(int code) {
    const char* name = libusb_error_name(code);
    const char* text = libusb_strerror(static_cast<enum libusb_error>(code));
    std::string message = name != nullptr ? name : std::to_string(code);
    if (text != nullptr && *text != '\0') {
        message += " (";
        message += text;
        message += ")";
    }
    return message;
}

std::string driver_hint(int code) {
    switch (code) {
        case LIBUSB_ERROR_ACCESS:
        case LIBUSB_ERROR_NOT_SUPPORTED:
            return "\nWindows cannot open a MediaTek device through its stock VCOM driver: bind "
                   "WinUSB or libusbK to interface 0 with Zadig, then reconnect. On Linux, check "
                   "the udev rules.";
        case LIBUSB_ERROR_NO_DEVICE:
            return "\nThe device disappeared while opening it. Re-enter BROM mode and try again.";
        case LIBUSB_ERROR_BUSY:
            return "\nAnother program has claimed the device. Close SP Flash Tool or any other "
                   "MediaTek utility and try again.";
        default:
            return "";
    }
}

using Deadline = std::chrono::steady_clock::time_point;

Deadline deadline_after(unsigned int timeout_ms) {
    return std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
}

unsigned int remaining_ms(Deadline deadline) {
    const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(
                          deadline - std::chrono::steady_clock::now())
                          .count();
    return left <= 0 ? 0u : static_cast<unsigned int>(left);
}

}  // namespace

const char* to_string(MtkMode mode) noexcept {
    switch (mode) {
        case MtkMode::BootRom:   return "BROM";
        case MtkMode::Preloader: return "Preloader";
        case MtkMode::Unknown:   return "unknown";
    }
    return "unknown";
}

MtkTransport::~MtkTransport() {
    close();
}

std::vector<std::string> MtkTransport::devices_present() {
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
            if (descriptor.idVendor != kMediatekVid) {
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

void MtkTransport::open(const Options& options) {
    if (m_handle != nullptr) {
        return;
    }

    libusb_context* context = nullptr;
    const int init_result = libusb_init(&context);
    if (init_result != LIBUSB_SUCCESS) {
        throw ProtocolError("libusb_init failed: " + libusb_error_text(init_result));
    }

    libusb_device** list = nullptr;
    const ssize_t count = libusb_get_device_list(context, &list);
    if (count < 0) {
        libusb_exit(context);
        throw ProtocolError("libusb_get_device_list failed: "
                            + libusb_error_text(static_cast<int>(count)));
    }

    libusb_device* target = nullptr;
    std::uint16_t matched_pid = 0;
    std::uint16_t matched_vid = 0;
    const char* target_label = "";
    for (ssize_t index = 0; index < count && target == nullptr; ++index) {
        libusb_device_descriptor descriptor{};
        if (libusb_get_device_descriptor(list[index], &descriptor) != LIBUSB_SUCCESS) {
            continue;
        }
        for (const UsbTarget& candidate : options.targets) {
            if (descriptor.idVendor == candidate.vid && descriptor.idProduct == candidate.pid) {
                target = list[index];
                matched_pid = candidate.pid;
                matched_vid = candidate.vid;
                target_label = candidate.label;
                break;
            }
        }
    }

    if (target == nullptr) {
        libusb_free_device_list(list, 1);
        libusb_exit(context);
        std::string looking;
        for (const UsbTarget& candidate : options.targets) {
            if (!looking.empty()) {
                looking += ", ";
            }
            char id[16];
            std::snprintf(id, sizeof(id), "%04x:%04x", candidate.vid, candidate.pid);
            looking += id;
        }
        throw ProtocolError(std::string("no ") + options.vendor_name + " device is in a "
                            "download mode (looking for " + looking + "). "
                            + options.absent_hint);
    }

    libusb_ref_device(target);
    libusb_device_handle* handle = nullptr;
    const int open_result = libusb_open(target, &handle);
    libusb_free_device_list(list, 1);

    if (open_result != LIBUSB_SUCCESS || handle == nullptr) {
        libusb_unref_device(target);
        libusb_exit(context);
        throw ProtocolError(std::string("could not open the ") + options.vendor_name + " device: "
                            + libusb_error_text(open_result) + driver_hint(open_result));
    }

    libusb_set_auto_detach_kernel_driver(handle, 1);
    const int claim_result = libusb_claim_interface(handle, options.interface_number);
    if (claim_result != LIBUSB_SUCCESS) {
        libusb_close(handle);
        libusb_unref_device(target);
        libusb_exit(context);
        throw ProtocolError("could not claim interface "
                            + std::to_string(options.interface_number) + ": "
                            + libusb_error_text(claim_result) + driver_hint(claim_result));
    }

    libusb_config_descriptor* config = nullptr;
    std::uint8_t endpoint_in = 0;
    std::uint8_t endpoint_out = 0;
    if (libusb_get_active_config_descriptor(target, &config) == LIBUSB_SUCCESS && config != nullptr) {
        for (std::uint8_t interface_index = 0; interface_index < config->bNumInterfaces;
             ++interface_index) {
            const libusb_interface& interface = config->interface[interface_index];
            for (int alternate = 0; alternate < interface.num_altsetting; ++alternate) {
                const libusb_interface_descriptor& setting = interface.altsetting[alternate];
                if (setting.bInterfaceNumber != options.interface_number) {
                    continue;
                }
                for (std::uint8_t endpoint_index = 0; endpoint_index < setting.bNumEndpoints;
                     ++endpoint_index) {
                    const libusb_endpoint_descriptor& endpoint = setting.endpoint[endpoint_index];
                    const bool is_bulk = (endpoint.bmAttributes & LIBUSB_TRANSFER_TYPE_MASK)
                                         == LIBUSB_TRANSFER_TYPE_BULK;
                    if (!is_bulk) {
                        continue;
                    }
                    if ((endpoint.bEndpointAddress & LIBUSB_ENDPOINT_DIR_MASK) == LIBUSB_ENDPOINT_IN) {
                        endpoint_in = endpoint.bEndpointAddress;
                    } else {
                        endpoint_out = endpoint.bEndpointAddress;
                    }
                }
            }
        }
        libusb_free_config_descriptor(config);
    }

    if (endpoint_in == 0 || endpoint_out == 0) {
        libusb_release_interface(handle, options.interface_number);
        libusb_close(handle);
        libusb_unref_device(target);
        libusb_exit(context);
        throw ProtocolError("the MediaTek interface has no bulk endpoints; this device does not "
                            "look like it is in BROM or preloader mode");
    }

    m_handle = handle;
    m_endpoint_in = endpoint_in;
    m_endpoint_out = endpoint_out;
    m_interface = options.interface_number;
    m_mode = (matched_pid == kBootRomPid) ? MtkMode::BootRom : MtkMode::Preloader;

    char description[220];
    std::snprintf(description, sizeof(description),
                  "%s %s %04x:%04x bus %u port %u, bulk out 0x%02x / in 0x%02x",
                  options.vendor_name, *target_label == 0 ? to_string(m_mode) : target_label,
                  matched_vid, matched_pid, libusb_get_bus_number(target),
                  libusb_get_port_number(target), m_endpoint_out, m_endpoint_in);
    m_description = description;
    core::Logger::instance().info("opened " + m_description);

    libusb_unref_device(target);
}

void MtkTransport::control_transfer(std::uint8_t request_type, std::uint8_t request,
                                    std::uint16_t value, std::uint16_t index,
                                    unsigned int timeout_ms) {
    if (m_handle == nullptr) {
        throw ProtocolError("the link is not open");
    }
    const int result = libusb_control_transfer(m_handle, request_type, request, value, index,
                                               nullptr, 0, timeout_ms);
    if (result < 0) {
        throw ProtocolError("the control transfer failed: " + libusb_error_text(result));
    }
}

void MtkTransport::close() noexcept {
    if (m_handle == nullptr) {
        return;
    }
    core::Logger::instance().info("closing " + m_description);
    libusb_release_interface(m_handle, m_interface);
    libusb_close(m_handle);
    m_handle = nullptr;
    m_description.clear();
}

std::string MtkTransport::describe() const {
    return m_description.empty() ? "MediaTek transport (closed)" : m_description;
}

void MtkTransport::write_all(const std::uint8_t* data, std::size_t size, unsigned int timeout_ms) {
    if (m_handle == nullptr) {
        throw ProtocolError("the MediaTek transport is not open");
    }
    const Deadline deadline = deadline_after(timeout_ms);
    std::size_t sent = 0;
    while (sent < size) {
        const std::size_t chunk = std::min(kWriteChunk, size - sent);
        const unsigned int budget = remaining_ms(deadline);
        if (budget == 0) {
            throw ProtocolError("timed out writing to the device after " + std::to_string(sent)
                                + " of " + std::to_string(size) + " bytes");
        }
        int transferred = 0;
        const int result = libusb_bulk_transfer(m_handle, m_endpoint_out,
                                                const_cast<std::uint8_t*>(data + sent),
                                                static_cast<int>(chunk), &transferred, budget);
        if (transferred > 0) {
            sent += static_cast<std::size_t>(transferred);
            // Optional speed cap. Does nothing unless one is set.
            m_limiter.pace(static_cast<std::size_t>(transferred));
            continue;
        }
        if (result == LIBUSB_ERROR_TIMEOUT) {
            throw ProtocolError("the device stopped accepting data after " + std::to_string(sent)
                                + " of " + std::to_string(size) + " bytes");
        }
        if (result != LIBUSB_SUCCESS) {
            throw_transfer_error(result, "writing to the device");
        }
    }
}

void MtkTransport::read_exact(std::uint8_t* buffer, std::size_t size, unsigned int timeout_ms) {
    if (m_handle == nullptr) {
        throw ProtocolError("the MediaTek transport is not open");
    }
    const Deadline deadline = deadline_after(timeout_ms);
    std::size_t received = 0;
    while (received < size) {
        const unsigned int budget = remaining_ms(deadline);
        if (budget == 0) {
            throw ProtocolError("timed out waiting for the device: got " + std::to_string(received)
                                + " of " + std::to_string(size) + " bytes");
        }
        int transferred = 0;
        const int result = libusb_bulk_transfer(m_handle, m_endpoint_in, buffer + received,
                                                static_cast<int>(size - received), &transferred,
                                                budget);
        if (transferred > 0) {
            received += static_cast<std::size_t>(transferred);
            continue;
        }
        if (result == LIBUSB_ERROR_TIMEOUT) {
            throw ProtocolError("timed out waiting for the device: got " + std::to_string(received)
                                + " of " + std::to_string(size) + " bytes");
        }
        if (is_disconnect(result)) {
            core::Logger::instance().error("device disconnected while reading");
            close();
        }
        throw_transfer_error(result, "reading from the device");
    }
}

std::size_t MtkTransport::read_some(std::uint8_t* buffer, std::size_t size, unsigned int timeout_ms) {
    if (m_handle == nullptr) {
        throw ProtocolError("the MediaTek transport is not open");
    }
    int transferred = 0;
    const int result = libusb_bulk_transfer(m_handle, m_endpoint_in, buffer,
                                            static_cast<int>(size), &transferred, timeout_ms);
    if (transferred > 0) {
        return static_cast<std::size_t>(transferred);
    }
    if (result == LIBUSB_ERROR_TIMEOUT) {
        throw ProtocolError("timed out waiting for a response from the device");
    }
    if (result != LIBUSB_SUCCESS) {
        if (is_disconnect(result)) {
            close();
        }
        throw_transfer_error(result, "reading from the device");
    }
    return 0;
}

}  // namespace huaxin::usb
