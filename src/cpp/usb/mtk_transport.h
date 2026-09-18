#pragma once

// =============================================================================
//  Bulk transport for MediaTek BROM / Preloader mode.
//
//  Structurally the same job as EdlTransport: find a device by VID/PID, claim
//  interface 0, discover its bulk endpoints, and expose exact-length reads and
//  writes. The differences are the USB IDs it looks for, and the fact that a
//  MediaTek bootrom hides behind three of them (BROM and two preloader PIDs),
//  whichever the boot stage happens to be advertising.
//
//  TODO: unify this with EdlTransport into a single bulk transport that takes
//  its target IDs as a parameter. They are near-duplicates, kept separate for
//  now only because the Qualcomm path is already verified and the duplication is
//  cheaper than a regression there.
// =============================================================================

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#include "protocols/qualcomm/sahara.h"
#include "core/flash_throttle.h"

struct libusb_device_handle;

namespace huaxin::usb {

/// One VID/PID pair a bulk transport will open, with the name the log uses for
/// it. Generalised out of the MediaTek-only original so the Unisoc transport can
/// reuse this plumbing instead of duplicating 300 lines of libusb calls.
struct UsbTarget {
    std::uint16_t vid{0};
    std::uint16_t pid{0};
    /// What to call this identity in a log line, e.g. "boot ROM".
    const char* label{""};
};

/// The MediaTek set, which is what this transport opens when nothing else is
/// asked for.
const std::vector<UsbTarget>& kMediatekTargets();

/// Which boot stage a MediaTek device is currently in. The USB ID tells you:
/// the boot ROM and the preloader advertise different product IDs.
enum class MtkMode {
    Unknown,
    BootRom,
    Preloader,
};

const char* to_string(MtkMode mode) noexcept;

/// A libusb bulk link to a device in a vendor download mode.
///
/// Despite the name it is now the shared implementation for two vendors: the
/// MediaTek BROM and the Unisoc Research Download agent. The only thing that
/// differs is which VID/PID pairs it looks for and what it calls them, both of
/// which come from `Options::targets`. The alternatives were a third copy of
/// this file or a rename that touched the verified MediaTek path; neither was
/// worth it, so the class kept its name and grew a parameter.
class MtkTransport : public protocols::qualcomm::IByteTransport {
public:
    MtkTransport() = default;
    ~MtkTransport() override;

    MtkTransport(const MtkTransport&) = delete;
    MtkTransport& operator=(const MtkTransport&) = delete;
    MtkTransport(MtkTransport&&) = delete;
    MtkTransport& operator=(MtkTransport&&) = delete;

    struct Options {
        unsigned int read_timeout_ms{5000};
        int interface_number{0};
        /// Which identities to look for. Defaults to the MediaTek set, so every
        /// existing call site behaves exactly as before.
        std::vector<UsbTarget> targets{kMediatekTargets()};
        /// What to call the vendor in messages, e.g. "MediaTek".
        const char* vendor_name{"MediaTek"};
        /// The sentence explaining how to reach this mode, used when no
        /// matching device is on the bus.
        std::string absent_hint{
            "Power the device off, then hold the volume keys while connecting USB to enter "
            "BROM mode."};
    };

    /// Finds a matching device and opens it.
    /// Throws ProtocolError with driver guidance on failure.
    void open();
    void open(const Options& options);

    void close() noexcept;
    bool is_open() const noexcept { return m_handle != nullptr; }

    /// Which stage the opened device is in, decided by its product ID.
    MtkMode mode() const noexcept { return m_mode; }

    std::string describe() const override;

    /// MediaTek USB IDs currently present, as "0e8d:0003" style strings.
    static std::vector<std::string> devices_present();

    void write_all(const std::uint8_t* data, std::size_t size, unsigned int timeout_ms) override;
    void read_exact(std::uint8_t* buffer, std::size_t size, unsigned int timeout_ms) override;
    std::size_t read_some(std::uint8_t* buffer, std::size_t size, unsigned int timeout_ms) override;
    void control_transfer(std::uint8_t request_type, std::uint8_t request, std::uint16_t value,
                          std::uint16_t index, unsigned int timeout_ms) override;

private:
    libusb_device_handle* m_handle{nullptr};
    std::uint8_t m_endpoint_in{0};
    std::uint8_t m_endpoint_out{0};
    int m_interface{0};
    MtkMode m_mode{MtkMode::Unknown};
    std::string m_description;

    /// Optional speed cap; see core/flash_throttle.h.
    core::RateLimiter m_limiter;

};

}  // namespace huaxin::usb
