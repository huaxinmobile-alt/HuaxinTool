#pragma once

// =============================================================================
//  MediaTek Download Agent protocol - the "XFlash" generation.
//
//  PROVENANCE - the command codes, the framing, the parameter layouts and the
//  flash-info structures were transcribed from mtkclient (github.com/bkerler/
//  mtkclient, GPL-3.0): Library/DA/xflash/xflash_lib.py, xflash_param.py,
//  xflash_flash_param.py, Library/DA/storage.py and Library/error.py.
//
//  No mtkclient code was copied. What is reused is the protocol itself.
//
//  WHERE THIS SITS. The bootrom in brom.h is only the front door: it can read
//  and write memory and start a download agent, and nothing else. Flashing is
//  the *agent's* protocol, and it is a different conversation on the same wire.
//  The order is: handshake -> SEND_DA -> JUMP_DA -> everything below.
//
//  Three things about this protocol that a reader coming from the bootrom code
//  will get wrong by assuming symmetry:
//
//    * The framing is LITTLE-endian and carries a MAGIC. The bootrom layer is
//      big-endian with an echo and no magic. Both are the same device on the
//      same cable, one second apart.
//    * Every transfer is a 12-byte header (magic, data type, length) followed
//      by `length` bytes - in both directions. There is no other framing.
//    * A status is not a separate message type. It is a response frame whose
//      payload is 2 or 4 bytes, and 0 means success.
//
//  The data checksum is the low 16 bits of the sum of the payload bytes, which
//  is NOT the XOR checksum the bootrom wants for an agent upload. See
//  compute_da_checksum().
// =============================================================================

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include "protocols/mediatek/brom.h"      // IByteTransport, ProtocolError
#include "protocols/qualcomm/sahara.h"    // ProtocolError

namespace huaxin::protocols::mediatek {

// --- framing -----------------------------------------------------------------
/// The first four bytes of every frame in both directions.
inline constexpr std::uint32_t kDaProtocolMagic = 0xFEEEEEEF;

/// Payload kinds. Only the first is used by the commands implemented here.
inline constexpr std::uint32_t kDaDataTypeProtocolFlow = 1;
inline constexpr std::uint32_t kDaDataTypeMessage = 2;

/// magic + data type + length, all little-endian 32-bit.
inline constexpr std::size_t kDaFrameHeaderSize = 12;

/// A decoded frame. `payload` is empty for a zero-length frame.
struct DaFrame {
    std::uint32_t magic{kDaProtocolMagic};
    std::uint32_t data_type{kDaDataTypeProtocolFlow};
    std::vector<std::uint8_t> payload;
};

/// Builds the 12-byte header for a payload of `length` bytes.
std::vector<std::uint8_t> encode_da_header(std::uint32_t data_type, std::uint32_t length);

/// Header plus payload, ready to write in one call.
std::vector<std::uint8_t> encode_da_frame(const std::vector<std::uint8_t>& payload,
                                          std::uint32_t data_type = kDaDataTypeProtocolFlow);

/// A command frame: the command id as a little-endian 32-bit payload.
std::vector<std::uint8_t> encode_da_command(std::uint32_t command);

/// Decodes a frame header. Throws ProtocolError when the magic is wrong, which
/// means the stream has desynchronised and continuing would read device data as
/// a header.
DaFrame decode_da_header(const std::uint8_t* data, std::size_t size);

/// True when `size` is enough to hold a header.
inline bool is_complete_da_header(std::size_t size) noexcept {
    return size >= kDaFrameHeaderSize;
}

// --- checksum ----------------------------------------------------------------
/// The DA's data checksum: the low 16 bits of the sum of every byte.
///
/// This is deliberately a different function from the bootrom's
/// compute_checksum() in brom.h, which XORs 16-bit little-endian words. The two
/// algorithms belong to two different protocols running on the same device; a
/// checksum computed with the wrong one is accepted by neither.
std::uint16_t compute_da_checksum(const std::uint8_t* data, std::size_t size);
std::uint16_t compute_da_checksum(const std::vector<std::uint8_t>& data);

/// The checksum as the wire carries it: a little-endian 32-bit word.
std::vector<std::uint8_t> encode_da_checksum(std::uint16_t checksum);

// --- commands ----------------------------------------------------------------
/// Command ids, verbatim from mtkclient's xflash Cmd class.
enum class DaCommand : std::uint32_t {
    Unknown = 0x010000,
    Download = 0x010001,
    Upload = 0x010002,
    Format = 0x010003,
    WriteData = 0x010004,
    ReadData = 0x010005,
    FormatPartition = 0x010006,
    Shutdown = 0x010007,
    BootTo = 0x010008,
    DeviceCtrl = 0x010009,
    InitExtRam = 0x01000A,
    SwitchUsbSpeed = 0x01000B,

    SetupEnvironment = 0x010100,
    SetupHwInitParams = 0x010101,

    SetBmtPercentage = 0x020001,
    SetBatteryOpt = 0x020002,
    SetChecksumLevel = 0x020003,
    SetResetKey = 0x020004,
    SetHostInfo = 0x020005,
    SetMetaBootMode = 0x020006,
    SetGenerateGpx = 0x020008,
    SetRegisterValue = 0x020009,
    SetExternalSig = 0x02000A,
    SetRemoteSecPolicy = 0x02000B,
    SetUfsConfig = 0x020011,

    GetEmmcInfo = 0x040001,
    GetNandInfo = 0x040002,
    GetNorInfo = 0x040003,
    GetUfsInfo = 0x040004,
    GetDaVersion = 0x040005,
    GetExpireData = 0x040006,
    GetPacketLength = 0x040007,
    GetRandomId = 0x040008,
    GetPartitionTblCata = 0x040009,
    GetConnectionAgent = 0x04000A,
    GetUsbSpeed = 0x04000B,
    GetRamInfo = 0x04000C,
    GetChipId = 0x04000D,
    GetBatteryVoltage = 0x04000F,
    GetRpmbStatus = 0x040010,
    GetDevFwInfo = 0x040013,
    GetHrid = 0x040014,
    SlaEnabledStatus = 0x040016,

    StartDlInfo = 0x080001,
    EndDlInfo = 0x080002,
    CcOptionalDownloadAct = 0x080005,
};

/// The command ids that are not on the command path: they are sent *inside* a
/// DeviceCtrl exchange.
enum class DaControlCode : std::uint32_t {
    SetBmtPercentage = 0x020001,
    SetBatteryOpt = 0x020002,
    SetChecksumLevel = 0x020003,
    SetResetKey = 0x020004,
    SetHostInfo = 0x020005,
    SetMetaBootMode = 0x020006,
    SetEmmcHwresetPin = 0x020007,
    SetRemoteSecPolicy = 0x02000B,

    GetEmmcInfo = 0x040001,
    GetNandInfo = 0x040002,
    GetNorInfo = 0x040003,
    GetUfsInfo = 0x040004,
    GetDaVersion = 0x040005,
    GetPacketLength = 0x040007,
    GetRandomId = 0x040008,
    GetConnectionAgent = 0x04000A,
    GetUsbSpeed = 0x04000B,
    GetRamInfo = 0x04000C,
    GetChipId = 0x04000D,
    GetBatteryVoltage = 0x04000F,
    GetRpmbStatus = 0x040010,
    GetDevFwInfo = 0x040013,
    GetHrid = 0x040014,
    GetErrorDetail = 0x040015,
    SlaEnabledStatus = 0x040016,

    DeviceCtrlReadRegister = 0x0E0003,
    CtrlStorageTest = 0x0E0001,
    CtrlRamTest = 0x0E0002,
};

/// SYNC_SIGNAL, which is spelled as ASCII "SYNC" in little-endian.
inline constexpr std::uint32_t kDaSyncSignal = 0x434E5953;

const char* to_string(DaCommand command) noexcept;

// --- storage -----------------------------------------------------------------
/// Storage types, verbatim from mtkclient's DaStorage class.
enum class DaStorage : std::uint32_t {
    Emmc = 0x1,
    Sdmmc = 0x2,
    Nand = 0x10,
    NandSlc = 0x11,
    NandMlc = 0x12,
    NandTlc = 0x13,
    NandAmlc = 0x14,
    NandSpi = 0x15,
    Nor = 0x20,
    NorSerial = 0x21,
    NorParallel = 0x22,
    Ufs = 0x30,
};

const char* to_string(DaStorage storage) noexcept;

/// eMMC hardware partitions, verbatim from mtkclient's EmmcPartitionType.
enum class EmmcPartition : std::uint32_t {
    Boot1 = 1,
    Boot2 = 2,
    Rpmb = 3,
    Gp1 = 4,
    Gp2 = 5,
    Gp3 = 6,
    Gp4 = 7,
    User = 8,
    End = 9,
};

const char* to_string(EmmcPartition partition) noexcept;

/// Maps a scatter file's `storage:`/`region:` spelling onto a storage type.
///
/// Returns false for a spelling this build does not know, so an unrecognised
/// package is reported rather than silently written to the wrong medium. The
/// spellings below are the ones that appear in the scattered packages this tool
/// was written against; a miss is not an error.
bool parse_storage_name(const std::string& text, DaStorage& out);
/// Maps a scatter `region:` name onto an eMMC partition. "EMMC_USER" and an
/// empty region both mean the user area.
bool parse_region_name(const std::string& text, EmmcPartition& out);

// --- status ------------------------------------------------------------------
/// Decodes a status frame payload.
///
/// A 2-byte payload is a 16-bit status, a 4-byte payload a 32-bit one, and in
/// the 4-byte case the protocol magic itself means success - transcribed from
/// mtkclient's xflash status(). Anything longer is read as 32-bit words and the
/// first one is the status.
std::uint32_t decode_da_status(const std::vector<std::uint8_t>& payload);

/// Names for the status codes this tool can encounter, from mtkclient's
/// ErrorCodes_XFlash table. Unknown codes are reported as a number rather than
/// guessed at.
std::string describe_da_status(std::uint32_t status);

/// True for the two flow-control statuses FORMAT returns while it works.
inline constexpr std::uint32_t kDaStatusContinue = 0x40040004;
inline constexpr std::uint32_t kDaStatusComplete = 0x40040005;

/// A device that refuses writes because its security policy forbids them.
inline constexpr std::uint32_t kDaStatusDlForbidden = 0xc0020004;
/// Anti-rollback: the image is older than what is already on the device.
inline constexpr std::uint32_t kDaStatusAntiRollback = 0xc002002d;
/// The command is not supported by this agent version.
inline constexpr std::uint32_t kDaStatusUnsupportedCommand = 0xc0010003;

// --- flash geometry ----------------------------------------------------------
/// What the agent reports about the eMMC it is driving.
///
/// Layout from mtkclient's get_emmc_info(): two 32-bit words, then eight 64-bit
/// sizes, then a 16-byte CID and a 64-bit firmware version.
struct EmmcInfo {
    std::uint32_t type{0};
    std::uint32_t block_size{0};
    std::uint64_t boot1_size{0};
    std::uint64_t boot2_size{0};
    std::uint64_t rpmb_size{0};
    std::uint64_t gp1_size{0};
    std::uint64_t gp2_size{0};
    std::uint64_t gp3_size{0};
    std::uint64_t gp4_size{0};
    std::uint64_t user_size{0};
    std::vector<std::uint8_t> cid;
    std::uint64_t firmware_version{0};

    /// The area a partition lives in unless it says otherwise.
    std::uint64_t user_capacity() const noexcept { return user_size; }
};

/// The byte count mtkclient reads for an eMMC info reply.
inline constexpr std::size_t kEmmcInfoSize = 8 + 8 * 8 + 16 + 8;

/// Parses an eMMC info payload. Throws ProtocolError when it is too short.
EmmcInfo parse_emmc_info(const std::vector<std::uint8_t>& data);

struct NandInfo {
    std::uint32_t type{0};
    std::uint32_t page_size{0};
    std::uint32_t block_size{0};
    std::uint32_t spare_size{0};
    std::uint64_t total_size{0};
    std::uint64_t available_size{0};
    std::uint32_t bmt_exist{0};
};

/// Parses a NAND info payload. Layout from mtkclient's get_nand_info().
NandInfo parse_nand_info(const std::vector<std::uint8_t>& data);

struct NorInfo {
    std::uint32_t type{0};
    std::uint64_t available_size{0};
};

/// Parses a NOR info payload. Layout from mtkclient's get_nor_info().
NorInfo parse_nor_info(const std::vector<std::uint8_t>& data);

struct RamRegion {
    std::uint32_t type{0};
    std::uint64_t base_address{0};
    std::uint64_t size{0};
};

/// The agent reports SRAM and DRAM together, as either 32-bit or 64-bit fields.
struct RamInfo {
    RamRegion sram;
    RamRegion dram;
    bool is_64bit{false};
};

/// Parses a RAM info payload: 24 bytes of 32-bit fields or 48 of 64-bit ones.
RamInfo parse_ram_info(const std::vector<std::uint8_t>& data);

struct ChipId {
    std::uint16_t hw_code{0};
    std::uint16_t hw_sub_code{0};
    std::uint16_t hw_version{0};
    std::uint16_t sw_version{0};
    std::uint16_t chip_evolution{0};
};

/// Parses the chip id payload: five little-endian 16-bit fields.
ChipId parse_chip_id(const std::vector<std::uint8_t>& data);

/// The transfer sizes the agent says it wants. Both are advisory: the agent
/// accepts smaller chunks, it just works harder.
struct PacketLengths {
    std::uint32_t write_packet_length{0};
    std::uint32_t read_packet_length{0};
};

PacketLengths parse_packet_length(const std::vector<std::uint8_t>& data);

// --- the NAND extension block ------------------------------------------------
/// Every command that touches storage carries these eight words after the
/// address and length. For eMMC and UFS every one of them is zero, which is
/// what mtkclient's NandExtension defaults to; they exist for raw NAND, where
/// they select the cell usage, address type and format level.
struct NandExtension {
    std::uint32_t cell_usage{0};
    std::uint32_t addr_type{0};
    std::uint32_t bin_type{0};
    std::uint32_t region{0};
    std::uint32_t format_level{0};
    std::uint32_t sys_slc_percent{0};
    std::uint32_t usr_slc_percent{0};
    std::uint32_t phy_max_size{0};
};

/// The parameter block for READ_DATA, WRITE_DATA and FORMAT:
/// storage, partition, address, length, then the eight NAND extension words.
inline constexpr std::size_t kDaRegionParamSize = 8 + 8 + 8 + 32;

std::vector<std::uint8_t> encode_region_param(DaStorage storage, std::uint32_t partition,
                                              std::uint64_t address, std::uint64_t length,
                                              const NandExtension& extension = {});

/// Decodes a region parameter block. Exposed so the encoding can be checked
/// against the recorded layout without a device.
void decode_region_param(const std::vector<std::uint8_t>& data, DaStorage& storage,
                         std::uint32_t& partition, std::uint64_t& address, std::uint64_t& length);

// --- session -----------------------------------------------------------------
class DaSession {
public:
    struct Callbacks {
        std::function<void(const std::string& level, const std::string& message)> log;
        std::function<void(int percent, const std::string& message)> progress;
        std::function<bool()> cancelled;
    };

    DaSession(IByteTransport& transport, Callbacks callbacks);

    // -- framing -----------------------------------------------------------

    /// Writes one frame.
    void send_frame(const std::vector<std::uint8_t>& payload);

    /// Writes one command frame (the command id as its payload).
    void send_command(DaCommand command);

    /// Reads one frame of any kind.
    DaFrame read_frame(unsigned int timeout_ms);

    /// Reads one frame and decodes it as a status. Throws ProtocolError when
    /// the agent reports a failure, naming the code.
    void expect_status(const std::string& what, unsigned int timeout_ms);

    /// Reads one frame and decodes its status *without* treating a non-zero
    /// code as an error, for the calls where a refusal is a normal answer.
    std::uint32_t read_status(unsigned int timeout_ms);

    // -- device control ----------------------------------------------------

    /// DeviceCtrl(code) followed by the optional parameter, returning whatever
    /// payload the agent answered with (empty when the code takes none).
    ///
    /// This is the shape most of the protocol uses: a query is a DeviceCtrl
    /// carrying a control code, and the answer is a data frame followed by a
    /// status frame.
    std::vector<std::uint8_t> device_control(DaControlCode code,
                                             const std::vector<std::uint8_t>& parameter = {});

    /// Same, but returns the trailing status instead of throwing on failure.
    std::vector<std::uint8_t> device_control(DaControlCode code,
                                            const std::vector<std::uint8_t>& parameter,
                                            std::uint32_t& status);

    // -- agent setup -------------------------------------------------------

    /// SyncSignal. Cheap, and worth sending before the first real command.
    void sync_signal();

    /// SetupEnvironment: tells the agent where to send its own logs.
    void setup_environment(unsigned int uart_log_level = 0, unsigned int log_channel = 2,
                           unsigned int system_os = 1);

    /// SetupHwInitParams with "no config", which is what mtkclient sends.
    void setup_hw_init();

    /// SetChecksumLevel. Levels are none/usb/storage/both.
    void set_checksum_level(unsigned int level);

    /// SetResetKey. 0 = none, 0x50 = one, 0x68 = two (mtkclient's default).
    void set_reset_key(unsigned int key = 0x68);

    /// InitExtRam: uploads an EMI/DRAM configuration blob.
    ///
    /// Only needed when the agent cannot initialise DRAM on its own. The blob
    /// layout is chip-specific and is supplied by the caller; this method does
    /// not invent one.
    void init_external_ram(const std::vector<std::uint8_t>& emi);

    // -- queries -----------------------------------------------------------

    DaCommand get_da_version(std::string& version_text);

    /// "brom" or "preloader", as the agent reports which stage it took over
    /// from. Empty when the agent does not answer.
    std::string get_connection_agent();

    ChipId get_chip_id();
    EmmcInfo get_emmc_info();
    NandInfo get_nand_info();
    NorInfo get_nor_info();
    RamInfo get_ram_info();
    PacketLengths get_packet_length();

    /// True when the agent says SLA is enabled, i.e. this is a secure-boot
    /// device. Read-only: this build never performs the signed handshake.
    bool sla_enabled();

    // -- storage operations ------------------------------------------------

    /// Erases a region. The agent answers Continue while it works and Complete
    /// when it is done; both are handled here, and the caller waits.
    void format_region(DaStorage storage, std::uint32_t partition, std::uint64_t address,
                       std::uint64_t length, const NandExtension& extension = {});

    /// Writes `data` to a region, in packets the agent negotiated.
    ///
    /// Returns the number of bytes written, which is `data.size()` padded up to
    /// a 512-byte boundary - the protocol writes whole sectors, and the padding
    /// is zeros.
    std::uint64_t write_region(DaStorage storage, std::uint32_t partition,
                               std::uint64_t address, const std::vector<std::uint8_t>& data,
                               std::uint32_t packet_size = 0);

    /// Reads `length` bytes from a region.
    std::vector<std::uint8_t> read_region(DaStorage storage, std::uint32_t partition,
                                          std::uint64_t address, std::uint64_t length,
                                          std::uint32_t packet_size = 0);

    /// Shutdown with the given mode. The device may reset, so the link is not
    /// assumed usable afterwards.
    void shutdown(unsigned int mode = 0);

private:
    void log(const std::string& level, const std::string& message) const;
    void check_cancelled() const;
    void write(const std::vector<std::uint8_t>& data, const std::string& what);
    void write_frame(const std::vector<std::uint8_t>& payload, const std::string& what);
    void send_parameter(const std::vector<std::uint8_t>& parameter, const std::string& what);

    /// Reads a data frame and the status frame that follows it, as the query
    /// commands answer.
    std::vector<std::uint8_t> read_answer(const std::string& what);

    IByteTransport& m_transport;
    Callbacks m_callbacks;
    /// What the agent says it wants per transfer; 0 means "not asked yet".
    std::uint32_t m_write_packet{0};
    std::uint32_t m_read_packet{0};
};

// --- timeouts ----------------------------------------------------------------
/// A plain command: the agent answers or it does not.
inline constexpr unsigned int kDaCommandTimeoutMs = 5000;
/// A data transfer, per packet.
inline constexpr unsigned int kDaDataTimeoutMs = 30000;
/// Erase and format run as long as a write and report progress while they do.
inline constexpr unsigned int kDaFormatTimeoutMs = 120000;

/// How much is written or read per parameter block when the agent has not said.
///
/// 1 MiB is mtkclient's fallback and matches what the agents it talks to
/// accept; the real value comes from GET_PACKET_LENGTH when the agent answers.
inline constexpr std::uint32_t kDaFallbackPacketSize = 0x100000;

/// Writes are padded to this, because the protocol writes whole sectors.
inline constexpr std::size_t kDaSectorSize = 512;

}  // namespace huaxin::protocols::mediatek
