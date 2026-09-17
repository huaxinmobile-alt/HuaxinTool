#include "usb/usb_manager.h"

#include <libusb.h>  // via the include directory exported by our libusb target

#include <algorithm>
#include <cstddef>
#include <string>
#include <utility>

#include "core/device_catalog.h"

namespace huaxin::usb {

namespace {

/// LIBUSB_ERROR_* value -> text. Falls back to the numeric code so a failure is
/// never reported as an empty string.
std::string describe(int libusb_code) {
    if (libusb_code == 0) {
        return "no error";
    }
    const char* name = libusb_error_name(libusb_code);
    const char* text = libusb_strerror(static_cast<enum libusb_error>(libusb_code));
    std::string message = name != nullptr ? name : std::to_string(libusb_code);
    if (text != nullptr && *text != '\0') {
        message += " (";
        message += text;
        message += ")";
    }
    return message;
}

/// libusb_alloc/free pairs that must survive exceptions.
struct DeviceListGuard {
    libusb_device** list{nullptr};
    ~DeviceListGuard() {
        if (list != nullptr) {
            libusb_free_device_list(list, 1);  // 1 = also unref the devices
        }
    }
};

struct HandleGuard {
    libusb_device_handle* handle{nullptr};
    ~HandleGuard() {
        if (handle != nullptr) {
            libusb_close(handle);
        }
    }
};

/// Reads the device's first supported language ID (USB 2.0 spec, 9.6.7).
/// String descriptor index 0 returns a list of LANGID words.
std::uint16_t first_language_id(libusb_device_handle* handle, unsigned int timeout_ms) {
    unsigned char buffer[32] = {};
    const int received = libusb_control_transfer(
        handle, LIBUSB_ENDPOINT_IN, LIBUSB_REQUEST_GET_DESCRIPTOR,
        static_cast<std::uint16_t>(LIBUSB_DT_STRING << 8),  // index 0
        0, buffer, sizeof(buffer), timeout_ms);

    if (received < 4 || buffer[1] != LIBUSB_DT_STRING) {
        return 0;
    }
    // LANGID array starts at offset 2, little endian, one entry per language.
    return static_cast<std::uint16_t>(buffer[2] | (buffer[3] << 8));
}

/// Reads one string descriptor as ASCII.
///
/// Non-ASCII code units become '?', which is what libusb's own helper does and
/// is good enough for "MediaTek" or "Pixel 7". The transfer length is bounded by
/// `timeout_ms` rather than the 1 s libusb hardcodes.
std::string read_string(libusb_device_handle* handle, std::uint8_t index,
                        std::uint16_t langid, unsigned int timeout_ms) {
    if (index == 0) {
        return {};
    }

    unsigned char buffer[256] = {};
    const int received = libusb_control_transfer(
        handle, LIBUSB_ENDPOINT_IN, LIBUSB_REQUEST_GET_DESCRIPTOR,
        static_cast<std::uint16_t>((LIBUSB_DT_STRING << 8) | index),
        langid, buffer, sizeof(buffer), timeout_ms);

    if (received < 2 || buffer[1] != LIBUSB_DT_STRING) {
        return {};
    }

    // bLength counts the two header bytes; trust the smaller of it and what
    // actually arrived, since a device may answer with a short descriptor.
    const int length = std::min<int>(received, buffer[0]);
    std::string text;
    text.reserve(static_cast<std::size_t>(std::max(0, length - 2)) / 2);
    for (int offset = 2; offset + 1 < length; offset += 2) {
        const auto code_unit = static_cast<std::uint16_t>(buffer[offset] | (buffer[offset + 1] << 8));
        text.push_back(code_unit < 0x80 ? static_cast<char>(code_unit) : '?');
    }
    return text;
}

/// Fills in the string descriptors, if the device will let us read them.
///
/// Opening fails routinely on Windows for anything without a WinUSB/libusbK
/// driver bound (which is most of the bus), and on Linux without udev
/// permissions. That is not an error: the device descriptor is what identifies
/// the target, and that we already have.
void read_string_descriptors(libusb_device* device, const libusb_device_descriptor& descriptor,
                             const EnumerateOptions& options, core::DeviceInfo& info) {
    libusb_device_handle* raw_handle = nullptr;
    if (libusb_open(device, &raw_handle) != LIBUSB_SUCCESS || raw_handle == nullptr) {
        return;
    }
    HandleGuard guard{raw_handle};

    const std::uint16_t langid = first_language_id(raw_handle, options.string_timeout_ms);
    if (langid == 0) {
        return;
    }

    info.manufacturer = read_string(raw_handle, descriptor.iManufacturer, langid, options.string_timeout_ms);
    info.product = read_string(raw_handle, descriptor.iProduct, langid, options.string_timeout_ms);
    info.serial = read_string(raw_handle, descriptor.iSerialNumber, langid, options.string_timeout_ms);
}

/// Display label: catalogue name when recognised, otherwise the best the device
/// itself offers, otherwise an honest placeholder.
std::string build_description(const core::DeviceInfo& info) {
    if (info.recognised) {
        std::string label = info.vendor;
        if (!info.mode.empty()) {
            label += " - ";
            label += info.mode;
        }
        if (!info.verified) {
            label += " [unverified]";
        }
        if (!info.product.empty()) {
            label += " (" + info.product + ")";
        }
        return label;
    }

    if (!info.product.empty()) {
        return info.product;
    }
    if (!info.manufacturer.empty()) {
        return info.manufacturer;
    }
    return "Unrecognised USB device";
}

}  // namespace

UsbError::UsbError(const std::string& message, int libusb_code)
    : std::runtime_error(message + ": " + describe(libusb_code)), m_code(libusb_code) {}

UsbManager::~UsbManager() {
    close();
}

void UsbManager::open() {
    if (m_context != nullptr) {
        return;
    }

    libusb_context* context = nullptr;
    const int rc = libusb_init(&context);
    if (rc != LIBUSB_SUCCESS || context == nullptr) {
        throw UsbError("libusb_init failed", rc);
    }

    // Errors only. libusb writes to stderr directly rather than through us, and
    // at WARNING it emits per-scan noise on any normal bus (HID devices cannot
    // be opened read/write, root hubs report a missing connection descriptor).
    // Raise this to LIBUSB_LOG_LEVEL_WARNING first when diagnosing a device that
    // fails to enumerate.
    libusb_set_option(context, LIBUSB_OPTION_LOG_LEVEL, LIBUSB_LOG_LEVEL_ERROR);
    m_context = context;
}

void UsbManager::close() noexcept {
    if (m_context == nullptr) {
        return;
    }
    libusb_exit(m_context);
    m_context = nullptr;
}

std::vector<core::DeviceInfo> UsbManager::enumerate(const EnumerateOptions& options) {
    if (m_context == nullptr) {
        throw UsbError("USB context is not open - call open() first", 0);
    }

    libusb_device** raw_list = nullptr;
    const ssize_t count = libusb_get_device_list(m_context, &raw_list);
    if (count < 0) {
        throw UsbError("libusb_get_device_list failed", static_cast<int>(count));
    }
    DeviceListGuard list_guard{raw_list};

    std::vector<core::DeviceInfo> devices;
    devices.reserve(static_cast<std::size_t>(count));

    for (ssize_t index = 0; index < count; ++index) {
        libusb_device* device = raw_list[index];

        libusb_device_descriptor descriptor{};
        if (libusb_get_device_descriptor(device, &descriptor) != LIBUSB_SUCCESS) {
            continue;  // one unreadable device must not hide the rest
        }

        // A device with no parent is a root hub: libusb_get_parent() documents
        // nullptr for the top of the tree.
        const bool is_root_hub = libusb_get_parent(device) == nullptr;
        if (is_root_hub && !options.include_root_hubs) {
            continue;
        }

        core::DeviceInfo info;
        info.is_root_hub = is_root_hub;
        info.vid = descriptor.idVendor;
        info.pid = descriptor.idProduct;
        info.bus_number = libusb_get_bus_number(device);
        info.port_number = libusb_get_port_number(device);
        info.device_address = libusb_get_device_address(device);

        if (const core::KnownTarget* target = core::find_target(info.vid, info.pid)) {
            info.recognised = true;
            info.kind = target->kind;
            info.vendor = target->vendor;
            info.mode = target->mode;
            info.phase = target->phase;
            info.verified = target->verified;
        } else if (const char* vendor = core::vendor_for_vid(info.vid)) {
            info.vendor = vendor;  // known maker, mode we do not implement
        }

        if (options.read_string_descriptors) {
            read_string_descriptors(device, descriptor, options, info);
        }

        // A recognised device always shows its catalogue name; an unknown one
        // is better described by its own product string.
        if (!info.recognised && info.vendor.empty() && !info.manufacturer.empty()) {
            info.vendor = info.manufacturer;
        }
        info.description = build_description(info);

        devices.push_back(std::move(info));
    }

    return devices;
}

std::string UsbManager::libusb_version() {
    // libusb.h declares this as a plain struct with no typedef, so the
    // elaborated-type-specifier is required here.
    const struct libusb_version* version = libusb_get_version();
    if (version == nullptr) {
        return "unknown";
    }
    return std::to_string(version->major) + "." + std::to_string(version->minor) + "."
           + std::to_string(version->micro) + "." + std::to_string(version->nano)
           + (version->rc != nullptr && *version->rc != '\0' ? std::string(version->rc) : std::string());
}

}  // namespace huaxin::usb
