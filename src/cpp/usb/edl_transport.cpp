#include "usb/edl_transport.h"

#include "core/flash_throttle.h"

#include "core/logger.h"
#include "usb/usb_errors.h"

#include <libusb.h>

#include <chrono>
#include <cstdio>
#include <string>

#include "protocols/qualcomm/sahara.h"

namespace huaxin::usb {

using protocols::qualcomm::ProtocolError;

namespace {

/// Writes larger than this go out in several transfers, so a stalled device
/// cannot hold a multi-megabyte transfer open indefinitely.
constexpr std::size_t kWriteChunk = 256 * 1024;

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

/// Windows cannot open a device that has no WinUSB-class driver, and the error
/// libusb returns for that is indistinguishable from a permissions problem.
/// Spelling out the fix is the difference between a usable tool and a puzzle.
std::string open_failure_hint(int code) {
    switch (code) {
        case LIBUSB_ERROR_ACCESS:
            return "\nThe device is present but could not be opened. On Windows this means no "
                   "WinUSB-class driver is bound to it: replace the Qualcomm QDLoader driver with "
                   "WinUSB or libusbK (Zadig does this in one step), then reconnect. On Linux, "
                   "check the udev rules or run with the right permissions.";
        case LIBUSB_ERROR_NOT_SUPPORTED:
            return "\nlibusb cannot drive this device with the driver currently bound to it. Bind "
                   "WinUSB or libusbK to the EDL interface (Zadig), then reconnect.";
        case LIBUSB_ERROR_NO_DEVICE:
            return "\nThe device disappeared while opening it. Re-enter EDL mode and try again.";
        case LIBUSB_ERROR_BUSY:
            return "\nAnother program has claimed the device. Close Qualcomm tools, QFIL, or any "
                   "other flashing utility and try again.";
        default:
            return "";
    }
}

std::chrono::steady_clock::time_point deadline_after(unsigned int timeout_ms) {
    return std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
}

unsigned int remaining_ms(std::chrono::steady_clock::time_point deadline) {
    const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(
                          deadline - std::chrono::steady_clock::now())
                          .count();
    return left <= 0 ? 0u : static_cast<unsigned int>(left);
}

}  // namespace

EdlTransport::~EdlTransport() {
    close();
}

bool EdlTransport::device_present() {
    libusb_context* context = nullptr;
    if (libusb_init(&context) != LIBUSB_SUCCESS) {
        return false;
    }
    libusb_device** list = nullptr;
    const ssize_t count = libusb_get_device_list(context, &list);
    bool found = false;
    if (count > 0) {
        for (ssize_t index = 0; index < count && !found; ++index) {
            libusb_device_descriptor descriptor{};
            if (libusb_get_device_descriptor(list[index], &descriptor) != LIBUSB_SUCCESS) {
                continue;
            }
            found = descriptor.idVendor == kQualcommVid && descriptor.idProduct == kEdlPid;
        }
        libusb_free_device_list(list, 1);
    }
    libusb_exit(context);
    return found;
}

void EdlTransport::open(const Options& options) {
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
        throw ProtocolError("libusb_get_device_list failed: " + libusb_error_text(static_cast<int>(count)));
    }

    libusb_device* target = nullptr;
    for (ssize_t index = 0; index < count; ++index) {
        libusb_device_descriptor descriptor{};
        if (libusb_get_device_descriptor(list[index], &descriptor) != LIBUSB_SUCCESS) {
            continue;
        }
        if (descriptor.idVendor == options.vid && descriptor.idProduct == options.pid) {
            target = list[index];
            break;
        }
    }

    if (target == nullptr) {
        libusb_free_device_list(list, 1);
        libusb_exit(context);
        char id[16];
        std::snprintf(id, sizeof(id), "%04x:%04x", options.vid, options.pid);
        throw ProtocolError(std::string("no device with USB ID ") + id
                            + " is on the bus. Put the device into EDL mode first "
                              "(from adb: 'adb reboot edl').");
    }

    // Referenced so it survives freeing the list we found it in.
    libusb_ref_device(target);

    libusb_device_handle* handle = nullptr;
    const int open_result = libusb_open(target, &handle);
    libusb_free_device_list(list, 1);

    if (open_result != LIBUSB_SUCCESS || handle == nullptr) {
        libusb_unref_device(target);
        libusb_exit(context);
        throw ProtocolError("could not open the EDL device: " + libusb_error_text(open_result)
                            + open_failure_hint(open_result));
    }

    if (options.detach_kernel_driver) {
        // Linux only in practice; on Windows this returns NOT_SUPPORTED and the
        // device is already driver-free from libusb's point of view.
        libusb_set_auto_detach_kernel_driver(handle, 1);
    }

    const int claim_result = libusb_claim_interface(handle, options.interface_number);
    if (claim_result != LIBUSB_SUCCESS) {
        libusb_close(handle);
        libusb_unref_device(target);
        libusb_exit(context);
        throw ProtocolError("could not claim EDL interface " + std::to_string(options.interface_number)
                            + ": " + libusb_error_text(claim_result) + open_failure_hint(claim_result));
    }

    // Discover the bulk endpoints from the configuration descriptor.
    libusb_config_descriptor* config = nullptr;
    std::uint8_t endpoint_in = 0;
    std::uint8_t endpoint_out = 0;

    if (libusb_get_active_config_descriptor(target, &config) == LIBUSB_SUCCESS && config != nullptr) {
        for (std::uint8_t interface_index = 0; interface_index < config->bNumInterfaces; ++interface_index) {
            const libusb_interface& interface = config->interface[interface_index];
            for (int alternate = 0; alternate < interface.num_altsetting; ++alternate) {
                const libusb_interface_descriptor& setting = interface.altsetting[alternate];
                if (setting.bInterfaceNumber != options.interface_number) {
                    continue;
                }
                for (std::uint8_t endpoint_index = 0; endpoint_index < setting.bNumEndpoints; ++endpoint_index) {
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
        throw ProtocolError("the EDL interface has no bulk endpoints; this does not look like a "
                            "device in Emergency Download mode");
    }

    m_handle = handle;
    m_endpoint_in = endpoint_in;
    m_endpoint_out = endpoint_out;
    m_interface = options.interface_number;
    m_read_timeout_ms = options.read_timeout_ms;

    char description[128];
    std::snprintf(description, sizeof(description),
                  "EDL %04x:%04x bus %u port %u, interface %d, bulk out 0x%02x / in 0x%02x",
                  options.vid, options.pid, libusb_get_bus_number(target),
                  libusb_get_port_number(target), m_interface, m_endpoint_out, m_endpoint_in);
    m_description = description;
    core::Logger::instance().info("opened " + m_description);

    // libusb keeps the context alive per open device; releasing our reference is
    // safe because the handle holds the device open.
    libusb_unref_device(target);
}

void EdlTransport::close() noexcept {
    if (m_handle == nullptr) {
        return;
    }
    core::Logger::instance().info("closing " + m_description);
    libusb_release_interface(m_handle, m_interface);
    libusb_close(m_handle);
    m_handle = nullptr;
    m_description.clear();
}

std::string EdlTransport::describe() const {
    return m_description.empty() ? "EDL transport (closed)" : m_description;
}

std::string EdlTransport::endpoints() const {
    char text[16];
    std::snprintf(text, sizeof(text), "0x%02x/0x%02x", m_endpoint_out, m_endpoint_in);
    return text;
}

void EdlTransport::write_all(const std::uint8_t* data, std::size_t size, unsigned int timeout_ms) {
    if (m_handle == nullptr) {
        throw ProtocolError("the EDL transport is not open");
    }

    const auto deadline = deadline_after(timeout_ms);
    std::size_t sent = 0;
    while (sent < size) {
        const std::size_t chunk = std::min(kWriteChunk, size - sent);
        int transferred = 0;
        const unsigned int budget = remaining_ms(deadline);
        if (budget == 0) {
            throw ProtocolError("timed out writing to the device after " + std::to_string(sent)
                                + " of " + std::to_string(size) + " bytes");
        }
        const int result = libusb_bulk_transfer(m_handle, m_endpoint_out,
                                                const_cast<std::uint8_t*>(data + sent),
                                                static_cast<int>(chunk), &transferred, budget);
        if (result == LIBUSB_SUCCESS || result == LIBUSB_ERROR_TIMEOUT) {
            if (transferred > 0) {
                sent += static_cast<std::size_t>(transferred);
                // Optional speed cap. Does nothing unless one is set.
                m_limiter.pace(static_cast<std::size_t>(transferred));
                continue;
            }
        }
        if (result != LIBUSB_SUCCESS && result != LIBUSB_ERROR_TIMEOUT) {
            if (is_disconnect(result)) {
                core::Logger::instance().error("device disconnected while writing");
                close();  // the handle is dead; stop offering it to callers
            }
            throw_transfer_error(result, "writing to the device");
        }
        if (transferred == 0) {
            throw ProtocolError("the device stopped accepting data after "
                                + std::to_string(sent) + " of " + std::to_string(size) + " bytes");
        }
    }
}

void EdlTransport::read_exact(std::uint8_t* buffer, std::size_t size, unsigned int timeout_ms) {
    if (m_handle == nullptr) {
        throw ProtocolError("the EDL transport is not open");
    }

    const auto deadline = deadline_after(timeout_ms);
    std::size_t received = 0;
    while (received < size) {
        int transferred = 0;
        const unsigned int budget = remaining_ms(deadline);
        if (budget == 0) {
            throw ProtocolError("timed out waiting for the device: got " + std::to_string(received)
                                + " of " + std::to_string(size) + " bytes");
        }
        const int result = libusb_bulk_transfer(m_handle, m_endpoint_in, buffer + received,
                                                static_cast<int>(size - received), &transferred, budget);
        if (transferred > 0) {
            // A bulk read may be short; keep going until the caller has what it
            // asked for. This is the reason read_exact exists at all.
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

std::size_t EdlTransport::read_some(std::uint8_t* buffer, std::size_t size, unsigned int timeout_ms) {
    if (m_handle == nullptr) {
        throw ProtocolError("the EDL transport is not open");
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
