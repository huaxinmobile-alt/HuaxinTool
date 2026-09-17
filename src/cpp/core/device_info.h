#pragma once

#include <cstdint>
#include <string>

namespace huaxin::core {

/// Which flashing backend a device is reachable through.
/// Populated from the VID/PID catalogue in device_catalog.cpp.
enum class TargetKind : std::uint8_t {
    Unknown = 0,
    AdbInterface,
    FastbootInterface,
    QualcommEdl,
    MediaTekBootRom,
    MediaTekPreloader,
    UnisocResearchDownload,
    SamsungDownload,
};

/// Stable identifier for the UI and for logs. Never localise this string.
const char* to_string(TargetKind kind) noexcept;

/// A device as it currently appears on the USB bus.
///
/// Everything except the descriptors is best effort: `product`, `manufacturer`
/// and `serial` are only filled when libusb can open the device, which on
/// Windows requires a WinUSB/libusbK driver to be bound to it. A device with no
/// driver is still enumerated and identified by VID/PID - it just carries no
/// strings.
struct DeviceInfo {
    // -- from the device descriptor (always available) ---------------------
    std::uint16_t vid{0};
    std::uint16_t pid{0};
    std::uint8_t bus_number{0};
    std::uint8_t port_number{0};
    std::uint8_t device_address{0};

    // -- best effort string descriptors ------------------------------------
    std::string manufacturer;
    std::string product;
    std::string serial;

    /// True for a USB root hub (the host controller's own hub), which libusb
    /// lists alongside real devices on Windows. A root hub has no parent device
    /// - that is exactly what libusb_get_parent() returning nullptr means - and
    /// it is never a flashing target, so enumeration hides it by default.
    bool is_root_hub{false};

    // -- from the VID/PID catalogue ----------------------------------------
    /// Vendor name: from the catalogue, else the USB manufacturer string.
    std::string vendor;
    /// Mode name, e.g. "EDL (Emergency Download)". Empty when unrecognised.
    std::string mode;
    /// Phase that implements this target's protocol, for UI messaging.
    std::string phase;
    TargetKind kind{TargetKind::Unknown};
    /// True when VID:PID matched a catalogue entry.
    bool recognised{false};
    /// True when that entry is confirmed against real hardware. An unrecognised
    /// device is never "verified", and neither is a guessed VID:PID pairing.
    bool verified{false};

    /// Display label: catalogue name when recognised, else the best string the
    /// device offers, else a neutral placeholder.
    std::string description;

    /// "05c6:9008" - lowercase hex, the way lsusb and Zadig render it.
    std::string usb_id() const;
};

inline std::string DeviceInfo::usb_id() const {
    static constexpr char kDigits[] = "0123456789abcdef";
    std::string id;
    id.reserve(9);
    for (int shift = 12; shift >= 0; shift -= 4) {
        id.push_back(kDigits[(vid >> shift) & 0xF]);
    }
    id.push_back(':');
    for (int shift = 12; shift >= 0; shift -= 4) {
        id.push_back(kDigits[(pid >> shift) & 0xF]);
    }
    return id;
}

}  // namespace huaxin::core
