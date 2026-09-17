#pragma once

// =============================================================================
//  Samsung Download mode (Odin protocol) and the PIT it exposes.
//
//  PROVENANCE - two sources, because the protocol has two generations and
//  Heimdall only implements the older one:
//
//    * Heimdall (github.com/Benjamin-Dobell/Heimdall, MIT licence), libpit/
//      source/libpit.h and heimdall/source/{Packet,OutboundPacket,ControlPacket,
//      SessionSetupPacket,ResponsePacket}.h - the PIT layout, the 1024-byte
//      control packet, the control type values and the classic command set.
//    * Marza4/rodin (github.com/Marza4/rodin), src/odin.rs - the newer
//      generation: the ODIN/LOKE handshake, the protocol version in the
//      begin-session response, the SendFilePartSize request that version 2 and
//      above requires, the 0x69 device-info command, the 500-byte PIT block
//      protocol, and the exact payload the end-of-sequence packet carries.
//
//  Both are permissive: attribution is required and is given here.
//
//  Endianness: LITTLE-endian throughout, like Qualcomm and unlike MediaTek.
//
//  Two layers live here, deliberately separated because only one of them needs
//  a device:
//
//    * PitData  - a pure parser for the partition table. Fully testable.
//    * OdinSession - the USB conversation. Testable through a scripted
//      transport, which checks the bytes the host emits.
// =============================================================================

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include "protocols/qualcomm/sahara.h"  // IByteTransport, ProtocolError

namespace huaxin::protocols::samsung {

/// Bulk link, shared with the other protocol implementations so one scripted
/// transport can test them all.
using IByteTransport = protocols::qualcomm::IByteTransport;

// --- PIT ---------------------------------------------------------------------
/// One entry of the partition information table. Layout from Heimdall's
/// PitEntry: nine 32-bit fields then three 32-byte name buffers, 132 bytes total.
struct PitEntry {
    std::uint32_t binary_type{0};       // 0 = application processor, 1 = comms processor
    std::uint32_t device_type{0};       // 0 = OneNAND, 1 = file/FAT, 2 = MMC, 3 = all
    std::uint32_t identifier{0};        // partition id as Odin refers to it
    std::uint32_t attributes{1};        // bit 0 = writable, bit 1 = STL
    std::uint32_t update_attributes{0}; // bit 0 = FOTA, bit 1 = secure
    std::uint32_t block_size_or_offset{0};
    std::uint32_t block_count{0};
    std::uint32_t file_offset{0};  // obsolete in the format
    std::uint32_t file_size{0};    // obsolete in the format

    std::string partition_name;
    std::string flash_filename;  // the name Odin transfers the image under
    std::string fota_filename;

    /// The partition can be written to (attribute bit 0).
    bool writable() const noexcept { return (attributes & 0x1) != 0; }
    /// Bit 1 of the attributes; Heimdall marks it STL, hence the name.
    bool stl() const noexcept { return (attributes & 0x2) != 0; }
    bool fota_update() const noexcept { return (update_attributes & 0x1) != 0; }
    bool secure() const noexcept { return (update_attributes & 0x2) != 0; }

    /// Size in bytes, from the block geometry. 0 when the entry carries no
    /// block size, which some vendor PITs do for informational entries.
    std::uint64_t size_bytes() const noexcept;

    std::string device_type_name() const;
};

/// A parsed PIT.
struct PitData {
    /// Magic that starts a PIT: Heimdall's kFileIdentifier.
    static constexpr std::uint32_t kFileIdentifier = 0x12349876;
    static constexpr std::size_t kHeaderDataSize = 28;
    static constexpr std::size_t kEntryDataSize = 132;

    std::uint32_t reserved1{0};
    std::uint32_t reserved2{0};
    std::vector<PitEntry> entries;

    /// Total size a PIT with this many entries occupies, before padding.
    std::size_t expected_size() const noexcept;

    /// Finds an entry by partition name. Returns nullptr when absent.
    const PitEntry* find(const std::string& partition_name) const;

    /// True when a partition table internally consistent enough to flash
    /// against: entries present, names non-empty, no two entries claiming the
    /// same id. A PIT that fails this is one a repartition would wreck a device
    /// with, so it is worth knowing before the write rather than after.
    bool looks_sane() const;

    /// Why `looks_sane` is false, or an empty string when it is true.
    std::string sanity_problem() const;
};

/// Parses a PIT image.
///
/// Throws ProtocolError with a specific reason when the data is not a PIT, has a
/// truncated header, or promises more entries than the data can hold. A parser
/// that returned a half-filled table instead would send an operator to flash a
/// partition that does not exist.
PitData parse_pit(const std::uint8_t* data, std::size_t size);

/// Convenience overload for a byte vector.
PitData parse_pit(const std::vector<std::uint8_t>& data);

/// Builds a PIT image from its parts, for the repartition path and for tests.
///
/// The result is padded to the 4096-byte boundary Odin expects, because a PIT
/// is written in whole flash blocks.
std::vector<std::uint8_t> build_pit(const PitData& pit);

// --- Odin protocol -----------------------------------------------------------
/// Packet control types, from Heimdall's ControlPacket (outbound) and
/// ResponsePacket (inbound) enums. The two directions share the values.
enum class OdinControl : std::uint32_t {
    SendFilePart = 0x00,
    Session = 0x64,
    PitFile = 0x65,
    FileTransfer = 0x66,
    EndSession = 0x67,
    /// Device information. Not in Heimdall - it is a newer command, and its
    /// reply is the one place in this protocol that carries a magic word.
    DeviceInfo = 0x69,
};

const char* to_string(OdinControl control) noexcept;

/// Requests carried by a session packet, from Heimdall's SessionSetupPacket.
enum class OdinSessionRequest : std::uint32_t {
    BeginSession = 0,
    DeviceType = 1,
    TotalBytes = 2,
    FilePartSize = 5,
    EnableTFlash = 8,
};

/// Requests carried by a PIT packet, from Heimdall's PitFilePacket.
enum class OdinPitRequest : std::uint32_t {
    Flash = 0x00,
    Dump = 0x01,
    Part = 0x02,
    EndTransfer = 0x03,
};

/// Requests carried by a file-transfer packet, from Heimdall's
/// FileTransferPacket.
enum class OdinFileRequest : std::uint32_t {
    Flash = 0x00,
    Dump = 0x01,
    Part = 0x02,
    End = 0x03,
};

/// Requests carried by an end-session packet.
enum class OdinEndRequest : std::uint32_t {
    EndSession = 0x00,
    /// Reboot back into download mode rather than out of it.
    RebootToDownload = 0x02,
};

/// An outbound control packet is a fixed 1024 bytes, zero padded, with the
/// control type at offset 0.
inline constexpr std::size_t kControlPacketSize = 1024;
/// An inbound response is 8 bytes: the response type and a result word.
inline constexpr std::size_t kResponsePacketSize = 8;
/// Typical file part size; the device may negotiate a different one.
inline constexpr std::uint32_t kDefaultFilePartSize = 131072;
/// What a protocol version 2 or newer bootloader uses instead.
inline constexpr std::uint32_t kModernFilePartSize = 1024u * 1024u;
/// The sequence size both generations use for a file transfer.
inline constexpr std::uint32_t kDefaultSequenceSize = 30u * 1024u * 1024u;
/// The magic that starts a device-info reply.
inline constexpr std::uint32_t kDeviceInfoMagic = 0x12345678;
/// Timeouts. Named rather than literal, because the two ends of this range mean
/// different things: a handshake reply is immediate or the device is not there,
/// while a flash sequence runs as long as the flash takes.
inline constexpr unsigned int kHandshakeTimeout = 5000;
inline constexpr unsigned int kCommandTimeout = 5000;
inline constexpr unsigned int kFlashTimeout = 120000;

/// A PIT is written in whole 4096-byte blocks.
inline constexpr std::size_t kPitBlockSize = 4096;
/// A PIT dump arrives in 500-byte pieces, which is not a typo: the protocol
/// reads them that way and the last one is short.
inline constexpr std::size_t kPitDumpBlockSize = 500;

// --- the handshake -----------------------------------------------------------
//  The link does not answer anything until this exchange has happened. It is
//  four bytes out and four bytes back:
//
//      host -> "ODIN"      device -> "LOKE"
//
//  There is no checksum on it and no length field: it is a literal string
//  comparison on both sides.
//
//  NOTE ON THE NAME. This exchange is sometimes described as an "AOLN magic",
//  which is not what either side sends - the strings are ODIN and LOKE, and both
//  reference implementations use exactly those. The value implemented here is
//  the verified one.
inline constexpr char kHandshakeRequest[] = "ODIN";
inline constexpr char kHandshakeResponse[] = "LOKE";
inline constexpr std::size_t kHandshakeLength = 4;
/// The control-transfer form a bootloader falls back to, from odin4: an OUT
/// with this request type and value, then an IN for the reply.
inline constexpr std::uint8_t kHandshakeOutRequestType = 0x40;
inline constexpr std::uint8_t kHandshakeInRequestType = 0xC0;
inline constexpr std::uint8_t kHandshakeRequestCode = 0x01;
inline constexpr std::uint16_t kHandshakeValue = 0x0200;

// --- checksums ---------------------------------------------------------------
//  THERE IS NO PACKET CHECKSUM IN THIS PROTOCOL. Heimdall has none, rodin has
//  none, and the 1024-byte control packet carries no checksum field in either.
//  A task description that asks for an "XOR-based checksum" per packet is
//  describing something this protocol does not have, and inventing one would
//  produce frames every bootloader rejects.
//
//  Where integrity actually lives is the `.tar.md5` container: an MD5 over the
//  whole archive, appended to it. That is implemented in tar.h, and it is the
//  check that matters - a truncated firmware download is the failure a flash
//  actually suffers.

/// Little-endian helpers, shared by the two implementation units. Everything in
/// this protocol is little-endian, like Qualcomm's and unlike MediaTek's.
inline void write_le32(std::vector<std::uint8_t>& out, std::size_t offset, std::uint32_t value) {
    out[offset] = static_cast<std::uint8_t>(value & 0xFF);
    out[offset + 1] = static_cast<std::uint8_t>((value >> 8) & 0xFF);
    out[offset + 2] = static_cast<std::uint8_t>((value >> 16) & 0xFF);
    out[offset + 3] = static_cast<std::uint8_t>((value >> 24) & 0xFF);
}

inline std::uint32_t read_le32(const std::uint8_t* data) {
    return static_cast<std::uint32_t>(data[0]) | (static_cast<std::uint32_t>(data[1]) << 8)
           | (static_cast<std::uint32_t>(data[2]) << 16)
           | (static_cast<std::uint32_t>(data[3]) << 24);
}

inline std::uint32_t read_le32(const std::vector<std::uint8_t>& data, std::size_t offset) {
    return read_le32(data.data() + offset);
}

/// Builds a bare control packet of `size` bytes.
std::vector<std::uint8_t> build_control_packet(OdinControl control, std::size_t size = kControlPacketSize);

/// Builds a session packet carrying one request word.
std::vector<std::uint8_t> build_session_packet(OdinSessionRequest request);

/// Builds a session packet carrying a request and one argument word.
std::vector<std::uint8_t> build_session_packet(OdinSessionRequest request, std::uint32_t argument);

/// Builds a PIT packet carrying a request and one argument word.
std::vector<std::uint8_t> build_pit_packet(OdinPitRequest request, std::uint32_t argument = 0);

/// Builds a file-transfer packet carrying a request and one argument word.
std::vector<std::uint8_t> build_file_packet(OdinFileRequest request, std::uint32_t argument = 0);

/// Builds an end-session packet.
std::vector<std::uint8_t> build_end_packet(OdinEndRequest request);

/// One decoded response.
///
/// The meaning of the second word depends entirely on the response type, which
/// is a trap worth stating plainly:
///
///   * session (0x64)  - it is the device's preferred **packet size** (older
///                       bootloaders) or a **protocol version** in its high half
///                       (newer ones). Reading it as a status makes every
///                       session look refused.
///   * pit file (0x65) - it is the **length of the PIT** that follows.
///   * everything else - it is a status, where 0 means success.
struct OdinResponse {
    OdinControl type{OdinControl::Session};
    std::uint32_t result{0};

    /// True when the word is a status and it says success. Meaningless for
    /// session and PIT responses; use `result` there.
    bool accepted() const noexcept { return result == 0; }
};

/// Decodes an 8-byte response, checking the type matches what was asked for.
OdinResponse decode_response(const std::uint8_t* data, std::size_t size, OdinControl expected);

// --- session ----------------------------------------------------------------
/// What the device says about itself when a session opens.
struct SessionInfo {
    /// The raw second word of the begin-session response.
    std::uint32_t raw{0};
    /// The protocol version, read from the high half of `raw`. That is where the
    /// newer bootloaders put it; on a classic one the same bits are part of the
    /// packet size, which is why both are reported rather than one being
    /// silently preferred.
    std::uint16_t version{0};
    /// The file part size to use, decided from the version and the raw word.
    std::uint32_t packet_size{kDefaultFilePartSize};
    /// The version's top bit, which the newer bootloaders set when they accept
    /// LZ4-compressed transfers. Reported, never used: this build sends
    /// uncompressed data, which every bootloader accepts.
    bool lz4_supported{false};
    /// True when the version-bearing reading is the one the device meant.
    /// Set when the high half looks like a plausible version rather than part
    /// of a packet size.
    bool version_known{false};

    std::string describe() const;
};

/// One partition's entry in a device-info reply.
struct DeviceInfoEntry {
    std::uint32_t index{0};
    std::uint32_t offset{0};
};

/// The device-info reply: a magic, a count, and two parallel arrays.
struct DeviceInfo {
    std::uint32_t magic{0};
    std::uint32_t count{0};
    std::vector<DeviceInfoEntry> entries;
    /// True when the magic was the expected one. The reply is still exposed when
    /// it is not, because a newer bootloader with a different magic is worth
    /// looking at rather than being thrown away.
    bool recognised{false};
    std::string describe() const;
};

/// One file to flash: its name inside the archive and the partition it goes to.
struct FlashTarget {
    /// The member name in the archive, e.g. "boot.img".
    std::string file_name;
    /// The partition name as the PIT spells it, e.g. "BOOT".
    std::string partition_name;
    /// 0 for the application processor, 1 for the comms processor. Decides
    /// which layout the end-of-sequence packet uses.
    std::uint32_t binary_type{0};
    /// The device type the PIT records for this partition.
    std::uint32_t device_type{0};
    /// The partition id the PIT records.
    std::uint32_t partition_id{0};
};

/// The result of flashing one file.
struct FlashOutcome {
    std::string file_name;
    std::string partition_name;
    std::uint64_t bytes{0};
    bool succeeded{false};
    std::string error;
};

/// Drives the Odin conversation over a transport.
///
/// Ordering enforced by the device: handshake, begin session, [set packet size],
/// [read PIT], [device info], transfer file parts, end session. Nothing can be
/// flashed before the session is open, and nothing at all works before the
/// handshake.
class OdinSession {
public:
    struct Callbacks {
        std::function<void(const std::string& level, const std::string& message)> log;
        std::function<void(int percent, const std::string& message)> progress;
        std::function<bool()> cancelled;
    };

    OdinSession(IByteTransport& transport, Callbacks callbacks);

    /// The ODIN/LOKE exchange. Must happen before anything else, and nothing
    /// else works without it.
    ///
    /// Tries the bulk form first and falls back to the control-transfer form,
    /// which is what a bootloader that has stalled its bulk endpoint needs.
    void handshake();

    /// True once the handshake has succeeded.
    bool handshake_done() const noexcept { return m_handshake_done; }

    /// Sends the begin-session request, carrying the highest protocol version
    /// this host understands, and decodes the reply.
    SessionInfo begin_session();

    /// The session's parameters, from the last begin_session().
    const SessionInfo& session_info() const noexcept { return m_session; }

    /// Announces the device type to the device.
    void send_device_type(std::uint32_t device_type);

    /// Tells the device how many bytes will be transferred in total.
    void send_total_bytes(std::uint64_t total);

    /// Sets the file part size used by later transfers.
    void set_file_part_size(std::uint32_t size);

    /// Reads the partition table from the device.
    ///
    /// Odin reports the PIT length first, then streams it in 500-byte blocks.
    /// Each block is requested individually, which is the part that is easy to
    /// get wrong: it is not one long read.
    PitData read_pit();

    /// Reads the device information. Optional: an older bootloader does not
    /// implement 0x69 and says so instead of answering.
    DeviceInfo read_device_info();

    // -- flashing ----------------------------------------------------------

    /// Flashes one file to one partition, streaming it from a buffer.
    ///
    /// `total_bytes` should be the sum of every file in the run, announced once
    /// before the first transfer: Odin uses it for its own progress reporting,
    /// and a device told the wrong total can refuse later sequences.
    FlashOutcome flash_partition(const FlashTarget& target, const std::vector<std::uint8_t>& image,
                                 std::uint64_t total_bytes);

    /// Flashes every target from one archive, in archive order.
    ///
    /// Stops at the first failure by default, because a half-flashed device is
    /// harder to recover than one where the run stopped and said where. The
    /// result lists every target and what happened to it either way.
    std::vector<FlashOutcome> flash_archive(const std::vector<std::uint8_t>& archive_data,
                                            const std::vector<FlashTarget>& targets,
                                            bool continue_on_error = false);

    /// Writes a partition table to the device. This repartitions the flash and
    /// is the one operation that can render a device unbootable on its own.
    void flash_pit(const std::vector<std::uint8_t>& pit_data);

    /// Erases a partition's block range through the file-transfer path, by
    /// sending a sequence of zeroes. Odin has no separate erase command; this is
    /// how the tools do it.
    void erase_partition(std::uint32_t partition_id, std::uint32_t device_type,
                         std::uint64_t total_bytes);

    /// Closes the session.
    void end_session();

    /// Asks the device to reboot into download mode again. Some bootloaders
    /// need this before they will accept a new session.
    void reboot_to_download();

    bool session_open() const noexcept { return m_session_open; }

private:
    void log(const std::string& level, const std::string& message) const;
    void check_cancelled() const;
    void write_packet(const std::vector<std::uint8_t>& packet);
    OdinResponse read_response(OdinControl expected);
    void read_exact_raw(std::uint8_t* buffer, std::size_t size);

    /// Writes one sequence of a file transfer: the data packets, then the
    /// end-of-sequence packet with the payload layout the binary type selects.
    void write_sequence(const FlashTarget& target, const std::uint8_t* data, std::size_t size,
                        std::uint64_t file_size, bool is_last_sequence);

    IByteTransport& m_transport;
    Callbacks m_callbacks;
    bool m_session_open{false};
    bool m_handshake_done{false};
    SessionInfo m_session;
};

/// The Samsung USB ID for download mode, from the VID/PID catalogue.
inline constexpr std::uint16_t kSamsungVid = 0x04E8;
inline constexpr std::uint16_t kDownloadModePid = 0x685D;

}  // namespace huaxin::protocols::samsung
