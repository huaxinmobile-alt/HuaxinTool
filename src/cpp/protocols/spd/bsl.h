#pragma once

// =============================================================================
//  Spreadtrum / Unisoc Research Download protocol (BSL).
//
//  PROVENANCE - the command codes, the framing and the checksums were taken from
//  sources that agree with each other:
//
//    * Mani-Sadhasivam/uwpflash (Apache-2.0), command.h - the complete BSL
//      command and response enum, both directions, with the meanings written
//      next to each value.
//    * iscle/sprdclient (GPL-3.0), main.c - the HDLC framing, the USB hello, the
//      payload-download sequence, and the two checksum implementations as they
//      appear in a client that talks to real hardware.
//    * ajsb85/sprdflash-rs (MIT), sprdflash-core/src/bsl.rs and checksum.rs -
//      the same framing with the chip-family checksum split named explicitly.
//
//  THE FRAMING IS HDLC. A frame is
//
//      0x7E  <escaped body>  0x7E
//
//  where the body is `type(u16 BE) | size(u16 BE) | data | checksum(u16 BE)` and
//  escaping replaces 0x7E and 0x7D with 0x7D followed by the byte xor 0x20.
//
//  THE CHECKSUM IS NOT ONE ALGORITHM. Classic Spreadtrum boot ROMs (SC65xx,
//  SC77xx, SC98xx) use CRC-16-CCITT; the RDA8910/UIS8910 family uses the
//  Spreadtrum ones-complement word sum. The two are not interchangeable and the
//  host cannot choose: a frame with the wrong one gets no answer at all, which
//  looks exactly like a dead device. So the kind is *detected* from the device's
//  first reply, where either can be tried against the bytes that arrived.
// =============================================================================

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include "protocols/mediatek/brom.h"    // IByteTransport
#include "protocols/qualcomm/sahara.h"  // ProtocolError
#include "core/flash_timeouts.h"
#include "protocols/spd/checksum.h"

namespace huaxin::protocols::spd {

/// The byte pipe is the same abstraction the other vendors use, which is what
/// lets one scripted transport test all four protocols.
using IByteTransport = protocols::mediatek::IByteTransport;

// --- framing constants -------------------------------------------------------
/// The HDLC flag that opens and closes a frame.
inline constexpr std::uint8_t kBslFlag = 0x7E;
/// The HDLC escape byte.
inline constexpr std::uint8_t kBslEscape = 0x7D;
/// What an escaped byte is xored with.
inline constexpr std::uint8_t kBslEscapeMask = 0x20;

/// The largest data field a frame may carry.
inline constexpr std::size_t kBslMaxData = 4096;
/// The chunk size a MIDST_DATA frame uses.
///
/// 528 is not arbitrary: the frame body is 4 + 528 + 2 = 534 bytes, and in the
/// worst case every one of those bytes needs escaping, giving 1068, plus the two
/// flags. That fits the 4096-byte buffer with room to spare. Both reference
/// clients use exactly this number.
inline constexpr std::size_t kBslMidstChunk = 528;

/// The offset the boot ROM jumps to inside a payload it was given.
///
/// `EXEC_DATA` runs the payload at its load address plus this, which is where
/// the loader's entry point is expected to sit after its header.
inline constexpr std::uint32_t kBslPayloadEntryOffset = 0x200;

// --- commands ----------------------------------------------------------------
/// Host to device. Names and values from uwpflash's `enum CMD_TYPE`.
enum class BslCommand : std::uint16_t {
    Connect = 0x00,
    StartData = 0x01,
    MidstData = 0x02,
    EndData = 0x03,
    ExecData = 0x04,
    NormalReset = 0x05,
    ReadFlash = 0x06,
    ReadChipType = 0x07,
    ReadNvItem = 0x08,
    ChangeBaud = 0x09,
    EraseFlash = 0x0A,
    Repartition = 0x0B,
    ReadFlashType = 0x0C,
    ReadFlashInfo = 0x0D,
    ReadSectorSize = 0x0F,
    ReadStart = 0x10,
    ReadMidst = 0x11,
    ReadEnd = 0x12,
    KeepCharge = 0x13,
    ExtTable = 0x14,
    ReadFlashUid = 0x15,
    PowerOff = 0x17,
    CheckRoot = 0x19,
    ReadChipUid = 0x1A,
    ReadPartition = 0x2D,
    EndProcess = 0x7F,
};

/// Device to host. The same enum's upper half.
enum class BslResponse : std::uint16_t {
    Ack = 0x80,
    Version = 0x81,
    InvalidCommand = 0x82,
    UnknownCommand = 0x83,
    OperationFailed = 0x84,
    NotSupportBaudrate = 0x85,
    DownloadNotStarted = 0x86,
    DownloadMultiStart = 0x87,
    DownloadEarlyEnd = 0x88,
    DownloadDestError = 0x89,
    DownloadSizeError = 0x8A,
    VerifyError = 0x8B,
    NotVerify = 0x8C,
    PhoneNotEnoughMemory = 0x8D,
    PhoneWaitInputTimeout = 0x8E,
    PhoneSucceed = 0x8F,
    PhoneValidBaudrate = 0x90,
    PhoneRepeatContinue = 0x91,
    PhoneRepeatBreak = 0x92,
    ReadFlashResult = 0x93,
    ReadChipTypeResult = 0x94,
    ReadNvItemResult = 0x95,
    IncompatiblePartition = 0x96,
    UnknownDevice = 0x97,
    InvalidDeviceSize = 0x98,
    IllegalSdram = 0x99,
    WrongSdramParameter = 0x9A,
    ReadFlashInfoResult = 0x9B,
    ReadSectorSizeResult = 0x9C,
    ReadFlashTypeResult = 0x9D,
    ReadFlashUidResult = 0x9E,
    ErrorChecksum = 0xA0,
    ChecksumDiff = 0xA1,
    WriteError = 0xA2,
    ChipIdNotMatch = 0xA3,
    FlashConfigError = 0xA4,
    PhoneIsRooted = 0xA7,
    SecurityVerifyError = 0xAA,
    ReadChipUidResult = 0xAB,
    NotEnableWriteFlash = 0xAC,
    EnableSecureBootError = 0xAD,
    FlashWrittenProtection = 0xB3,
    FlashInitializingFail = 0xB4,
    UnsupportedCommand = 0xFE,
    /// The agent's own log output. Carries text rather than a verdict.
    Log = 0xFF,
};

const char* to_string(BslCommand command) noexcept;
const char* to_string(BslResponse response) noexcept;
/// A sentence for a response code, for the log. Unknown codes are reported as a
/// number rather than guessed at.
std::string describe_response(std::uint16_t code);

// --- frames ------------------------------------------------------------------
/// A decoded BSL frame: the type, the data, and how it was checked.
struct BslFrame {
    std::uint16_t type{0};
    std::vector<std::uint8_t> data;
    /// Which checksum the frame's trailing two bytes actually matched.
    ChecksumKind checksum{ChecksumKind::Unknown};
};

/// HDLC-escapes a frame body - everything between the two flags.
std::vector<std::uint8_t> hdlc_escape(const std::vector<std::uint8_t>& body);
/// Reverses it. A trailing lone escape byte is dropped rather than throwing:
/// a USB read can split a frame anywhere, and the caller reassembles.
std::vector<std::uint8_t> hdlc_unescape(const std::vector<std::uint8_t>& body);

/// Builds one complete frame, flags included.
std::vector<std::uint8_t> build_frame(std::uint16_t type, const std::vector<std::uint8_t>& data,
                                      ChecksumKind checksum);

/// Decodes an unescaped frame body: type, data, and the checksum that matched.
///
/// Throws ProtocolError when the body is too short, when the declared length runs
/// past what arrived, or when neither checksum matches - a frame that verifies
/// with neither is not a BSL frame and continuing would interpret noise as a
/// verdict.
BslFrame parse_frame(const std::vector<std::uint8_t>& body);

/// Which checksum a body's trailing bytes agree with, or Unknown.
ChecksumKind detect_checksum(const std::vector<std::uint8_t>& body) noexcept;

// --- device information ------------------------------------------------------
/// What the boot ROM reports about itself.
struct ChipInfo {
    /// The version string from BSL_REP_VER, e.g. "SPRD3".
    std::string boot_version;
    /// ``BSL_REP_READ_CHIP_TYPE``: the chip type word.
    bool have_chip_type{false};
    std::uint32_t chip_type{0};
    /// ``BSL_REP_READ_FLASH_INFO``: raw, because the field layout of this reply
    /// is not confirmed by the sources this build was written from.
    bool have_flash_info{false};
    std::vector<std::uint8_t> flash_info;
    /// ``BSL_REP_READ_FLASH_TYPE``.
    bool have_flash_type{false};
    std::uint32_t flash_type{0};
    /// ``BSL_REP_READ_SECTOR_SIZE``.
    bool have_sector_size{false};
    std::uint32_t sector_size{0};
    /// ``BSL_REP_READ_CHIP_UID``.
    bool have_chip_uid{false};
    std::vector<std::uint8_t> chip_uid;

    std::string describe() const;
};

// --- session -----------------------------------------------------------------
class BslSession {
public:
    struct Callbacks {
        std::function<void(const std::string& level, const std::string& message)> log;
        std::function<void(int percent, const std::string& message)> progress;
        std::function<bool()> cancelled;
    };

    BslSession(IByteTransport& transport, Callbacks callbacks);

    // -- framing -----------------------------------------------------------

    /// Writes one frame using the session's checksum.
    void send_frame(std::uint16_t type, const std::vector<std::uint8_t>& data);

    /// Reads frames until one arrives that is not the agent's log output, and
    /// returns it. Log frames are reported through the log callback.
    BslFrame read_frame(unsigned int timeout_ms);

    /// Sends a command and requires an ACK.
    void send_and_expect_ack(std::uint16_t type, const std::vector<std::uint8_t>& data,
                             const std::string& what,
                             unsigned int timeout_ms = core::command_timeout_ms(kCommandTimeoutMs));

    // -- checksum ----------------------------------------------------------

    /// The checksum the session is using.
    ChecksumKind checksum() const noexcept { return m_checksum; }
    /// True once a device reply has settled which checksum is in use.
    bool checksum_known() const noexcept { return m_checksum != ChecksumKind::Unknown; }
    /// Forces a checksum, for a caller that knows the chip family. Normally the
    /// session works it out from the first reply.
    void set_checksum(ChecksumKind kind) noexcept { m_checksum = kind; }

    // -- handshake ---------------------------------------------------------

    /// The USB hello: a control transfer to configure the endpoints, then a lone
    /// HDLC flag. The device answers with its version string.
    ///
    /// Returns the version text. Throws when the device does not answer as a
    /// boot ROM does.
    std::string usb_hello(unsigned int timeout_ms = core::connect_timeout_ms(kHelloTimeoutMs));

    /// BSL_CMD_CONNECT, which the device answers with an ACK. This is the point
    /// at which the link is up and commands can be sent.
    void connect();

    /// True once connect() has succeeded.
    bool connected() const noexcept { return m_connected; }

    // -- payload download --------------------------------------------------

    /// Sends a payload and runs it: START_DATA with the address and length,
    /// MIDST_DATA chunks, END_DATA, EXEC_DATA.
    ///
    /// This is the primitive that loads FDL1, and then FDL2 through FDL1 - the
    /// two are the same operation at different addresses.
    void execute_payload(std::uint32_t address, const std::vector<std::uint8_t>& payload,
                         const std::string& what, bool execute = true);

    // -- device information ------------------------------------------------

    /// Everything the boot ROM will say about itself and its flash. Each query
    /// is optional to the device, so a refusal leaves that field unset rather
    /// than failing the whole call.
    ChipInfo read_device_info();

    /// BSL_CMD_READ_CHIP_TYPE.
    std::vector<std::uint8_t> read_chip_type();
    /// BSL_CMD_READ_FLASH_INFO.
    std::vector<std::uint8_t> read_flash_info();
    /// BSL_CMD_READ_FLASH_TYPE.
    std::vector<std::uint8_t> read_flash_type();
    /// BSL_CMD_READ_SECTOR_SIZE.
    std::vector<std::uint8_t> read_sector_size();
    /// BSL_CMD_READ_CHIP_UID.
    std::vector<std::uint8_t> read_chip_uid();

    // -- flash operations --------------------------------------------------

    /// BSL_CMD_ERASE_FLASH over an address range.
    void erase_flash(std::uint32_t address, std::uint32_t length);

    /// Reads a region: READ_START, READ_MIDST until done, READ_END.
    std::vector<std::uint8_t> read_flash(std::uint32_t address, std::uint32_t length);

    /// BSL_CMD_NORMAL_RESET: restarts the device out of download mode.
    void reset();

    /// BSL_CMD_POWER_OFF.
    void power_off();

    // -- timeouts ----------------------------------------------------------
    /// A plain command.
    static constexpr unsigned int kCommandTimeoutMs = 5000;
    /// Waiting for the device to announce itself after the hello.
    static constexpr unsigned int kHelloTimeoutMs = 5000;
    /// One MIDST_DATA chunk.
    static constexpr unsigned int kChunkTimeoutMs = 10000;
    /// An erase or a read, which take as long as the flash does.
    static constexpr unsigned int kFlashTimeoutMs = 120000;

private:
    void log(const std::string& level, const std::string& message) const;
    void check_cancelled() const;
    /// Reads bytes until a complete frame has arrived, unescapes it and decodes
    /// it. The link delivers whatever it likes per read, so a frame can span
    /// several or arrive with others.
    BslFrame read_frame_from_link(unsigned int timeout_ms);

    IByteTransport& m_transport;
    Callbacks m_callbacks;
    ChecksumKind m_checksum{ChecksumKind::Unknown};
    bool m_connected{false};
    /// Bytes read from the link that are not yet a complete frame.
    std::vector<std::uint8_t> m_pending;
};

/// The USB IDs this protocol speaks on.
inline constexpr std::uint16_t kUnisocVid = 0x1782;
inline constexpr std::uint16_t kResearchDownloadPid = 0x4D00;

}  // namespace huaxin::protocols::spd
