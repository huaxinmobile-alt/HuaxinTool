#include "protocols/mediatek/da.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <map>
#include <thread>
#include <utility>

namespace huaxin::protocols::mediatek {

using protocols::qualcomm::ProtocolError;

namespace {

// The DA layer is little-endian, the opposite of the bootrom layer in brom.cpp.
// The two are on the same wire, one second apart, so the helpers are spelled out
// in both files rather than shared: a shared "read_word" would hide the single
// most expensive mistake in this protocol.
void put_le32(std::vector<std::uint8_t>& out, std::uint32_t value) {
    out.push_back(static_cast<std::uint8_t>(value & 0xFF));
    out.push_back(static_cast<std::uint8_t>((value >> 8) & 0xFF));
    out.push_back(static_cast<std::uint8_t>((value >> 16) & 0xFF));
    out.push_back(static_cast<std::uint8_t>((value >> 24) & 0xFF));
}

std::uint16_t read_le16(const std::uint8_t* data) {
    return static_cast<std::uint16_t>(data[0] | (data[1] << 8));
}

std::uint32_t read_le32(const std::uint8_t* data) {
    return static_cast<std::uint32_t>(data[0]) | (static_cast<std::uint32_t>(data[1]) << 8)
           | (static_cast<std::uint32_t>(data[2]) << 16) | (static_cast<std::uint32_t>(data[3]) << 24);
}

std::uint64_t read_le64(const std::uint8_t* data) {
    std::uint64_t value = 0;
    for (int index = 7; index >= 0; --index) {
        value = (value << 8) | data[index];
    }
    return value;
}

std::string hex32(std::uint32_t value) {
    char buffer[16];
    std::snprintf(buffer, sizeof(buffer), "0x%08x", value);
    return buffer;
}

/// Reads `size` bytes out of `data` at `offset`, refusing to run off the end.
/// Every structure parser below goes through this, so a short reply is a clear
/// error rather than a read past the buffer.
const std::uint8_t* slice(const std::vector<std::uint8_t>& data, std::size_t offset,
                          std::size_t size, const char* what) {
    if (data.size() < offset + size) {
        throw ProtocolError(std::string("the agent's ") + what + " reply is "
                            + std::to_string(data.size()) + " bytes, shorter than the "
                            + std::to_string(offset + size) + " the structure needs");
    }
    return data.data() + offset;
}

/// The status table, limited to the codes this tool can actually act on or
/// explain. Transcribed from mtkclient's ErrorCodes_XFlash. Anything not here
/// is reported as a number: guessing at an unknown code is worse than showing it.
const std::map<std::uint32_t, const char*>& status_names() {
    static const std::map<std::uint32_t, const char*> names = {
        {0x0, "OK"},
        {0xc0010001, "error"},
        {0xc0010002, "aborted"},
        {0xc0010003, "unsupported command"},
        {0xc0010004, "unsupported control code"},
        {0xc0010005, "protocol error"},
        {0xc0010006, "protocol buffer overflow"},
        {0xc0010007, "insufficient buffer"},
        {0xc0010009, "invalid host session"},
        {0xc001000a, "invalid session"},
        {0xc001000b, "invalid stage"},
        {0xc001000c, "not implemented"},
        {0xc001000d, "file not found"},
        {0xc0020001, "ROM info not found"},
        {0xc0020003, "device not supported"},
        {0xc0020004, "download forbidden by the device's security policy"},
        {0xc0020005, "image too large"},
        {0xc0020007, "image verify failed"},
        {0xc002000c, "write data not allowed"},
        {0xc002000d, "format not allowed"},
        {0xc002000e, "SV5 public key authentication failed"},
        {0xc002002d, "anti-rollback violation: the image is older than the device allows"},
        {0xc002004c, "download agent anti-rollback error"},
        {0xc0030002, "DA file invalid"},
        {0xc0030003, "DA selection error"},
        {0xc0040050, "EMI setting version error"},
        {0xc0070004, "DA hash mismatch"},
        {0x40040004, "in progress"},
        {0x40040005, "complete"},
    };
    return names;
}

/// A region name with its punctuation and case removed, so "EMMC_USER",
/// "emmc user" and "EMMC-USER" all compare equal.
std::string normalise(const std::string& text) {
    std::string out;
    out.reserve(text.size());
    for (const char character : text) {
        const unsigned char value = static_cast<unsigned char>(character);
        if (std::isalnum(value) != 0) {
            out.push_back(static_cast<char>(std::toupper(value)));
        }
    }
    return out;
}

}  // namespace

// --- framing -----------------------------------------------------------------
std::vector<std::uint8_t> encode_da_header(std::uint32_t data_type, std::uint32_t length) {
    std::vector<std::uint8_t> out;
    out.reserve(kDaFrameHeaderSize);
    put_le32(out, kDaProtocolMagic);
    put_le32(out, data_type);
    put_le32(out, length);
    return out;
}

std::vector<std::uint8_t> encode_da_frame(const std::vector<std::uint8_t>& payload,
                                          std::uint32_t data_type) {
    std::vector<std::uint8_t> out = encode_da_header(data_type,
                                                     static_cast<std::uint32_t>(payload.size()));
    out.insert(out.end(), payload.begin(), payload.end());
    return out;
}

std::vector<std::uint8_t> encode_da_command(std::uint32_t command) {
    std::vector<std::uint8_t> payload;
    put_le32(payload, command);
    return encode_da_frame(payload);
}

DaFrame decode_da_header(const std::uint8_t* data, std::size_t size) {
    if (size < kDaFrameHeaderSize) {
        throw ProtocolError("a download agent frame header is 12 bytes; the buffer holds "
                            + std::to_string(size));
    }
    DaFrame frame;
    frame.magic = read_le32(data);
    frame.data_type = read_le32(data + 4);
    const std::uint32_t length = read_le32(data + 8);
    if (frame.magic != kDaProtocolMagic) {
        char buffer[128];
        std::snprintf(buffer, sizeof(buffer),
                      "the download agent sent 0x%08x where the frame magic 0x%08x was expected: "
                      "the stream has desynchronised",
                      frame.magic, kDaProtocolMagic);
        throw ProtocolError(buffer);
    }
    // A frame length is bounded by what a caller is willing to hold, and the
    // protocol's real maximum is far below this. Without a bound, a corrupt
    // length field asks for a multi-gigabyte allocation.
    constexpr std::uint32_t kMaxFrame = 64u * 1024u * 1024u;
    if (length > kMaxFrame) {
        throw ProtocolError("the download agent announced a " + std::to_string(length)
                            + " byte frame, above the " + std::to_string(kMaxFrame)
                            + " byte ceiling this build accepts");
    }
    frame.payload.resize(length);
    return frame;
}

// --- checksum ----------------------------------------------------------------
std::uint16_t compute_da_checksum(const std::uint8_t* data, std::size_t size) {
    std::uint32_t sum = 0;
    for (std::size_t index = 0; index < size; ++index) {
        sum += data[index];
    }
    return static_cast<std::uint16_t>(sum & 0xFFFF);
}

std::uint16_t compute_da_checksum(const std::vector<std::uint8_t>& data) {
    return compute_da_checksum(data.data(), data.size());
}

std::vector<std::uint8_t> encode_da_checksum(std::uint16_t checksum) {
    std::vector<std::uint8_t> out;
    put_le32(out, checksum);
    return out;
}

// --- names -------------------------------------------------------------------
const char* to_string(DaCommand command) noexcept {
    switch (command) {
        case DaCommand::Download:               return "DOWNLOAD";
        case DaCommand::Upload:                 return "UPLOAD";
        case DaCommand::Format:                 return "FORMAT";
        case DaCommand::WriteData:              return "WRITE_DATA";
        case DaCommand::ReadData:               return "READ_DATA";
        case DaCommand::FormatPartition:        return "FORMAT_PARTITION";
        case DaCommand::Shutdown:               return "SHUTDOWN";
        case DaCommand::BootTo:                 return "BOOT_TO";
        case DaCommand::DeviceCtrl:             return "DEVICE_CTRL";
        case DaCommand::InitExtRam:             return "INIT_EXT_RAM";
        case DaCommand::SwitchUsbSpeed:         return "SWITCH_USB_SPEED";
        case DaCommand::SetupEnvironment:       return "SETUP_ENVIRONMENT";
        case DaCommand::SetupHwInitParams:      return "SETUP_HW_INIT_PARAMS";
        case DaCommand::GetEmmcInfo:            return "GET_EMMC_INFO";
        case DaCommand::GetNandInfo:            return "GET_NAND_INFO";
        case DaCommand::GetNorInfo:             return "GET_NOR_INFO";
        case DaCommand::GetUfsInfo:             return "GET_UFS_INFO";
        case DaCommand::GetDaVersion:           return "GET_DA_VERSION";
        case DaCommand::GetPacketLength:        return "GET_PACKET_LENGTH";
        case DaCommand::GetConnectionAgent:     return "GET_CONNECTION_AGENT";
        case DaCommand::GetChipId:              return "GET_CHIP_ID";
        case DaCommand::GetRamInfo:             return "GET_RAM_INFO";
        case DaCommand::SlaEnabledStatus:       return "SLA_ENABLED_STATUS";
        case DaCommand::StartDlInfo:            return "START_DL_INFO";
        case DaCommand::EndDlInfo:              return "END_DL_INFO";
        case DaCommand::CcOptionalDownloadAct:  return "CC_OPTIONAL_DOWNLOAD_ACT";
        default:                                return "UNKNOWN";
    }
}

const char* to_string(DaStorage storage) noexcept {
    switch (storage) {
        case DaStorage::Emmc:        return "eMMC";
        case DaStorage::Sdmmc:       return "SD/MMC";
        case DaStorage::Nand:        return "NAND";
        case DaStorage::NandSlc:     return "NAND SLC";
        case DaStorage::NandMlc:     return "NAND MLC";
        case DaStorage::NandTlc:     return "NAND TLC";
        case DaStorage::NandAmlc:    return "NAND aMLC";
        case DaStorage::NandSpi:     return "SPI NAND";
        case DaStorage::Nor:         return "NOR";
        case DaStorage::NorSerial:   return "serial NOR";
        case DaStorage::NorParallel: return "parallel NOR";
        case DaStorage::Ufs:         return "UFS";
    }
    return "unknown";
}

const char* to_string(EmmcPartition partition) noexcept {
    switch (partition) {
        case EmmcPartition::Boot1: return "boot1";
        case EmmcPartition::Boot2: return "boot2";
        case EmmcPartition::Rpmb:  return "rpmb";
        case EmmcPartition::Gp1:   return "GP1";
        case EmmcPartition::Gp2:   return "GP2";
        case EmmcPartition::Gp3:   return "GP3";
        case EmmcPartition::Gp4:   return "GP4";
        case EmmcPartition::User:  return "user";
        case EmmcPartition::End:   return "end";
    }
    return "unknown";
}

bool parse_storage_name(const std::string& text, DaStorage& out) {
    const std::string key = normalise(text);
    if (key.empty()) {
        return false;
    }
    static const std::map<std::string, DaStorage> names = {
        {"EMMC", DaStorage::Emmc},
        {"HWSTORAGEEMMC", DaStorage::Emmc},
        {"SDMMC", DaStorage::Sdmmc},
        {"SDCARD", DaStorage::Sdmmc},
        {"UFS", DaStorage::Ufs},
        {"HWSTORAGEUFS", DaStorage::Ufs},
        {"NAND", DaStorage::Nand},
        {"HWSTORAGENAND", DaStorage::Nand},
        {"NANDSLC", DaStorage::NandSlc},
        {"NANDMLC", DaStorage::NandMlc},
        {"NANDTLC", DaStorage::NandTlc},
        {"NANDSPI", DaStorage::NandSpi},
        {"NOR", DaStorage::Nor},
        {"SPINOR", DaStorage::Nor},
        {"HWSTORAGENOR", DaStorage::Nor},
        {"NORSERIAL", DaStorage::NorSerial},
        {"NORPARALLEL", DaStorage::NorParallel},
    };
    const auto entry = names.find(key);
    if (entry == names.end()) {
        return false;
    }
    out = entry->second;
    return true;
}

bool parse_region_name(const std::string& text, EmmcPartition& out) {
    const std::string key = normalise(text);
    // An absent region means the user area, which is the common case and what
    // every scatter file with no `region:` line intends.
    if (key.empty() || key == "EMMCUSER" || key == "USER" || key == "EMMC" || key == "UFS") {
        out = EmmcPartition::User;
        return true;
    }
    static const std::map<std::string, EmmcPartition> names = {
        {"EMMCBOOT1", EmmcPartition::Boot1}, {"BOOT1", EmmcPartition::Boot1},
        {"EMMCBOOT2", EmmcPartition::Boot2}, {"BOOT2", EmmcPartition::Boot2},
        {"EMMCRPMB", EmmcPartition::Rpmb},   {"RPMB", EmmcPartition::Rpmb},
        {"EMMCGP1", EmmcPartition::Gp1},     {"GP1", EmmcPartition::Gp1},
        {"EMMCGP2", EmmcPartition::Gp2},     {"GP2", EmmcPartition::Gp2},
        {"EMMCGP3", EmmcPartition::Gp3},     {"GP3", EmmcPartition::Gp3},
        {"EMMCGP4", EmmcPartition::Gp4},     {"GP4", EmmcPartition::Gp4},
    };
    const auto entry = names.find(key);
    if (entry == names.end()) {
        return false;
    }
    out = entry->second;
    return true;
}

// --- status ------------------------------------------------------------------
std::uint32_t decode_da_status(const std::vector<std::uint8_t>& payload) {
    if (payload.size() == 2) {
        return read_le16(payload.data());
    }
    if (payload.size() == 4) {
        const std::uint32_t value = read_le32(payload.data());
        // A four-byte frame carrying the magic is the agent's way of saying OK.
        return value == kDaProtocolMagic ? 0u : value;
    }
    if (payload.size() >= 4) {
        // Longer frames are word arrays; the first word is the status.
        return read_le32(payload.data());
    }
    // An empty payload is read as success. mtkclient indexes into an unpack of
    // zero words here and would raise; treating it as OK keeps a chatty agent
    // from failing a command it actually completed.
    return 0;
}

std::string describe_da_status(std::uint32_t status) {
    const auto& names = status_names();
    const auto entry = names.find(status);
    if (entry != names.end()) {
        return std::string(entry->second) + " (" + hex32(status) + ")";
    }
    return "unknown status " + hex32(status);
}

// --- structures --------------------------------------------------------------
EmmcInfo parse_emmc_info(const std::vector<std::uint8_t>& data) {
    EmmcInfo info;
    info.type = read_le32(slice(data, 0, 4, "eMMC info"));
    info.block_size = read_le32(slice(data, 4, 4, "eMMC info"));
    info.boot1_size = read_le64(slice(data, 8, 8, "eMMC info"));
    info.boot2_size = read_le64(slice(data, 16, 8, "eMMC info"));
    info.rpmb_size = read_le64(slice(data, 24, 8, "eMMC info"));
    info.gp1_size = read_le64(slice(data, 32, 8, "eMMC info"));
    info.gp2_size = read_le64(slice(data, 40, 8, "eMMC info"));
    info.gp3_size = read_le64(slice(data, 48, 8, "eMMC info"));
    info.gp4_size = read_le64(slice(data, 56, 8, "eMMC info"));
    info.user_size = read_le64(slice(data, 64, 8, "eMMC info"));
    const std::uint8_t* cid = slice(data, 72, 16, "eMMC info");
    info.cid.assign(cid, cid + 16);
    info.firmware_version = read_le64(slice(data, 88, 8, "eMMC info"));
    return info;
}

NandInfo parse_nand_info(const std::vector<std::uint8_t>& data) {
    // The NAND reply's exact field order is not confirmed against a primary
    // source in this build - mtkclient parses it positionally without naming the
    // layout, so the offsets below are inferred from the field widths it reads.
    // TODO: verify against a recorded NAND device reply before trusting it.
    NandInfo info;
    if (data.size() < 4) {
        throw ProtocolError("the agent's NAND info reply is " + std::to_string(data.size())
                            + " bytes, too short to hold a type");
    }
    info.type = read_le32(data.data());
    if (data.size() >= 8) {
        info.page_size = read_le32(data.data() + 4);
    }
    if (data.size() >= 12) {
        info.block_size = read_le32(data.data() + 8);
    }
    if (data.size() >= 16) {
        info.spare_size = read_le32(data.data() + 12);
    }
    if (data.size() >= 24) {
        info.total_size = read_le64(data.data() + 16);
    }
    if (data.size() >= 32) {
        info.available_size = read_le64(data.data() + 24);
    }
    return info;
}

NorInfo parse_nor_info(const std::vector<std::uint8_t>& data) {
    // Same caveat as the NAND layout above.
    // TODO: verify against a recorded NOR device reply before trusting it.
    NorInfo info;
    if (data.size() < 4) {
        throw ProtocolError("the agent's NOR info reply is " + std::to_string(data.size())
                            + " bytes, too short to hold a type");
    }
    info.type = read_le32(data.data());
    if (data.size() >= 12) {
        info.available_size = read_le64(data.data() + 4);
    } else if (data.size() >= 8) {
        info.available_size = read_le32(data.data() + 4);
    }
    return info;
}

RamInfo parse_ram_info(const std::vector<std::uint8_t>& data) {
    RamInfo info;
    if (data.size() == 24) {
        info.sram.type = read_le32(data.data());
        info.sram.base_address = read_le32(data.data() + 4);
        info.sram.size = read_le32(data.data() + 8);
        info.dram.type = read_le32(data.data() + 12);
        info.dram.base_address = read_le32(data.data() + 16);
        info.dram.size = read_le32(data.data() + 20);
        return info;
    }
    if (data.size() == 48) {
        info.is_64bit = true;
        info.sram.type = read_le32(data.data());
        info.sram.base_address = read_le64(data.data() + 8);
        info.sram.size = read_le64(data.data() + 16);
        info.dram.type = read_le32(data.data() + 24);
        info.dram.base_address = read_le64(data.data() + 32);
        info.dram.size = read_le64(data.data() + 40);
        return info;
    }
    throw ProtocolError("the agent's RAM info reply is " + std::to_string(data.size())
                        + " bytes; the protocol defines a 24-byte and a 48-byte form and "
                          "nothing else");
}

ChipId parse_chip_id(const std::vector<std::uint8_t>& data) {
    ChipId id;
    id.hw_code = read_le16(slice(data, 0, 2, "chip id"));
    id.hw_sub_code = read_le16(slice(data, 2, 2, "chip id"));
    id.hw_version = read_le16(slice(data, 4, 2, "chip id"));
    id.sw_version = read_le16(slice(data, 6, 2, "chip id"));
    id.chip_evolution = read_le16(slice(data, 8, 2, "chip id"));
    return id;
}

PacketLengths parse_packet_length(const std::vector<std::uint8_t>& data) {
    PacketLengths lengths;
    lengths.write_packet_length = read_le32(slice(data, 0, 4, "packet length"));
    lengths.read_packet_length = read_le32(slice(data, 4, 4, "packet length"));
    return lengths;
}

std::vector<std::uint8_t> encode_region_param(DaStorage storage, std::uint32_t partition,
                                              std::uint64_t address, std::uint64_t length,
                                              const NandExtension& extension) {
    std::vector<std::uint8_t> out;
    out.reserve(kDaRegionParamSize);
    put_le32(out, static_cast<std::uint32_t>(storage));
    put_le32(out, partition);
    put_le32(out, static_cast<std::uint32_t>(address & 0xFFFFFFFFu));
    put_le32(out, static_cast<std::uint32_t>((address >> 32) & 0xFFFFFFFFu));
    put_le32(out, static_cast<std::uint32_t>(length & 0xFFFFFFFFu));
    put_le32(out, static_cast<std::uint32_t>((length >> 32) & 0xFFFFFFFFu));
    put_le32(out, extension.cell_usage);
    put_le32(out, extension.addr_type);
    put_le32(out, extension.bin_type);
    put_le32(out, extension.region);
    put_le32(out, extension.format_level);
    put_le32(out, extension.sys_slc_percent);
    put_le32(out, extension.usr_slc_percent);
    put_le32(out, extension.phy_max_size);
    return out;
}

void decode_region_param(const std::vector<std::uint8_t>& data, DaStorage& storage,
                         std::uint32_t& partition, std::uint64_t& address, std::uint64_t& length) {
    if (data.size() < kDaRegionParamSize) {
        throw ProtocolError("a region parameter block is " + std::to_string(kDaRegionParamSize)
                            + " bytes; the buffer holds " + std::to_string(data.size()));
    }
    storage = static_cast<DaStorage>(read_le32(data.data()));
    partition = read_le32(data.data() + 4);
    address = read_le64(data.data() + 8);
    length = read_le64(data.data() + 16);
}

// --- session -----------------------------------------------------------------
DaSession::DaSession(IByteTransport& transport, Callbacks callbacks)
    : m_transport(transport), m_callbacks(std::move(callbacks)) {}

void DaSession::log(const std::string& level, const std::string& message) const {
    if (m_callbacks.log) {
        m_callbacks.log(level, message);
    }
}

void DaSession::check_cancelled() const {
    if (m_callbacks.cancelled && m_callbacks.cancelled()) {
        throw ProtocolError("cancelled by the operator");
    }
}

void DaSession::write(const std::vector<std::uint8_t>& data, const std::string& what) {
    // write_all throws rather than returning a failure, so the only thing left
    // to add here is what the host was doing when it gave up.
    try {
        m_transport.write_all(data.data(), data.size(), kDaDataTimeoutMs);
    } catch (const ProtocolError& error) {
        throw ProtocolError(std::string("the link failed while sending ") + what + ": "
                            + error.what());
    }
}

void DaSession::write_frame(const std::vector<std::uint8_t>& payload, const std::string& what) {
    write(encode_da_frame(payload), what);
}

void DaSession::send_frame(const std::vector<std::uint8_t>& payload) {
    write_frame(payload, "a frame");
}

void DaSession::send_command(DaCommand command) {
    write(encode_da_command(static_cast<std::uint32_t>(command)), "a command frame");
    if (m_callbacks.log) {
        m_callbacks.log("debug", std::string("DA -> ") + to_string(command));
    }
}

DaFrame DaSession::read_frame(unsigned int timeout_ms) {
    std::uint8_t header[kDaFrameHeaderSize];
    m_transport.read_exact(header, sizeof(header), timeout_ms);
    DaFrame frame = decode_da_header(header, sizeof(header));
    if (!frame.payload.empty()) {
        m_transport.read_exact(frame.payload.data(), frame.payload.size(), timeout_ms);
    }
    return frame;
}

std::uint32_t DaSession::read_status(unsigned int timeout_ms) {
    const DaFrame frame = read_frame(timeout_ms);
    const std::uint32_t status = decode_da_status(frame.payload);
    if (m_callbacks.log) {
        m_callbacks.log(status == 0 ? "debug" : "warn",
                        "DA <- status " + describe_da_status(status));
    }
    return status;
}

void DaSession::expect_status(const std::string& what, unsigned int timeout_ms) {
    const std::uint32_t status = read_status(timeout_ms);
    if (status != 0) {
        throw ProtocolError(what + ": the download agent reported " + describe_da_status(status));
    }
}

void DaSession::send_parameter(const std::vector<std::uint8_t>& parameter,
                               const std::string& what) {
    write_frame(parameter, what);
    expect_status(what, kDaDataTimeoutMs);
}

std::vector<std::uint8_t> DaSession::read_answer(const std::string& what) {
    const DaFrame frame = read_frame(kDaDataTimeoutMs);
    expect_status(what, kDaDataTimeoutMs);
    return frame.payload;
}

std::vector<std::uint8_t> DaSession::device_control(DaControlCode code,
                                                    const std::vector<std::uint8_t>& parameter) {
    send_command(DaCommand::DeviceCtrl);
    expect_status(std::string("device control ") + hex32(static_cast<std::uint32_t>(code)),
                  kDaCommandTimeoutMs);
    write(encode_da_command(static_cast<std::uint32_t>(code)), "a device control code");
    expect_status("the device control code", kDaCommandTimeoutMs);

    if (!parameter.empty()) {
        send_parameter(parameter, "a device control parameter");
        return {};
    }
    return read_answer("a device control query");
}

std::vector<std::uint8_t> DaSession::device_control(DaControlCode code,
                                                    const std::vector<std::uint8_t>& parameter,
                                                    std::uint32_t& status) {
    send_command(DaCommand::DeviceCtrl);
    status = read_status(kDaCommandTimeoutMs);
    if (status != 0) {
        return {};
    }
    write(encode_da_command(static_cast<std::uint32_t>(code)), "a device control code");
    status = read_status(kDaCommandTimeoutMs);
    if (status != 0) {
        return {};
    }
    if (parameter.empty()) {
        const DaFrame frame = read_frame(kDaDataTimeoutMs);
        status = read_status(kDaDataTimeoutMs);
        return frame.payload;
    }
    write_frame(parameter, "a device control parameter");
    status = read_status(kDaDataTimeoutMs);
    return {};
}

// --- agent setup -------------------------------------------------------------
void DaSession::sync_signal() {
    write(encode_da_command(kDaSyncSignal), "a sync signal");
    log("debug", "DA -> SYNC_SIGNAL");
}

void DaSession::setup_environment(unsigned int uart_log_level, unsigned int log_channel,
                                  unsigned int system_os) {
    send_command(DaCommand::SetupEnvironment);
    expect_status("setup environment", kDaCommandTimeoutMs);

    std::vector<std::uint8_t> parameter;
    put_le32(parameter, uart_log_level);
    put_le32(parameter, log_channel);
    put_le32(parameter, system_os);
    put_le32(parameter, 0);  // ufs provision
    put_le32(parameter, 0);
    send_parameter(parameter, "the setup environment parameter");
}

void DaSession::setup_hw_init() {
    send_command(DaCommand::SetupHwInitParams);
    expect_status("setup hardware init", kDaCommandTimeoutMs);
    std::vector<std::uint8_t> parameter;
    put_le32(parameter, 0);  // "no config", which is what mtkclient sends
    send_parameter(parameter, "the hardware init parameter");
}

void DaSession::set_checksum_level(unsigned int level) {
    std::vector<std::uint8_t> parameter;
    put_le32(parameter, level);
    device_control(DaControlCode::SetChecksumLevel, parameter);
}

void DaSession::set_reset_key(unsigned int key) {
    std::vector<std::uint8_t> parameter;
    put_le32(parameter, key);
    device_control(DaControlCode::SetResetKey, parameter);
}

void DaSession::init_external_ram(const std::vector<std::uint8_t>& emi) {
    if (emi.empty()) {
        throw ProtocolError("refusing to send an empty external RAM configuration");
    }
    send_command(DaCommand::InitExtRam);
    expect_status("init external RAM", kDaCommandTimeoutMs);

    // The length goes in its own frame before the blob.
    std::vector<std::uint8_t> length;
    put_le32(length, static_cast<std::uint32_t>(emi.size()));
    write_frame(length, "the external RAM configuration length");
    send_parameter(emi, "the external RAM configuration");
    if (m_callbacks.log) {
        m_callbacks.log("ok", "external RAM configuration accepted ("
                                  + std::to_string(emi.size()) + " bytes)");
    }
}

// --- queries -----------------------------------------------------------------
DaCommand DaSession::get_da_version(std::string& version_text) {
    const std::vector<std::uint8_t> payload = device_control(DaControlCode::GetDaVersion);
    if (payload.empty()) {
        // An agent that does not answer still told us something worth saying:
        // the command is not in its vocabulary, so nothing else here is
        // guaranteed either. Reported rather than treated as a failure.
        version_text.clear();
        if (m_callbacks.log) {
            m_callbacks.log("warn",
                            "the download agent did not report a version; the older ones do not "
                            "implement GET_DA_VERSION, so this is not necessarily a fault");
        }
        return DaCommand::Unknown;
    }
    version_text.assign(payload.begin(), payload.end());
    return DaCommand::GetDaVersion;
}

std::string DaSession::get_connection_agent() {
    const std::vector<std::uint8_t> payload = device_control(DaControlCode::GetConnectionAgent);
    return std::string(payload.begin(), payload.end());
}

ChipId DaSession::get_chip_id() {
    return parse_chip_id(device_control(DaControlCode::GetChipId));
}

EmmcInfo DaSession::get_emmc_info() {
    return parse_emmc_info(device_control(DaControlCode::GetEmmcInfo));
}

NandInfo DaSession::get_nand_info() {
    return parse_nand_info(device_control(DaControlCode::GetNandInfo));
}

NorInfo DaSession::get_nor_info() {
    return parse_nor_info(device_control(DaControlCode::GetNorInfo));
}

RamInfo DaSession::get_ram_info() {
    return parse_ram_info(device_control(DaControlCode::GetRamInfo));
}

PacketLengths DaSession::get_packet_length() {
    const PacketLengths lengths = parse_packet_length(device_control(DaControlCode::GetPacketLength));
    // A zero would mean "send nothing", so it is treated as "no answer" and the
    // fallback is used instead.
    if (lengths.write_packet_length != 0) {
        m_write_packet = std::min(lengths.write_packet_length, 32u * 1024u * 1024u);
    }
    if (lengths.read_packet_length != 0) {
        m_read_packet = std::min(lengths.read_packet_length, 32u * 1024u * 1024u);
    }
    return lengths;
}

bool DaSession::sla_enabled() {
    const std::vector<std::uint8_t> payload = device_control(DaControlCode::SlaEnabledStatus);
    if (payload.empty()) {
        return false;
    }
    std::uint32_t value = 0;
    for (std::size_t index = 0; index < payload.size() && index < 4; ++index) {
        value |= static_cast<std::uint32_t>(payload[index]) << (8 * index);
    }
    return value != 0;
}

// --- storage operations ------------------------------------------------------
void DaSession::format_region(DaStorage storage, std::uint32_t partition, std::uint64_t address,
                              std::uint64_t length, const NandExtension& extension) {
    check_cancelled();
    if (length == 0) {
        throw ProtocolError("refusing to format a zero-length region");
    }
    send_command(DaCommand::Format);
    expect_status("format", kDaCommandTimeoutMs);

    const std::vector<std::uint8_t> parameter =
        encode_region_param(storage, partition, address, length, extension);
    send_parameter(parameter, "the format parameter");

    if (m_callbacks.log) {
        m_callbacks.log("info", "erasing " + std::to_string(length) + " bytes at 0x"
                                    + [&] {
                                          char buffer[24];
                                          std::snprintf(buffer, sizeof(buffer), "%llx",
                                                        static_cast<unsigned long long>(address));
                                          return std::string(buffer);
                                      }()
                                    + " on " + to_string(storage));
    }

    // The agent reports Continue with a delay in milliseconds while it works,
    // and Complete when it is done. Each round is: Continue, delay, our ack,
    // next status.
    std::uint32_t status = read_status(kDaDataTimeoutMs);
    std::uint64_t rounds = 0;
    while (status == kDaStatusContinue) {
        check_cancelled();
        const std::uint32_t requested_delay = read_status(kDaDataTimeoutMs);
        // The agent's delay is advisory and a broken one must not park the
        // worker thread for an hour. Clamped, and said so when it happens.
        constexpr std::uint32_t kMaxDelayMs = 5000;
        const std::uint32_t delay = std::min(requested_delay, kMaxDelayMs);
        if (requested_delay > kMaxDelayMs) {
            log("warn", "the agent asked for a " + std::to_string(requested_delay)
                            + " ms wait between erase steps; waiting "
                            + std::to_string(kMaxDelayMs) + " ms instead");
        }
        if (delay > 0) {
            std::this_thread::sleep_for(std::chrono::milliseconds(delay));
        }

        std::vector<std::uint8_t> ack(4, 0);
        write_frame(ack, "an erase acknowledgement");
        status = read_status(kDaDataTimeoutMs);
        ++rounds;
        if (m_callbacks.progress) {
            m_callbacks.progress(-1, "erasing, " + std::to_string(rounds) + " steps");
        }
    }
    if (status != kDaStatusComplete) {
        throw ProtocolError("erase: the download agent reported " + describe_da_status(status)
                            + " instead of completion");
    }
    if (m_callbacks.log) {
        m_callbacks.log("ok", "erased " + std::to_string(length) + " bytes in "
                                  + std::to_string(rounds) + " steps");
    }
}

std::uint64_t DaSession::write_region(DaStorage storage, std::uint32_t partition,
                                      std::uint64_t address, const std::vector<std::uint8_t>& data,
                                      std::uint32_t packet_size) {
    check_cancelled();
    if (data.empty()) {
        throw ProtocolError("refusing to write zero bytes");
    }

    // The protocol writes whole sectors, so the payload is padded rather than
    // rejected: a scatter image is very often not a sector multiple, and a short
    // write would leave the tail of the previous contents in place.
    std::vector<std::uint8_t> payload = data;
    if (payload.size() % kDaSectorSize != 0) {
        const std::size_t padding = kDaSectorSize - (payload.size() % kDaSectorSize);
        if (m_callbacks.log) {
            m_callbacks.log("debug", "padding the write with " + std::to_string(padding)
                                         + " zero bytes to reach a sector boundary");
        }
        payload.resize(payload.size() + padding, 0);
    }

    send_command(DaCommand::WriteData);
    expect_status("write data", kDaCommandTimeoutMs);

    const std::vector<std::uint8_t> parameter =
        encode_region_param(storage, partition, address, payload.size());
    send_parameter(parameter, "the write parameter");

    const std::uint32_t chunk_limit =
        packet_size != 0 ? packet_size : (m_write_packet != 0 ? m_write_packet : kDaFallbackPacketSize);

    std::uint64_t written = 0;
    while (written < payload.size()) {
        check_cancelled();
        const std::size_t count = static_cast<std::size_t>(
            std::min<std::uint64_t>(chunk_limit, payload.size() - written));
        const std::uint8_t* begin = payload.data() + written;

        // Three frames per chunk: a zero word, the checksum, then the bytes.
        std::vector<std::uint8_t> zero;
        put_le32(zero, 0);
        write_frame(zero, "the write chunk header");
        write_frame(encode_da_checksum(compute_da_checksum(begin, count)), "the write checksum");
        write(std::vector<std::uint8_t>(begin, begin + count), "a write chunk");
        expect_status("write data", kDaDataTimeoutMs);

        written += count;
        if (m_callbacks.progress) {
            const int percent = static_cast<int>((written * 100) / payload.size());
            m_callbacks.progress(percent, "writing " + std::to_string(written) + " of "
                                              + std::to_string(payload.size()) + " bytes");
        }
    }

    // One more status once every chunk is in: the agent's verdict on the whole
    // write, separate from the per-chunk acknowledgements.
    expect_status("write data", kDaDataTimeoutMs);

    // The optional post-download action follows a completed write. A refusal
    // here does not mean the data did not land - the bytes are already
    // acknowledged - so it is reported and not raised.
    send_command(DaCommand::CcOptionalDownloadAct);
    const std::uint32_t post = read_status(kDaCommandTimeoutMs);
    if (post != 0 && m_callbacks.log) {
        m_callbacks.log("debug",
                        "the agent's optional post-download action answered "
                            + describe_da_status(post) + "; the write itself succeeded");
    }

    return written;
}

std::vector<std::uint8_t> DaSession::read_region(DaStorage storage, std::uint32_t partition,
                                                 std::uint64_t address, std::uint64_t length,
                                                 std::uint32_t packet_size) {
    check_cancelled();
    if (length == 0) {
        throw ProtocolError("refusing to read zero bytes");
    }
    // The same ceiling the Qualcomm read path uses: a read is held in memory, so
    // an implausible length is refused before anything is transferred.
    constexpr std::uint64_t kMaxRead = 512ull * 1024ull * 1024ull;
    if (length > kMaxRead) {
        throw ProtocolError("the read is " + std::to_string(length / (1024 * 1024))
                            + " MiB, above the " + std::to_string(kMaxRead / (1024 * 1024))
                            + " MiB this build holds in memory");
    }

    send_command(DaCommand::ReadData);
    expect_status("read data", kDaCommandTimeoutMs);

    const std::vector<std::uint8_t> parameter =
        encode_region_param(storage, partition, address, length);
    send_parameter(parameter, "the read parameter");
    expect_status("read data", kDaDataTimeoutMs);

    const std::uint32_t chunk_limit =
        packet_size != 0 ? packet_size : (m_read_packet != 0 ? m_read_packet : kDaFallbackPacketSize);
    (void)chunk_limit;  // the agent frames the payload itself; this is only a sanity bound

    std::vector<std::uint8_t> out;
    out.reserve(static_cast<std::size_t>(std::min<std::uint64_t>(length, 64ull * 1024ull * 1024ull)));

    std::uint64_t received = 0;
    while (received < length) {
        check_cancelled();
        const DaFrame frame = read_frame(kDaDataTimeoutMs);
        if (frame.payload.size() > 4) {
            out.insert(out.end(), frame.payload.begin(), frame.payload.end());
            received += frame.payload.size();

            // Every data frame is acknowledged with a zero word.
            std::vector<std::uint8_t> ack(4, 0);
            write_frame(ack, "a read acknowledgement");

            if (m_callbacks.progress) {
                const int percent = static_cast<int>((received * 100) / length);
                m_callbacks.progress(percent, "reading " + std::to_string(received) + " of "
                                                  + std::to_string(length) + " bytes");
            }
            continue;
        }
        if (frame.payload.size() == 4) {
            // A short frame in the middle of a read is the agent's end-of-data
            // flag. Zero is "done", anything else is a failure with a code.
            const std::uint32_t flag = read_le32(frame.payload.data());
            if (flag != 0) {
                throw ProtocolError("read data: the agent stopped early with "
                                    + describe_da_status(flag));
            }
            break;
        }
        throw ProtocolError("read data: the agent sent a " + std::to_string(frame.payload.size())
                            + "-byte frame, which is neither payload nor a status");
    }

    // The closing status, separate from the end-of-data flag.
    expect_status("read data", kDaDataTimeoutMs);

    if (out.size() > length) {
        // The agent may round the transfer up to its own packet size.
        out.resize(static_cast<std::size_t>(length));
    }
    if (m_callbacks.log) {
        m_callbacks.log("ok", "read " + std::to_string(out.size()) + " bytes");
    }
    return out;
}

void DaSession::shutdown(unsigned int mode) {
    send_command(DaCommand::Shutdown);
    expect_status("shutdown", kDaCommandTimeoutMs);
    std::vector<std::uint8_t> parameter;
    put_le32(parameter, mode);
    put_le32(parameter, 0);
    send_parameter(parameter, "the shutdown parameter");
    if (m_callbacks.log) {
        m_callbacks.log("info", "the device has been told to shut down; it will re-enumerate");
    }
}

}  // namespace huaxin::protocols::mediatek
