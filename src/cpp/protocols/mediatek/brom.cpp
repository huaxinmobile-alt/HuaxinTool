#include "protocols/mediatek/brom.h"

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstring>
#include <utility>

namespace huaxin::protocols::mediatek {

using protocols::qualcomm::ProtocolError;

namespace {

/// The protocol is big-endian everywhere: the host sends and reads big-endian
/// words, unlike Sahara/Firehose. These helpers exist so that is stated once
/// instead of being implied at every call site.
void put_be32(std::vector<std::uint8_t>& out, std::uint32_t value) {
    out.push_back(static_cast<std::uint8_t>((value >> 24) & 0xFF));
    out.push_back(static_cast<std::uint8_t>((value >> 16) & 0xFF));
    out.push_back(static_cast<std::uint8_t>((value >> 8) & 0xFF));
    out.push_back(static_cast<std::uint8_t>(value & 0xFF));
}

std::uint16_t read_be16(const std::uint8_t* data) {
    return static_cast<std::uint16_t>((data[0] << 8) | data[1]);
}

std::uint32_t read_be32(const std::uint8_t* data) {
    return (static_cast<std::uint32_t>(data[0]) << 24) | (static_cast<std::uint32_t>(data[1]) << 16)
           | (static_cast<std::uint32_t>(data[2]) << 8) | static_cast<std::uint32_t>(data[3]);
}

/// Formatters for log messages. Spelled out once so the protocol code reads as
/// protocol code rather than as printf plumbing.
std::string hex8(std::uint32_t value) {
    char buffer[8];
    std::snprintf(buffer, sizeof(buffer), "0x%02x", value & 0xFF);
    return buffer;
}

std::string hex16(std::uint32_t value) {
    char buffer[8];
    std::snprintf(buffer, sizeof(buffer), "0x%04x", value & 0xFFFF);
    return buffer;
}

std::string hex32(std::uint32_t value) {
    char buffer[16];
    std::snprintf(buffer, sizeof(buffer), "0x%08x", value);
    return buffer;
}

}  // namespace

const char* to_string(BromCommand command) noexcept {
    switch (command) {
        case BromCommand::Read16:           return "READ16";
        case BromCommand::Read32:           return "READ32";
        case BromCommand::Write16:          return "WRITE16";
        case BromCommand::Write16NoEcho:    return "WRITE16_NO_ECHO";
        case BromCommand::Write32:          return "WRITE32";
        case BromCommand::JumpDa:           return "JUMP_DA";
        case BromCommand::JumpBl:           return "JUMP_BL";
        case BromCommand::SendDa:           return "SEND_DA";
        case BromCommand::GetTargetConfig:  return "GET_TARGET_CONFIG";
        case BromCommand::SendEnvPrepare:   return "SEND_ENV_PREPARE";
        case BromCommand::RegisterAccess:   return "BROM_REGISTER_ACCESS";
        case BromCommand::Uart1LogEnable:   return "UART1_LOG_EN";
        case BromCommand::Uart1SetBaudrate: return "UART1_SET_BAUDRATE";
        case BromCommand::SendCert:         return "SEND_CERT";
        case BromCommand::GetMeId:          return "GET_ME_ID";
        case BromCommand::SendAuth:         return "SEND_AUTH";
        case BromCommand::Sla:              return "SLA";
        case BromCommand::GetSocId:         return "GET_SOC_ID";
        case BromCommand::Zeroization:      return "ZEROIZATION";
        case BromCommand::GetPlCap:         return "GET_PL_CAP";
        case BromCommand::GetHwSwVer:       return "GET_HW_SW_VER";
        case BromCommand::GetHwCode:        return "GET_HW_CODE";
        case BromCommand::GetBlVer:         return "GET_BL_VER";
        case BromCommand::GetVersion:       return "GET_VERSION";
    }
    return "UNKNOWN";
}

// --- target config -----------------------------------------------------------
std::string TargetConfig::describe() const {
    std::string text;
    const auto add = [&text](const char* name, bool set) {
        if (set) {
            text += (text.empty() ? "" : ", ");
            text += name;
        }
    };
    add("secure boot", secure_boot);
    add("SLA", sla_required);
    add("DA authentication", da_authentication);
    add("SW JTAG", sw_jtag);
    add("EPP", epp_supported);
    add("certificate", certificate_required);
    add("memory read", memory_read_allowed);
    add("memory write", memory_write_allowed);
    add("CMD_C8", cmd_c8_supported);
    return text.empty() ? "no security features reported" : text;
}

TargetConfig decode_target_config(std::uint32_t raw, std::uint16_t status) {
    TargetConfig config;
    config.raw = raw;
    config.status = status;
    config.secure_boot = (raw & 0x01) != 0;
    config.sla_required = (raw & 0x02) != 0;
    config.da_authentication = (raw & 0x04) != 0;
    // Upstream masks 0x06 for SW JTAG, which overlaps the SLA (0x02) and DAA
    // (0x04) bits. That is what mtkclient does, so it is reproduced verbatim
    // rather than "corrected" - if this reads true, check the other two bits
    // before believing it means JTAG.
    config.sw_jtag = (raw & 0x06) != 0;
    config.epp_supported = (raw & 0x08) != 0;
    config.certificate_required = (raw & 0x10) != 0;
    config.memory_read_allowed = (raw & 0x20) != 0;
    config.memory_write_allowed = (raw & 0x40) != 0;
    config.cmd_c8_supported = (raw & 0x80) != 0;
    return config;
}

// --- checksum ----------------------------------------------------------------
std::uint16_t compute_checksum(const std::uint8_t* data, std::size_t size) {
    // XOR of the payload read as 16-bit little-endian words, from mtkclient's
    // prepare_data(). Note the endianness differs from the command framing:
    // this one really is little-endian.
    std::uint16_t checksum = 0;
    std::size_t index = 0;
    for (; index + 1 < size; index += 2) {
        checksum ^= static_cast<std::uint16_t>(data[index] | (data[index + 1] << 8));
    }
    if (index < size) {
        checksum ^= data[index];
    }
    return checksum;
}

std::uint16_t compute_checksum(const std::vector<std::uint8_t>& data) {
    return compute_checksum(data.data(), data.size());
}

std::vector<std::uint8_t> build_da_payload(const std::vector<std::uint8_t>& file) {
    std::vector<std::uint8_t> payload = file;
    if (payload.size() % 2 != 0) {
        payload.push_back(0x00);
    }
    return payload;
}

// --- session -----------------------------------------------------------------
BromSession::BromSession(IByteTransport& transport, Callbacks callbacks)
    : m_transport(transport), m_callbacks(std::move(callbacks)) {}

void BromSession::log(const std::string& level, const std::string& message) const {
    if (m_callbacks.log) {
        m_callbacks.log(level, message);
    }
}

void BromSession::check_cancelled() const {
    if (m_callbacks.cancelled && m_callbacks.cancelled()) {
        throw ProtocolError("cancelled by the operator");
    }
}

void BromSession::write_bytes(const std::vector<std::uint8_t>& data, unsigned int timeout_ms) {
    m_transport.write_all(data.data(), data.size(), timeout_ms);
}

void BromSession::handshake(bool send_lead_byte) {
    if (send_lead_byte) {
        const std::vector<std::uint8_t> lead{kHandshakeLead};
        write_bytes(lead, 1000);
        std::uint8_t echo = 0;
        m_transport.read_exact(&echo, 1, 1000);
        if (echo != static_cast<std::uint8_t>(~kHandshakeLead)) {
            throw ProtocolError("the bootrom did not answer the lead byte: expected "
                                + hex8(static_cast<std::uint8_t>(~kHandshakeLead)) + ", got "
                                + hex8(echo));
        }
    }

    // Each byte is sent on its own and answered with its bitwise complement.
    // Sending them as one write also works on many chipsets, but the bootrom
    // can then coalesce the echoes, so the per-byte form is what is relied on.
    for (std::size_t index = 0; index < kHandshakeLength; ++index) {
        check_cancelled();
        const std::uint8_t byte = kHandshakeBytes[index];
        const std::uint8_t expected = kHandshakeEcho[index];

        m_transport.write_all(&byte, 1, 1000);
        std::uint8_t echo = 0;
        m_transport.read_exact(&echo, 1, 2000);

        if (echo != expected) {
            throw ProtocolError("bootrom handshake failed at byte "
                                + std::to_string(index + 1) + ": sent " + hex8(byte)
                                + ", expected the echo " + hex8(expected) + ", got "
                                + hex8(echo)
                                + ". This is not a bootrom, or the device is not in BROM mode.");
        }
    }

    m_handshaked = true;
    log("ok", "bootrom handshake complete");
}

void BromSession::send_command(BromCommand command, unsigned int timeout_ms) {
    check_cancelled();
    const std::uint8_t byte = static_cast<std::uint8_t>(command);
    m_transport.write_all(&byte, 1, timeout_ms);

    std::uint8_t echo = 0;
    m_transport.read_exact(&echo, 1, timeout_ms);
    if (echo != byte) {
        throw ProtocolError(std::string("") + to_string(command) + " was not echoed back (got "
                            + hex8(echo) + "); the conversation is out of sync");
    }
}

std::vector<std::uint8_t> BromSession::send_command_with_response(BromCommand command,
                                                                  std::size_t length,
                                                                  unsigned int timeout_ms) {
    send_command(command, timeout_ms);
    return read_bytes(length, timeout_ms);
}

std::vector<std::uint8_t> BromSession::read_bytes(std::size_t length, unsigned int timeout_ms) {
    std::vector<std::uint8_t> buffer(length);
    if (length > 0) {
        m_transport.read_exact(buffer.data(), length, timeout_ms);
    }
    return buffer;
}

std::uint16_t BromSession::read_word(unsigned int timeout_ms) {
    const std::vector<std::uint8_t> bytes = read_bytes(2, timeout_ms);
    return read_be16(bytes.data());
}

std::uint32_t BromSession::read_dword(unsigned int timeout_ms) {
    const std::vector<std::uint8_t> bytes = read_bytes(4, timeout_ms);
    return read_be32(bytes.data());
}

BromChipInfo BromSession::read_chip_info() {
    BromChipInfo info;

    // GET_HW_CODE returns one 32-bit word carrying both ids.
    send_command(BromCommand::GetHwCode);
    const std::uint32_t value = read_dword();
    info.hardware_code = static_cast<std::uint16_t>((value >> 16) & 0xFFFF);
    info.hardware_version = static_cast<std::uint16_t>(value & 0xFFFF);
    info.have_hw_code = true;
    log("ok", "hardware code " + hex16(info.hardware_code) + ", hardware version "
                  + hex16(info.hardware_version));

    // GET_HW_SW_VER returns four big-endian halfwords.
    //
    // The echo check here follows the convention every other command in this
    // protocol uses, but mtkclient's sendcmd() helper was not available to read,
    // so this one command's framing is inferred rather than transcribed. A
    // failure here is reported and not fatal.
    try {
        const std::vector<std::uint8_t> bytes =
            send_command_with_response(BromCommand::GetHwSwVer, 8);
        info.hw_sw_hw_code = read_be16(bytes.data());
        info.hw_sw_hw_sub_code = read_be16(bytes.data() + 2);
        info.hw_sw_hw_version = read_be16(bytes.data() + 4);
        info.hw_sw_sw_version = read_be16(bytes.data() + 6);
        info.have_hw_sw_version = true;
        log("debug", "HW/SW version block: hw " + hex16(info.hw_sw_hw_code) + ", sub "
                         + hex16(info.hw_sw_hw_sub_code) + ", version "
                         + hex16(info.hw_sw_hw_version) + ", sw " + hex16(info.hw_sw_sw_version));
    } catch (const ProtocolError& failure) {
        log("debug", std::string("GET_HW_SW_VER did not answer as expected: ") + failure.what());
    }

    // GET_VERSION is the one command that is NOT echo-checked: mtkclient writes
    // the byte and reads a single-byte version straight back.
    try {
        const std::uint8_t byte = static_cast<std::uint8_t>(BromCommand::GetVersion);
        m_transport.write_all(&byte, 1, 1000);
        const std::vector<std::uint8_t> answer = read_bytes(1, 1000);
        info.brom_version = answer[0];
        info.have_brom_version = true;
        log("info", "BROM version " + std::to_string(info.brom_version));
    } catch (const ProtocolError& failure) {
        log("debug", std::string("the bootrom did not report a version: ") + failure.what());
    }

    // TODO: map hardware_code to a chip name (e.g. 0x0672 = MT6735). Deliberately
    // left empty: the table has not been verified against a primary source here,
    // and a wrong chipset name would send an operator to the wrong DA file.

    return info;
}

TargetConfig BromSession::read_target_config() {
    send_command(BromCommand::GetTargetConfig);
    const std::vector<std::uint8_t> bytes = read_bytes(6);
    const std::uint32_t raw = read_be32(bytes.data());
    const std::uint16_t status = read_be16(bytes.data() + 4);

    const TargetConfig config = decode_target_config(raw, status);
    log("info", "target config " + hex32(raw) + ": " + config.describe());
    return config;
}

void BromSession::upload_download_agent(const DownloadAgent& agent, bool jump,
                                        std::size_t chunk_size) {
    if (agent.payload.empty()) {
        throw ProtocolError("the download agent is empty");
    }
    if (agent.signature_length > agent.payload.size()) {
        throw ProtocolError("the download agent signature length ("
                            + std::to_string(agent.signature_length)
                            + ") is larger than the file itself ("
                            + std::to_string(agent.payload.size()) + " bytes)");
    }

    send_command(BromCommand::SendDa);

    // Address, total length and signature length, each echoed back by the device.
    for (const std::uint32_t value : {agent.load_address,
                                      static_cast<std::uint32_t>(agent.payload.size()),
                                      agent.signature_length}) {
        std::vector<std::uint8_t> encoded;
        put_be32(encoded, value);
        m_transport.write_all(encoded.data(), encoded.size(), 1000);
        std::vector<std::uint8_t> echo(4);
        m_transport.read_exact(echo.data(), echo.size(), 1000);
        if (echo != encoded) {
            throw ProtocolError("SEND_DA parameter was not echoed back correctly");
        }
    }

    const std::uint16_t status = read_word();
    if (status == kStatusSlaRequired) {
        throw ProtocolError(
            "the bootrom requires SLA authentication before it will accept a download agent "
            "(status 0x1D0D). This is a secure-boot device; uploading a DA needs a signed "
            "authentication handshake, which is not implemented.");
    }
    if (status != kStatusOk) {
        throw ProtocolError("SEND_DA was rejected with status " + hex16(status));
    }

    // Payload, then verify the device's checksum against ours.
    const std::uint16_t expected = compute_checksum(agent.payload);
    const std::size_t total = agent.payload.size();
    std::size_t sent = 0;
    std::size_t step = chunk_size == 0 ? total : chunk_size;

    while (sent < total) {
        check_cancelled();
        const std::size_t count = std::min(step, total - sent);
        m_transport.write_all(agent.payload.data() + sent, count, 5000);
        sent += count;
        if (m_callbacks.progress) {
            m_callbacks.progress(static_cast<int>(sent * 100 / total),
                                 "download agent " + std::to_string(sent) + "/"
                                     + std::to_string(total) + " bytes");
        }
    }

    const std::uint16_t reported = read_word(5000);
    if (reported != expected) {
        throw ProtocolError("the bootrom's checksum does not match: it computed " + hex16(reported)
                            + ", we computed " + hex16(expected)
                            + ". The transfer is corrupt and the agent must not be started.");
    }
    log("ok", "download agent uploaded (" + std::to_string(total) + " bytes, checksum "
                  + hex16(expected) + ")");

    if (jump) {
        jump_to_download_agent(agent.load_address);
    }
}

void BromSession::jump_to_download_agent(std::uint32_t address) {
    send_command(BromCommand::JumpDa);
    std::vector<std::uint8_t> encoded;
    put_be32(encoded, address);
    m_transport.write_all(encoded.data(), encoded.size(), 1000);
    std::vector<std::uint8_t> echo(4);
    m_transport.read_exact(echo.data(), echo.size(), 1000);
    if (echo != encoded) {
        throw ProtocolError("JUMP_DA address was not echoed back correctly");
    }
    log("ok", "jumped to the download agent; the device now speaks the DA protocol");
}

}  // namespace huaxin::protocols::mediatek
