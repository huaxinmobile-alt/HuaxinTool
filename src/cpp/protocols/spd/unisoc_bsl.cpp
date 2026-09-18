#include "protocols/spd/unisoc_bsl.h"

#include <cstdio>
#include <utility>

#include "core/flash_timeouts.h"

namespace huaxin::protocols::spd {

// The same alias bsl.cpp uses: ProtocolError is declared with the Qualcomm
// protocol and shared by every vendor that speaks over a bulk link.
using protocols::qualcomm::ProtocolError;

UnisocBsl::UnisocBsl(Callbacks callbacks) : m_callbacks(std::move(callbacks)) {}

// --- discovery ---------------------------------------------------------------

std::vector<std::string> UnisocBsl::devices_present() {
    return usb::SpdTransport::devices_present();
}

// --- connection ---------------------------------------------------------------

BslSession::Callbacks UnisocBsl::session_callbacks() const {
    // The session's callbacks are this object's callbacks: there is nothing to
    // translate between them, and a difference here would be a difference in what
    // the log says depending on which layer reported it.
    BslSession::Callbacks callbacks;
    callbacks.log = m_callbacks.log;
    callbacks.progress = m_callbacks.progress;
    callbacks.cancelled = m_callbacks.cancelled;
    return callbacks;
}

void UnisocBsl::connect() {
    if (m_session && m_session->connected()) {
        return;
    }

    if (m_callbacks.log) {
        m_callbacks.log("info", "opening a Unisoc device in Research Download mode");
    }

    usb::SpdTransport::Options options;
    options.read_timeout_ms = core::connect_timeout_ms(BslSession::kHelloTimeoutMs);
    m_transport.open(options);

    if (m_callbacks.log) {
        m_callbacks.log("debug", "transport: " + m_transport.describe());
    }

    m_session = std::make_unique<BslSession>(m_transport, session_callbacks());
    m_handshaked = false;

    // The hello both configures the endpoints and reads the boot version. It
    // throws when the device does not answer as a boot ROM does, which is the
    // useful failure: silence at this point means the driver, the cable or the
    // mode, not the protocol.
    const std::string version = m_session->usb_hello();
    if (m_callbacks.log) {
        m_callbacks.log("ok", "device answered the hello: " + version);
    }
}

void UnisocBsl::handshake() {
    ensure_connected();
    if (m_handshaked) {
        return;
    }
    m_session->connect();
    m_handshaked = true;
    if (m_callbacks.log) {
        m_callbacks.log("ok", "BSL link up, checksum " + checksum_name());
    }
}

void UnisocBsl::disconnect() noexcept {
    m_handshaked = false;
    m_chip = ChipInfo{};
    // The session holds a reference to the transport, so it is destroyed before
    // the transport closes - and destroyed first here, before the closing call,
    // because a session that outlives its link is a dangling reference.
    m_session.reset();
    try {
        m_transport.close();
    } catch (...) {
        // Closing what is already gone is not an error worth reporting.
    }
}

bool UnisocBsl::connected() const noexcept {
    return m_session != nullptr && m_transport.is_open();
}

std::string UnisocBsl::describe() const {
    return m_transport.is_open() ? m_transport.describe() : "no device open";
}

std::string UnisocBsl::checksum_name() const {
    if (!m_session) {
        return "unknown";
    }
    return to_string(m_session->checksum());
}

// --- device information -------------------------------------------------------

void UnisocBsl::ensure_connected() {
    if (!connected()) {
        throw ProtocolError(
            "no Unisoc device is open. Connect one in Research Download mode first "
            "(hold the download key while plugging it in).");
    }
}

const ChipInfo& UnisocBsl::read_device_info() {
    ensure_connected();
    handshake();
    m_chip = m_session->read_device_info();
    if (m_callbacks.log) {
        m_callbacks.log("info", m_chip.describe());
    }
    return m_chip;
}

// --- flash --------------------------------------------------------------------

void UnisocBsl::erase_flash(std::uint32_t address, std::uint32_t length) {
    ensure_connected();
    handshake();
    if (length == 0) {
        throw ProtocolError("refusing to erase a zero-length range");
    }
    if (m_callbacks.log) {
        m_callbacks.log("warn", "erasing " + std::to_string(length) + " bytes at 0x" + [&] {
            char buffer[16];
            std::snprintf(buffer, sizeof(buffer), "%08x", address);
            return std::string(buffer);
        }());
    }
    if (m_callbacks.progress) {
        m_callbacks.progress(-1, "erasing");
    }
    m_session->erase_flash(address, length);
    if (m_callbacks.log) {
        m_callbacks.log("ok", "erase finished");
    }
}

std::vector<std::uint8_t> UnisocBsl::read_flash(std::uint32_t address, std::uint32_t length) {
    ensure_connected();
    handshake();
    if (length == 0) {
        throw ProtocolError("refusing to read a zero-length range");
    }
    if (m_callbacks.log) {
        m_callbacks.log("info", "reading " + std::to_string(length) + " bytes");
    }
    return m_session->read_flash(address, length);
}

std::uint32_t UnisocBsl::write_flash(std::uint32_t address, const std::vector<std::uint8_t>& data,
                                     const std::string& what) {
    ensure_connected();
    handshake();
    if (data.empty()) {
        throw ProtocolError("refusing to write an empty image to " + what);
    }
    // `execute = false`: this is data for the flash, not a payload to run. The
    // framing is identical, which is why there is no second implementation of it.
    m_session->execute_payload(address, data, what, /*execute=*/false);
    if (m_callbacks.log) {
        m_callbacks.log("ok", what + ": " + std::to_string(data.size()) + " bytes written");
    }
    return static_cast<std::uint32_t>(data.size());
}

std::uint32_t UnisocBsl::write_pac_entry(const PacEntry& entry,
                                         const std::vector<std::uint8_t>& data) {
    if (entry.is_marker() || entry.is_logical_marker()) {
        throw ProtocolError(entry.file_id + " is a marker entry and carries no data");
    }
    if (entry.address > 0xFFFFFFFFull) {
        throw ProtocolError(entry.file_id + ": the address in the package does not fit in 32 bits");
    }
    const std::string what = entry.file_name.empty() ? entry.file_id : entry.file_name;
    return write_flash(static_cast<std::uint32_t>(entry.address), data, what);
}

// --- leaving ------------------------------------------------------------------

void UnisocBsl::reset() {
    ensure_connected();
    handshake();
    if (m_callbacks.log) {
        m_callbacks.log("info", "restarting the device out of download mode");
    }
    m_session->reset();
}

void UnisocBsl::power_off() {
    ensure_connected();
    handshake();
    m_session->power_off();
}

}  // namespace huaxin::protocols::spd
