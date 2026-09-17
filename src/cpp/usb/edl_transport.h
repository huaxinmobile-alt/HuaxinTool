#pragma once

// =============================================================================
//  Bulk transport for Qualcomm EDL (Emergency Download) mode.
//
//  EDL exposes two bulk endpoints and no control protocol of its own - Sahara
//  and Firehose run over them as raw byte streams. The endpoints are discovered
//  from the configuration descriptor rather than hardcoded, because they are not
//  guaranteed to be 0x81/0x01 across devices.
//
//  Windows note: enumeration works without any driver, but *opening* the device
//  requires a WinUSB-class driver bound to the interface. The stock Qualcomm
//  QDLoader driver does not provide that, so EDL work on Windows needs the
//  device rebound with Zadig (or an equivalent INF) first. open() says so
//  explicitly rather than failing with a bare libusb error code.
// =============================================================================

#include <cstddef>
#include <cstdint>
#include <string>

#include "protocols/qualcomm/sahara.h"
#include "core/flash_throttle.h"

struct libusb_device_handle;

namespace huaxin::usb {

/// A libusb bulk link to a device in EDL mode.
///
/// Not thread-safe; the owner (HardwareBridge) serialises access.
class EdlTransport final : public protocols::qualcomm::IByteTransport {
public:
    /// 0x05C6:0x9008 is the Qualcomm EDL identity, verified against the VID/PID
    /// catalogue in core/device_catalog.cpp.
    static constexpr std::uint16_t kQualcommVid = 0x05C6;
    static constexpr std::uint16_t kEdlPid = 0x9008;

    struct Options {
        std::uint16_t vid{kQualcommVid};
        std::uint16_t pid{kEdlPid};
        int interface_number{0};
        unsigned int read_timeout_ms{5000};
        /// Skip libusb_set_auto_detach_kernel_driver (no-op on Windows).
        bool detach_kernel_driver{true};
    };

    EdlTransport() = default;
    ~EdlTransport() override;

    EdlTransport(const EdlTransport&) = delete;
    EdlTransport& operator=(const EdlTransport&) = delete;
    EdlTransport(EdlTransport&&) = delete;
    EdlTransport& operator=(EdlTransport&&) = delete;

    /// Finds the first matching device, opens it, claims the interface and
    /// discovers its bulk endpoints. Throws ProtocolError with an actionable
    /// message on failure.
    void open(const Options& options = {});

    /// Releases the interface and closes the device. Safe to call repeatedly.
    void close() noexcept;

    bool is_open() const noexcept { return m_handle != nullptr; }

    /// Which device this link is attached to, for logs.
    std::string describe() const override;

    /// True when a device with the EDL VID:PID is currently on the bus, whether
    /// or not we can open it.
    static bool device_present();

    // -- IByteTransport ----------------------------------------------------
    void write_all(const std::uint8_t* data, std::size_t size, unsigned int timeout_ms) override;
    void read_exact(std::uint8_t* buffer, std::size_t size, unsigned int timeout_ms) override;
    std::size_t read_some(std::uint8_t* buffer, std::size_t size, unsigned int timeout_ms) override;

    /// Endpoint addresses discovered from the descriptor, e.g. "0x81/0x01".
    std::string endpoints() const;

private:
    libusb_device_handle* m_handle{nullptr};
    std::uint8_t m_endpoint_in{0};
    std::uint8_t m_endpoint_out{0};
    int m_interface{0};
    unsigned int m_read_timeout_ms{5000};
    std::string m_description;

    /// Optional speed cap; see core/flash_throttle.h.
    core::RateLimiter m_limiter;

};

}  // namespace huaxin::usb
