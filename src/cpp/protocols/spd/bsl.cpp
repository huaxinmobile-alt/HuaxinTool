#include "protocols/spd/bsl.h"
#include "core/flash_timeouts.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstring>

namespace huaxin::protocols::spd {

using protocols::qualcomm::ProtocolError;

namespace {

void put_be16(std::vector<std::uint8_t>& out, std::uint16_t value) {
    out.push_back(static_cast<std::uint8_t>((value >> 8) & 0xFF));
    out.push_back(static_cast<std::uint8_t>(value & 0xFF));
}

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

/// A response code's name, if it is one this build knows.
const char* response_name(std::uint16_t code) {
    switch (static_cast<BslResponse>(code)) {
        case BslResponse::Ack:                  return "ACK";
        case BslResponse::Version:              return "version";
        case BslResponse::InvalidCommand:       return "invalid command";
        case BslResponse::UnknownCommand:       return "unknown command";
        case BslResponse::OperationFailed:      return "operation failed";
        case BslResponse::NotSupportBaudrate:   return "unsupported baud rate";
        case BslResponse::DownloadNotStarted:   return "download was not started";
        case BslResponse::DownloadMultiStart:   return "download started twice";
        case BslResponse::DownloadEarlyEnd:     return "download ended early";
        case BslResponse::DownloadDestError:    return "download destination error";
        case BslResponse::DownloadSizeError:    return "download size error";
        case BslResponse::VerifyError:          return "verify error";
        case BslResponse::NotVerify:            return "verify not performed";
        case BslResponse::PhoneNotEnoughMemory: return "the device has not enough memory";
        case BslResponse::PhoneWaitInputTimeout:return "the device timed out waiting for input";
        case BslResponse::PhoneSucceed:         return "succeeded";
        case BslResponse::PhoneValidBaudrate:   return "valid baud rate";
        case BslResponse::PhoneRepeatContinue:  return "terminal asks to continue";
        case BslResponse::PhoneRepeatBreak:     return "terminal asks to break";
        case BslResponse::ReadFlashResult:      return "flash contents";
        case BslResponse::ReadChipTypeResult:   return "chip type";
        case BslResponse::ReadNvItemResult:     return "NV item";
        case BslResponse::IncompatiblePartition:return "incompatible partition table";
        case BslResponse::UnknownDevice:        return "unknown device";
        case BslResponse::InvalidDeviceSize:    return "invalid device size";
        case BslResponse::IllegalSdram:         return "illegal SDRAM";
        case BslResponse::WrongSdramParameter:  return "wrong SDRAM parameter";
        case BslResponse::ReadFlashInfoResult:  return "flash information";
        case BslResponse::ReadSectorSizeResult: return "sector size";
        case BslResponse::ReadFlashTypeResult:  return "flash type";
        case BslResponse::ReadFlashUidResult:   return "flash UID";
        case BslResponse::ErrorChecksum:        return "checksum error";
        case BslResponse::ChecksumDiff:         return "checksum mismatch";
        case BslResponse::WriteError:           return "write error";
        case BslResponse::ChipIdNotMatch:       return "the chip id does not match the package";
        case BslResponse::FlashConfigError:     return "flash configuration error";
        case BslResponse::PhoneIsRooted:        return "the device reports it is rooted";
        case BslResponse::SecurityVerifyError:  return "security data verify error";
        case BslResponse::ReadChipUidResult:    return "chip UID";
        case BslResponse::NotEnableWriteFlash:  return "writing to the flash is not enabled";
        case BslResponse::EnableSecureBootError:return "secure boot could not be enabled";
        case BslResponse::FlashWrittenProtection: return "the flash is write protected";
        case BslResponse::FlashInitializingFail:  return "the flash could not be initialised";
        case BslResponse::UnsupportedCommand:   return "the agent does not support this command";
        case BslResponse::Log:                  return "log";
    }
    return nullptr;
}

}  // namespace

const char* to_string(BslCommand command) noexcept {
    switch (command) {
        case BslCommand::Connect:        return "CONNECT";
        case BslCommand::StartData:      return "START_DATA";
        case BslCommand::MidstData:      return "MIDST_DATA";
        case BslCommand::EndData:        return "END_DATA";
        case BslCommand::ExecData:       return "EXEC_DATA";
        case BslCommand::NormalReset:    return "NORMAL_RESET";
        case BslCommand::ReadFlash:      return "READ_FLASH";
        case BslCommand::ReadChipType:   return "READ_CHIP_TYPE";
        case BslCommand::ReadNvItem:     return "READ_NVITEM";
        case BslCommand::ChangeBaud:     return "CHANGE_BAUD";
        case BslCommand::EraseFlash:     return "ERASE_FLASH";
        case BslCommand::Repartition:    return "REPARTITION";
        case BslCommand::ReadFlashType:  return "READ_FLASH_TYPE";
        case BslCommand::ReadFlashInfo:  return "READ_FLASH_INFO";
        case BslCommand::ReadSectorSize: return "READ_SECTOR_SIZE";
        case BslCommand::ReadStart:      return "READ_START";
        case BslCommand::ReadMidst:      return "READ_MIDST";
        case BslCommand::ReadEnd:        return "READ_END";
        case BslCommand::KeepCharge:     return "KEEP_CHARGE";
        case BslCommand::ExtTable:       return "EXTTABLE";
        case BslCommand::ReadFlashUid:   return "READ_FLASH_UID";
        case BslCommand::PowerOff:       return "POWER_OFF";
        case BslCommand::CheckRoot:      return "CHECK_ROOT";
        case BslCommand::ReadChipUid:    return "READ_CHIP_UID";
        case BslCommand::ReadPartition:  return "READ_PARTITION";
        case BslCommand::EndProcess:     return "END_PROCESS";
    }
    return "UNKNOWN";
}

const char* to_string(BslResponse response) noexcept {
    const char* name = response_name(static_cast<std::uint16_t>(response));
    return name == nullptr ? "UNKNOWN" : name;
}

std::string describe_response(std::uint16_t code) {
    char buffer[160];
    const char* name = response_name(code);
    if (name != nullptr) {
        std::snprintf(buffer, sizeof(buffer), "%s (0x%04X)", name, code);
    } else {
        std::snprintf(buffer, sizeof(buffer), "unknown response 0x%04X", code);
    }
    return buffer;
}

// --- frames ------------------------------------------------------------------
std::vector<std::uint8_t> hdlc_escape(const std::vector<std::uint8_t>& body) {
    std::vector<std::uint8_t> out;
    out.reserve(body.size());
    for (const std::uint8_t byte : body) {
        if (byte == kBslFlag || byte == kBslEscape) {
            out.push_back(kBslEscape);
            out.push_back(static_cast<std::uint8_t>(byte ^ kBslEscapeMask));
        } else {
            out.push_back(byte);
        }
    }
    return out;
}

std::vector<std::uint8_t> hdlc_unescape(const std::vector<std::uint8_t>& body) {
    std::vector<std::uint8_t> out;
    out.reserve(body.size());
    for (std::size_t index = 0; index < body.size(); ++index) {
        const std::uint8_t byte = body[index];
        if (byte == kBslEscape) {
            if (index + 1 >= body.size()) {
                // A read can split a frame between the escape and what it
                // escapes. Dropping it here is safe: the caller only calls this
                // on a body it has already delimitered, so a trailing escape is
                // the corruption rather than the start of something.
                break;
            }
            out.push_back(static_cast<std::uint8_t>(body[++index] ^ kBslEscapeMask));
        } else {
            out.push_back(byte);
        }
    }
    return out;
}

std::vector<std::uint8_t> build_frame(std::uint16_t type, const std::vector<std::uint8_t>& data,
                                      ChecksumKind checksum) {
    if (data.size() > kBslMaxData) {
        throw ProtocolError("a BSL frame carries at most " + std::to_string(kBslMaxData)
                            + " bytes; this one is " + std::to_string(data.size()));
    }
    if (checksum == ChecksumKind::Unknown) {
        throw ProtocolError(
            "refusing to build a BSL frame without knowing which checksum the device expects: "
            "the two in use are not interchangeable and a wrong one gets no reply at all");
    }

    std::vector<std::uint8_t> body;
    body.reserve(4 + data.size() + 2);
    put_be16(body, type);
    put_be16(body, static_cast<std::uint16_t>(data.size()));
    body.insert(body.end(), data.begin(), data.end());
    const std::uint16_t check = compute(checksum, body.data(), body.size());
    put_be16(body, check);

    std::vector<std::uint8_t> frame;
    frame.reserve(body.size() + 2);
    frame.push_back(kBslFlag);
    const std::vector<std::uint8_t> escaped = hdlc_escape(body);
    frame.insert(frame.end(), escaped.begin(), escaped.end());
    frame.push_back(kBslFlag);
    return frame;
}

ChecksumKind detect_checksum(const std::vector<std::uint8_t>& body) noexcept {
    if (body.size() < 6) {
        return ChecksumKind::Unknown;
    }
    const std::size_t payload_size = body.size() - 2;
    const std::uint16_t sent = read_be16(body.data() + payload_size);
    if (sprd_sum(body.data(), payload_size) == sent) {
        return ChecksumKind::SprdSum;
    }
    if (crc16_ccitt(body.data(), payload_size) == sent) {
        return ChecksumKind::Crc16Ccitt;
    }
    return ChecksumKind::Unknown;
}

BslFrame parse_frame(const std::vector<std::uint8_t>& body) {
    if (body.size() < 6) {
        throw ProtocolError("a BSL frame is at least six bytes; " + std::to_string(body.size())
                            + " arrived");
    }
    BslFrame frame;
    frame.type = read_be16(body.data());
    const std::uint16_t declared = read_be16(body.data() + 2);
    const std::size_t available = body.size() - 4;
    if (available < static_cast<std::size_t>(declared) + 2) {
        throw ProtocolError("a BSL frame declares " + std::to_string(declared)
                            + " bytes of data but only " + std::to_string(available)
                            + " bytes followed the header");
    }
    frame.checksum = detect_checksum(body);
    if (frame.checksum == ChecksumKind::Unknown) {
        char buffer[192];
        std::snprintf(buffer, sizeof(buffer),
                      "a BSL frame's checksum matched neither algorithm (type 0x%04X, %zu bytes): "
                      "the stream is not a BSL conversation",
                      frame.type, body.size());
        throw ProtocolError(buffer);
    }
    frame.data.assign(body.begin() + 4, body.begin() + 4 + declared);
    return frame;
}

// --- chip info ---------------------------------------------------------------
std::string ChipInfo::describe() const {
    std::string text;
    if (!boot_version.empty()) {
        text += "boot ROM " + boot_version;
    }
    if (have_chip_type) {
        char buffer[48];
        std::snprintf(buffer, sizeof(buffer), "%schip type 0x%08X", text.empty() ? "" : ", ",
                      chip_type);
        text += buffer;
    }
    if (have_flash_type) {
        char buffer[48];
        std::snprintf(buffer, sizeof(buffer), "%sflash type 0x%08X", text.empty() ? "" : ", ",
                      flash_type);
        text += buffer;
    }
    if (have_sector_size) {
        char buffer[48];
        std::snprintf(buffer, sizeof(buffer), "%ssector size %u", text.empty() ? "" : ", ",
                      sector_size);
        text += buffer;
    }
    return text.empty() ? "the device reported nothing about itself" : text;
}

// --- session -----------------------------------------------------------------
BslSession::BslSession(IByteTransport& transport, Callbacks callbacks)
    : m_transport(transport), m_callbacks(std::move(callbacks)) {}

void BslSession::log(const std::string& level, const std::string& message) const {
    if (m_callbacks.log) {
        m_callbacks.log(level, message);
    }
}

void BslSession::check_cancelled() const {
    if (m_callbacks.cancelled && m_callbacks.cancelled()) {
        throw ProtocolError("cancelled by the operator");
    }
}

void BslSession::send_frame(std::uint16_t type, const std::vector<std::uint8_t>& data) {
    const std::vector<std::uint8_t> frame = build_frame(type, data, m_checksum);
    try {
        m_transport.write_all(frame.data(), frame.size(), kCommandTimeoutMs);
    } catch (const ProtocolError& error) {
        throw ProtocolError(std::string("the link failed while sending ") + describe_response(type)
                            + ": " + error.what());
    }
    if (m_callbacks.log) {
        m_callbacks.log("debug", "BSL -> " + describe_response(type) + " (" + std::to_string(data.size())
                                     + " bytes)");
    }
}

BslFrame BslSession::read_frame_from_link(unsigned int timeout_ms) {
    std::uint8_t buffer[512];
    for (;;) {
        // Look for a complete frame in what has already arrived.
        const auto begin = std::find(m_pending.begin(), m_pending.end(), kBslFlag);
        if (begin != m_pending.end()) {
            const auto end = std::find(begin + 1, m_pending.end(), kBslFlag);
            if (end != m_pending.end()) {
                const std::vector<std::uint8_t> body(begin + 1, end);
                m_pending.erase(m_pending.begin(), end + 1);
                return parse_frame(hdlc_unescape(body));
            }
        }

        const std::size_t received = m_transport.read_some(buffer, sizeof(buffer), timeout_ms);
        if (received == 0) {
            throw ProtocolError("the device stopped answering: no BSL frame arrived within "
                                + std::to_string(timeout_ms) + " ms");
        }
        m_pending.insert(m_pending.end(), buffer, buffer + received);

        // A stream that never produces a frame is not a BSL conversation, and
        // holding it forever would grow without bound.
        if (m_pending.size() > 64 * 1024) {
            throw ProtocolError(
                "64 KiB arrived without a complete BSL frame; the device is not answering in "
                "this protocol");
        }
    }
}

BslFrame BslSession::read_frame(unsigned int timeout_ms) {
    for (;;) {
        BslFrame frame = read_frame_from_link(timeout_ms);
        if (frame.type == static_cast<std::uint16_t>(BslResponse::Log)) {
            // The agent's own log output, which is not a verdict and must not be
            // mistaken for one. Forwarded and skipped.
            if (m_callbacks.log) {
                m_callbacks.log("output", "device log: "
                                              + std::string(frame.data.begin(), frame.data.end()));
            }
            continue;
        }
        if (m_callbacks.log) {
            m_callbacks.log("debug", "BSL <- " + describe_response(frame.type) + " ("
                                         + std::to_string(frame.data.size()) + " bytes)");
        }
        return frame;
    }
}

void BslSession::send_and_expect_ack(std::uint16_t type, const std::vector<std::uint8_t>& data,
                                     const std::string& what, unsigned int timeout_ms) {
    send_frame(type, data);
    const BslFrame reply = read_frame(timeout_ms);
    if (reply.type != static_cast<std::uint16_t>(BslResponse::Ack)) {
        throw ProtocolError(what + ": the device answered " + describe_response(reply.type)
                            + " where an acknowledgement was expected");
    }
}

std::string BslSession::usb_hello(unsigned int timeout_ms) {
    // The device's endpoints are configured by a class control transfer before
    // it will accept a bulk frame at all. This is a bare transfer with no data
    // stage; skipping it leaves the link silently dead.
    m_transport.control_transfer(0x21, 0, 1, 0, timeout_ms);

    // A lone HDLC flag, with no frame around it, is the hello.
    const std::uint8_t hello = kBslFlag;
    m_transport.write_all(&hello, 1, kCommandTimeoutMs);

    const BslFrame reply = read_frame(timeout_ms);
    if (reply.type != static_cast<std::uint16_t>(BslResponse::Version)) {
        throw ProtocolError("the device answered " + describe_response(reply.type)
                            + " to the USB hello; a device in Research Download mode answers "
                              "with its version string");
    }

    // The first reply that verifies also settles which checksum this chip
    // family uses, which every later frame depends on.
    if (m_checksum == ChecksumKind::Unknown) {
        m_checksum = reply.checksum;
        if (m_callbacks.log) {
            m_callbacks.log("ok", std::string("the device checks its frames with ") + to_string(m_checksum));
        }
    }

    return std::string(reply.data.begin(), reply.data.end());
}

void BslSession::connect() {
    if (m_checksum == ChecksumKind::Unknown) {
        throw ProtocolError(
            "the checksum is not known yet: the USB hello has to succeed first, because the "
            "reply it brings is what says which one this chip family uses");
    }
    send_and_expect_ack(static_cast<std::uint16_t>(BslCommand::Connect), {}, "connect");
    m_connected = true;
    log("ok", "connected to the Research Download agent");
}

void BslSession::execute_payload(std::uint32_t address, const std::vector<std::uint8_t>& payload,
                                 const std::string& what, bool execute) {
    check_cancelled();
    if (payload.empty()) {
        throw ProtocolError("refusing to send an empty " + what);
    }

    // START_DATA carries the load address and the length, both big-endian.
    std::vector<std::uint8_t> start;
    put_be32(start, address);
    put_be32(start, static_cast<std::uint32_t>(payload.size()));
    send_and_expect_ack(static_cast<std::uint16_t>(BslCommand::StartData), start,
                        what + ": start of transfer");

    std::size_t sent = 0;
    while (sent < payload.size()) {
        check_cancelled();
        const std::size_t count = std::min(kBslMidstChunk, payload.size() - sent);
        const std::vector<std::uint8_t> chunk(payload.begin() + static_cast<std::ptrdiff_t>(sent),
                                              payload.begin()
                                                  + static_cast<std::ptrdiff_t>(sent + count));
        send_and_expect_ack(static_cast<std::uint16_t>(BslCommand::MidstData), chunk,
                            what + ": data chunk", core::transfer_timeout_ms(kChunkTimeoutMs));
        sent += count;
        if (m_callbacks.progress) {
            m_callbacks.progress(static_cast<int>((sent * 100) / payload.size()),
                                 "sending " + what + ": " + std::to_string(sent) + " of "
                                     + std::to_string(payload.size()) + " bytes");
        }
    }

    send_and_expect_ack(static_cast<std::uint16_t>(BslCommand::EndData), {},
                        what + ": end of transfer");
    if (execute) {
        send_and_expect_ack(static_cast<std::uint16_t>(BslCommand::ExecData), {},
                            what + ": execute");
        if (m_callbacks.log) {
            log("ok", what + " sent (" + std::to_string(payload.size()) + " bytes) and started at 0x"
                            + [&] {
                                  char buffer[16];
                                  std::snprintf(buffer, sizeof(buffer), "%08x",
                                                address + kBslPayloadEntryOffset);
                                  return std::string(buffer);
                              }());
        }
    }
}

// --- device information ------------------------------------------------------
std::vector<std::uint8_t> BslSession::read_chip_type() {
    send_frame(static_cast<std::uint16_t>(BslCommand::ReadChipType), {});
    const BslFrame reply = read_frame(kCommandTimeoutMs);
    if (reply.type == static_cast<std::uint16_t>(BslResponse::ReadChipTypeResult)) {
        return reply.data;
    }
    throw ProtocolError("read chip type: the device answered " + describe_response(reply.type));
}

std::vector<std::uint8_t> BslSession::read_flash_info() {
    send_frame(static_cast<std::uint16_t>(BslCommand::ReadFlashInfo), {});
    const BslFrame reply = read_frame(kCommandTimeoutMs);
    if (reply.type == static_cast<std::uint16_t>(BslResponse::ReadFlashInfoResult)) {
        return reply.data;
    }
    throw ProtocolError("read flash info: the device answered " + describe_response(reply.type));
}

std::vector<std::uint8_t> BslSession::read_flash_type() {
    send_frame(static_cast<std::uint16_t>(BslCommand::ReadFlashType), {});
    const BslFrame reply = read_frame(kCommandTimeoutMs);
    if (reply.type == static_cast<std::uint16_t>(BslResponse::ReadFlashTypeResult)) {
        return reply.data;
    }
    throw ProtocolError("read flash type: the device answered " + describe_response(reply.type));
}

std::vector<std::uint8_t> BslSession::read_sector_size() {
    send_frame(static_cast<std::uint16_t>(BslCommand::ReadSectorSize), {});
    const BslFrame reply = read_frame(kCommandTimeoutMs);
    if (reply.type == static_cast<std::uint16_t>(BslResponse::ReadSectorSizeResult)) {
        return reply.data;
    }
    throw ProtocolError("read sector size: the device answered " + describe_response(reply.type));
}

std::vector<std::uint8_t> BslSession::read_chip_uid() {
    send_frame(static_cast<std::uint16_t>(BslCommand::ReadChipUid), {});
    const BslFrame reply = read_frame(kCommandTimeoutMs);
    if (reply.type == static_cast<std::uint16_t>(BslResponse::ReadChipUidResult)) {
        return reply.data;
    }
    throw ProtocolError("read chip uid: the device answered " + describe_response(reply.type));
}

ChipInfo BslSession::read_device_info() {
    ChipInfo info;
    if (!m_connected) {
        throw ProtocolError("the device is not connected yet: run the handshake first");
    }

    // Every one of these is optional to the device. An older agent that does not
    // implement a query answers with an error rather than failing the link, so
    // each miss is logged and left as a gap in the report.
    try {
        const std::vector<std::uint8_t> data = read_chip_type();
        if (data.size() >= 4) {
            info.have_chip_type = true;
            info.chip_type = read_be32(data.data());
        }
    } catch (const ProtocolError& error) {
        log("warn", std::string("the device did not report a chip type: ") + error.what());
    }

    try {
        const std::vector<std::uint8_t> data = read_flash_type();
        if (data.size() >= 4) {
            info.have_flash_type = true;
            info.flash_type = read_be32(data.data());
        }
    } catch (const ProtocolError& error) {
        log("warn", std::string("the device did not report a flash type: ") + error.what());
    }

    try {
        const std::vector<std::uint8_t> data = read_flash_info();
        info.have_flash_info = true;
        info.flash_info = data;
    } catch (const ProtocolError& error) {
        log("warn", std::string("the device did not report flash information: ") + error.what());
    }

    try {
        const std::vector<std::uint8_t> data = read_sector_size();
        if (data.size() >= 4) {
            info.have_sector_size = true;
            info.sector_size = read_be32(data.data());
        }
    } catch (const ProtocolError&) {
        // A boot ROM that predates the query. Not worth a line of its own; the
        // summary below says what was found.
    }

    try {
        const std::vector<std::uint8_t> data = read_chip_uid();
        info.have_chip_uid = true;
        info.chip_uid = data;
    } catch (const ProtocolError&) {
    }

    return info;
}

// --- flash operations --------------------------------------------------------
void BslSession::erase_flash(std::uint32_t address, std::uint32_t length) {
    if (length == 0) {
        throw ProtocolError("refusing to erase a zero-length region");
    }
    std::vector<std::uint8_t> data;
    put_be32(data, address);
    put_be32(data, length);
    log("info", "erasing " + std::to_string(length) + " bytes at 0x"
                    + [&] {
                          char buffer[16];
                          std::snprintf(buffer, sizeof(buffer), "%08x", address);
                          return std::string(buffer);
                      }());
    send_and_expect_ack(static_cast<std::uint16_t>(BslCommand::EraseFlash), data, "erase flash",
                        kFlashTimeoutMs);
    log("ok", "the device accepted the erase command");
}

std::vector<std::uint8_t> BslSession::read_flash(std::uint32_t address, std::uint32_t length) {
    if (length == 0) {
        throw ProtocolError("refusing to read a zero-length region");
    }
    // The same ceiling the other vendors' read paths use: a read is held in
    // memory, so an implausible length is refused before anything moves.
    constexpr std::uint32_t kMaxRead = 512u * 1024u * 1024u;
    if (length > kMaxRead) {
        throw ProtocolError("the read is " + std::to_string(length / (1024 * 1024))
                            + " MiB, above the " + std::to_string(kMaxRead / (1024 * 1024))
                            + " MiB this build holds in memory");
    }

    std::vector<std::uint8_t> request;
    put_be32(request, address);
    put_be32(request, length);
    send_and_expect_ack(static_cast<std::uint16_t>(BslCommand::ReadStart), request,
                        "read flash: start");

    std::vector<std::uint8_t> out;
    out.reserve(length);
    std::size_t received = 0;
    while (received < length) {
        check_cancelled();

        // READ_MIDST asks for the next piece. The device answers either with
        // data or with the "no more" code, and both are normal.
        std::vector<std::uint8_t> chunk_request;
        put_be32(chunk_request, std::min<std::uint32_t>(kBslMaxData, length - static_cast<std::uint32_t>(received)));
        send_frame(static_cast<std::uint16_t>(BslCommand::ReadMidst), chunk_request);

        const BslFrame reply = read_frame(core::transfer_timeout_ms(kFlashTimeoutMs));
        if (reply.type == static_cast<std::uint16_t>(BslResponse::ReadFlashResult)) {
            out.insert(out.end(), reply.data.begin(), reply.data.end());
            received += reply.data.size();
            if (m_callbacks.progress) {
                m_callbacks.progress(static_cast<int>((received * 100) / length),
                                     "reading: " + std::to_string(received) + " of "
                                         + std::to_string(length) + " bytes");
            }
            continue;
        }
        if (reply.type == static_cast<std::uint16_t>(BslResponse::Ack)) {
            break;  // the device says there is no more
        }
        throw ProtocolError("read flash: the device answered " + describe_response(reply.type));
    }

    send_and_expect_ack(static_cast<std::uint16_t>(BslCommand::ReadEnd), {}, "read flash: end",
                        kFlashTimeoutMs);
    if (out.size() > length) {
        out.resize(length);
    }
    return out;
}

void BslSession::reset() {
    send_and_expect_ack(static_cast<std::uint16_t>(BslCommand::NormalReset), {}, "reset");
    log("info", "the device has been told to reset out of download mode");
}

void BslSession::power_off() {
    send_and_expect_ack(static_cast<std::uint16_t>(BslCommand::PowerOff), {}, "power off");
}

}  // namespace huaxin::protocols::spd
