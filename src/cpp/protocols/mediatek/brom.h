#pragma once

// =============================================================================
//  MediaTek BROM / Preloader protocol.
//
//  PROVENANCE - every constant and byte layout here was transcribed from the
//  bootrom protocol as implemented by mtkclient (github.com/bkerler/mtkclient,
//  GPL-3.0), files Library/Port.py, Library/mtk_preloader.py and
//  Library/Connection/devicehandler.py. No mtkclient code was copied: what is
//  reused is the protocol itself - command codes, framing, checksum algorithm
//  and bit layouts - which is what interoperability requires.
//
//  NOTE ON LICENSING: mtkclient is GPL-3.0, unlike the BSD-3-Clause sources used
//  for the Qualcomm and libusb work. Protocol facts are not copyrightable, but
//  if you intend to distribute this tool, have that reviewed rather than taking
//  my word for it.
//
//  Two things about this protocol surprise people coming from Qualcomm EDL:
//
//    * It is BIG-endian. Every word the host sends and reads is big-endian -
//      the exact opposite of Sahara/Firehose. Assuming consistency silently
//      produces garbage addresses and lengths.
//    * Commands are acknowledged by ECHO. The host sends N bytes and reads N
//      bytes back; they must match. There is no status field on the command
//      path itself; a mismatch means desynchronisation.
// =============================================================================

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include "protocols/qualcomm/sahara.h"  // IByteTransport, ProtocolError
#include "core/flash_timeouts.h"

namespace huaxin::protocols::mediatek {

/// The byte pipe is the same abstraction Sahara uses - a bulk link that can
/// read and write exact lengths. Reusing it rather than declaring a parallel
/// interface is what lets one scripted transport test both protocols.
using IByteTransport = protocols::qualcomm::IByteTransport;

// --- command codes -----------------------------------------------------------
// From mtkclient's Cmd enum (Library/mtk_preloader.py).
enum class BromCommand : std::uint8_t {
    Read16 = 0xD0,
    Read32 = 0xD1,
    Write16 = 0xD2,
    Write16NoEcho = 0xD3,
    Write32 = 0xD4,
    JumpDa = 0xD5,
    JumpBl = 0xD6,
    SendDa = 0xD7,
    GetTargetConfig = 0xD8,
    SendEnvPrepare = 0xD9,
    RegisterAccess = 0xDA,
    Uart1LogEnable = 0xDB,
    Uart1SetBaudrate = 0xDC,
    SendCert = 0xE0,
    GetMeId = 0xE1,
    SendAuth = 0xE2,
    Sla = 0xE3,
    GetSocId = 0xE7,
    Zeroization = 0xF0,
    GetPlCap = 0xFB,
    GetHwSwVer = 0xFC,
    GetHwCode = 0xFD,
    GetBlVer = 0xFE,
    GetVersion = 0xFF,
};

const char* to_string(BromCommand command) noexcept;

// --- handshake ---------------------------------------------------------------
/// The four bytes the host sends to start a BROM conversation. The device
/// answers each with its bitwise complement, so sending 0xA0 must be answered
/// by 0x5F, 0x0A by 0xF5, and so on.
inline constexpr std::uint8_t kHandshakeBytes[] = {0xA0, 0x0A, 0x50, 0x05};
inline constexpr std::size_t kHandshakeLength = 4;

/// The echo the device must produce, in order.
inline constexpr std::uint8_t kHandshakeEcho[] = {0x5F, 0xF5, 0xAF, 0xFA};

/// Some bootroms need 0xA0 sent on its own before the rest.
inline constexpr std::uint8_t kHandshakeLead = 0xA0;

/// Status values returned by SEND_DA before the data phase.
inline constexpr std::uint16_t kStatusOk = 0;
/// The bootrom wants a signed authentication handshake first.
inline constexpr std::uint16_t kStatusSlaRequired = 0x1D0D;

// --- target config -----------------------------------------------------------
/// What the bootrom reports about its own security configuration.
/// Bit assignments transcribed from mtkclient's get_target_config().
struct TargetConfig {
    std::uint32_t raw{0};
    std::uint16_t status{0};

    bool secure_boot{false};        // 0x01
    bool sla_required{false};       // 0x02
    bool da_authentication{false};  // 0x04
    bool sw_jtag{false};            // 0x06
    bool epp_supported{false};      // 0x08
    bool certificate_required{false};  // 0x10
    bool memory_read_allowed{false};   // 0x20
    bool memory_write_allowed{false};  // 0x40
    bool cmd_c8_supported{false};      // 0x80

    std::string describe() const;
};

/// Decodes the raw configuration word.
TargetConfig decode_target_config(std::uint32_t raw, std::uint16_t status);

// --- chip identity -----------------------------------------------------------
struct BromChipInfo {
    bool have_hw_code{false};
    std::uint16_t hardware_code{0};   // e.g. 0x0672 for MT6735
    std::uint16_t hardware_version{0};

    bool have_hw_sw_version{false};
    std::uint16_t hw_sw_hw_code{0};
    std::uint16_t hw_sw_hw_sub_code{0};
    std::uint16_t hw_sw_hw_version{0};
    std::uint16_t hw_sw_sw_version{0};

    bool have_brom_version{false};
    std::uint32_t brom_version{0};

    /// A marketing name where the hardware code is one this project has
    /// verified. Deliberately empty otherwise - see the TODO in brom.cpp.
    std::string chip_name;
};

// --- download agent ----------------------------------------------------------
struct DownloadAgent {
    std::vector<std::uint8_t> payload;
    /// Length of `payload` that is code; the file's last `signature_length`
    /// bytes are the signature the bootrom is told about separately.
    std::size_t code_length{0};
    std::uint32_t signature_length{0};
    std::uint32_t load_address{0};
};

/// Checksum over the transfer: XOR of the 16-bit little-endian words, with the
/// buffer padded to an even length. Transcribed from mtkclient's prepare_data().
std::uint16_t compute_checksum(const std::vector<std::uint8_t>& data);
std::uint16_t compute_checksum(const std::uint8_t* data, std::size_t size);

/// Builds the byte stream SEND_DA transmits: the file, padded to an even length.
/// The signature is not separated here - the bootrom is told its length and
/// expects it as the tail of this same buffer.
std::vector<std::uint8_t> build_da_payload(const std::vector<std::uint8_t>& file);

// --- session -----------------------------------------------------------------
class BromSession {
public:
    struct Callbacks {
        std::function<void(const std::string& level, const std::string& message)> log;
        std::function<void(int percent, const std::string& message)> progress;
        std::function<bool()> cancelled;
    };

    BromSession(IByteTransport& transport, Callbacks callbacks);

    /// Runs the BROM handshake: sends the handshake word one byte at a time and
    /// verifies each complemented echo. Throws ProtocolError if the device does
    /// not answer as a bootrom does.
    ///
    /// `send_lead_byte` sends 0xA0 alone first, which some bootroms require.
    void handshake(bool send_lead_byte = false);

    /// True once handshake() has succeeded.
    bool handshaked() const noexcept { return m_handshaked; }

    /// Sends a command and verifies the device echoes it back.
    void send_command(BromCommand command,
                      unsigned int timeout_ms = core::command_timeout_ms(1000));

    /// Sends a command, verifies the echo, then reads `length` bytes.
    std::vector<std::uint8_t> send_command_with_response(BromCommand command, std::size_t length,
                                                         unsigned int timeout_ms = core::command_timeout_ms(1000));

    /// Reads a big-endian 16/32-bit word, or `count` bytes.
    std::uint16_t read_word(unsigned int timeout_ms = core::command_timeout_ms(1000));
    std::uint32_t read_dword(unsigned int timeout_ms = core::command_timeout_ms(1000));
    std::vector<std::uint8_t> read_bytes(std::size_t length, unsigned int timeout_ms = core::command_timeout_ms(1000));

    /// Reads the hardware code and version (GET_HW_CODE, 0xFD).
    BromChipInfo read_chip_info();

    /// Reads the target configuration (GET_TARGET_CONFIG, 0xD8).
    TargetConfig read_target_config();

    /// Uploads the download agent (SEND_DA, 0xD7) and then starts it
    /// (JUMP_DA, 0xD5). Throws ProtocolError with a specific message when the
    /// bootrom demands SLA authentication first.
    void upload_download_agent(const DownloadAgent& agent,
                               bool jump = true,
                               std::size_t chunk_size = 4096);

    /// Jumps to an address (JUMP_DA). Used by upload_download_agent().
    void jump_to_download_agent(std::uint32_t address);

private:
    void log(const std::string& level, const std::string& message) const;
    void check_cancelled() const;
    void write_bytes(const std::vector<std::uint8_t>& data, unsigned int timeout_ms);

    IByteTransport& m_transport;
    Callbacks m_callbacks;
    bool m_handshaked{false};
};

/// The MediaTek USB IDs this protocol speaks on, from the VID/PID catalogue.
inline constexpr std::uint16_t kMediatekVid = 0x0E8D;
inline constexpr std::uint16_t kBootRomPid = 0x0003;
inline constexpr std::uint16_t kPreloaderPid = 0x2000;
inline constexpr std::uint16_t kPreloaderPidAlt = 0x2001;

}  // namespace huaxin::protocols::mediatek
