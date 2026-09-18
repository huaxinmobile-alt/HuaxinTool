#pragma once

// =============================================================================
//  Unisoc Research Download session: transport + BSL handshake in one object.
//
//  This is the SPD counterpart of MediaTekBrom, and it exists for the same
//  reason: a tab needs one object it can open, ask questions of and close, and
//  the BSL session on its own has no transport and no notion of finding a
//  device. Everything this class calls is already implemented and covered by the
//  protocol tests - it adds no new protocol logic of its own.
//
//  Ordering, which the boot ROM enforces:
//
//      1. the device is in Research Download mode (1782:4d00)
//      2. the transport configures the endpoints with a class control transfer,
//         then the BSL hello is sent; without the control transfer the link is
//         silently dead - nothing errors, the device simply never answers
//      3. BSL_CMD_CONNECT, after which commands are answered
//      4. the device can then be asked about itself and its flash
//
//  Steps 2-3 happen once per connection.
//
//  WHAT THIS DOES NOT DO YET. Loading FDL1 and FDL2 and replaying a PAC is the
//  next step, and it is deliberately not guessed at here: the payload primitive
//  is the same one that loads a MediaTek agent, so the *sending* is proven, but
//  the handover between FDL1 and FDL2 - whether the device re-enumerates, and
//  what it expects before FDL2's data - is not something this project has a
//  primary source for. Until that is settled, writing to a Unisoc device through
//  this class is limited to the erase and write primitives below, which a caller
//  drives deliberately rather than as a package replay.
// =============================================================================

#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

#include "protocols/spd/bsl.h"
#include "protocols/spd/pac.h"
#include "usb/spd_transport.h"

namespace huaxin::protocols::spd {

class UnisocBsl {
public:
    struct Callbacks {
        std::function<void(const std::string& level, const std::string& message)> log;
        std::function<void(int percent, const std::string& message)> progress;
        std::function<bool()> cancelled;
    };

    explicit UnisocBsl(Callbacks callbacks = {});

    /// Unisoc download-mode USB IDs currently on the bus, as "1782:4d00".
    static std::vector<std::string> devices_present();

    // -- connection --------------------------------------------------------

    /// Opens the device and runs the hello. Throws ProtocolError with driver
    /// guidance when there is no device, or when the device does not answer as a
    /// boot ROM does.
    void connect();

    /// Sends BSL_CMD_CONNECT. Idempotent.
    void handshake();

    void disconnect() noexcept;
    bool connected() const noexcept;
    bool handshaked() const noexcept { return m_handshaked; }
    std::string describe() const;

    /// The checksum the session settled on, as text, for the log.
    std::string checksum_name() const;

    // -- the device's own account of itself --------------------------------

    /// Runs every query the boot ROM will answer and returns what it said.
    /// Individual refusals leave a field unset rather than failing the call.
    const ChipInfo& read_device_info();

    const ChipInfo& chip_info() const noexcept { return m_chip; }

    // -- flash --------------------------------------------------------------

    /// BSL_CMD_ERASE_FLASH over an address range.
    void erase_flash(std::uint32_t address, std::uint32_t length);

    /// Reads a region back.
    std::vector<std::uint8_t> read_flash(std::uint32_t address, std::uint32_t length);

    /// Writes data to a flash address.
    ///
    /// The sequence is the payload download without the execute step:
    /// START_DATA (address, length), MIDST_DATA chunks, END_DATA. The same
    /// sequence sends FDL1 and a download agent, and it is covered by the
    /// protocol tests, so this adds no unverified framing - but it is a *write*
    /// to a device, and the UI treats it as one.
    std::uint32_t write_flash(std::uint32_t address, const std::vector<std::uint8_t>& data,
                              const std::string& what = "flash data");

    /// Writes one file's worth of data to the address a PAC entry names.
    std::uint32_t write_pac_entry(const PacEntry& entry, const std::vector<std::uint8_t>& data);

    // -- leaving ------------------------------------------------------------

    /// BSL_CMD_NORMAL_RESET: restarts the device out of download mode.
    void reset();

    /// BSL_CMD_POWER_OFF.
    void power_off();

private:
    BslSession::Callbacks session_callbacks() const;
    void ensure_connected();

    usb::SpdTransport m_transport;
    Callbacks m_callbacks;
    std::unique_ptr<BslSession> m_session;
    ChipInfo m_chip;
    bool m_handshaked{false};
};

}  // namespace huaxin::protocols::spd
