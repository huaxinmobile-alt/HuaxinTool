#include "protocols/qualcomm/sahara.h"

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstring>
#include <initializer_list>
#include <stdexcept>
#include <utility>

namespace huaxin::protocols::qualcomm {

namespace {

/// Refuses obviously bogus length fields before allocating. The largest
/// legitimate Sahara packet is 0x30 bytes; a device claiming megabytes is
/// either confused or hostile.
constexpr std::uint32_t kMaxPacketLength = 64 * 1024;

/// Digest sizes a PK hash can legitimately be, from qdl's pkhash trimming.
constexpr std::array<std::size_t, 3> kDigestSizes{32, 48, 64};

std::string quote_command(std::uint32_t command) {
    char buffer[16];
    std::snprintf(buffer, sizeof(buffer), "0x%02x", command);
    return buffer;
}

}  // namespace

// --- little-endian helpers ---------------------------------------------------
std::uint16_t read_le16(const std::uint8_t* data) noexcept {
    return static_cast<std::uint16_t>(data[0] | (data[1] << 8));
}

std::uint32_t read_le32(const std::uint8_t* data) noexcept {
    return static_cast<std::uint32_t>(data[0]) | (static_cast<std::uint32_t>(data[1]) << 8)
           | (static_cast<std::uint32_t>(data[2]) << 16) | (static_cast<std::uint32_t>(data[3]) << 24);
}

std::uint64_t read_le64(const std::uint8_t* data) noexcept {
    return static_cast<std::uint64_t>(read_le32(data))
           | (static_cast<std::uint64_t>(read_le32(data + 4)) << 32);
}

void write_le32(std::uint8_t* out, std::uint32_t value) noexcept {
    out[0] = static_cast<std::uint8_t>(value & 0xFF);
    out[1] = static_cast<std::uint8_t>((value >> 8) & 0xFF);
    out[2] = static_cast<std::uint8_t>((value >> 16) & 0xFF);
    out[3] = static_cast<std::uint8_t>((value >> 24) & 0xFF);
}

void write_le64(std::uint8_t* out, std::uint64_t value) noexcept {
    write_le32(out, static_cast<std::uint32_t>(value & 0xFFFFFFFFu));
    write_le32(out + 4, static_cast<std::uint32_t>(value >> 32));
}

std::string hex(const std::uint8_t* data, std::size_t size, bool spaced) {
    static constexpr char kDigits[] = "0123456789abcdef";
    std::string text;
    text.reserve(size * (spaced ? 3 : 2));
    for (std::size_t index = 0; index < size; ++index) {
        if (spaced && index != 0) {
            text.push_back(' ');
        }
        text.push_back(kDigits[(data[index] >> 4) & 0xF]);
        text.push_back(kDigits[data[index] & 0xF]);
    }
    return text;
}

// --- encoding ----------------------------------------------------------------
namespace {

/// Builds a header + payload packet with `length` filled in from the payload.
std::vector<std::uint8_t> build_packet(SaharaCommand command, std::uint32_t total_length) {
    std::vector<std::uint8_t> packet(total_length, 0);
    write_le32(packet.data(), static_cast<std::uint32_t>(command));
    write_le32(packet.data() + 4, total_length);
    return packet;
}

/// Copies `count` little-endian words starting at packet offset 8.
void put_words(std::vector<std::uint8_t>& packet, std::initializer_list<std::uint32_t> words) {
    std::size_t offset = kPacketHeaderLength;
    for (std::uint32_t word : words) {
        write_le32(packet.data() + offset, word);
        offset += 4;
    }
}

}  // namespace

std::vector<std::uint8_t> encode_hello_response(const SaharaHelloResponse& response) {
    std::vector<std::uint8_t> packet = build_packet(SaharaCommand::HelloResponse, kHelloLength);
    put_words(packet, {response.version, response.compatible, response.status, response.mode});
    // reserved[] stays zero: the 6 words the device expects but does not read.
    for (std::size_t index = 0; index < 6; ++index) {
        write_le32(packet.data() + kPacketHeaderLength + 16 + index * 4, response.reserved[index]);
    }
    return packet;
}

std::vector<std::uint8_t> encode_end_of_image(const SaharaEndOfImage& end_of_image) {
    std::vector<std::uint8_t> packet = build_packet(SaharaCommand::EndOfImage, kEndOfImageLength);
    put_words(packet, {end_of_image.image, end_of_image.status});
    return packet;
}

std::vector<std::uint8_t> encode_execute(const SaharaExecuteRequest& request) {
    std::vector<std::uint8_t> packet = build_packet(SaharaCommand::Execute, kExecuteLength);
    put_words(packet, {request.client_command});
    return packet;
}

std::vector<std::uint8_t> encode_execute_data(const SaharaExecuteRequest& request) {
    std::vector<std::uint8_t> packet = build_packet(SaharaCommand::ExecuteData, kExecuteLength);
    put_words(packet, {request.client_command});
    return packet;
}

std::vector<std::uint8_t> encode_reset() {
    return build_packet(SaharaCommand::Reset, kResetLength);
}

std::vector<std::uint8_t> encode_done_response(std::uint32_t status) {
    std::vector<std::uint8_t> packet = build_packet(SaharaCommand::DoneResponse, kDoneResponseLength);
    put_words(packet, {status});
    return packet;
}

std::vector<std::uint8_t> encode_switch_mode(SaharaMode mode) {
    std::vector<std::uint8_t> packet = build_packet(SaharaCommand::SwitchMode, kSwitchModeLength);
    put_words(packet, {static_cast<std::uint32_t>(mode)});
    return packet;
}

// --- decoding ----------------------------------------------------------------
bool decode_header(const std::uint8_t* data, std::size_t size,
                   std::uint32_t& command, std::uint32_t& length, std::string& error) {
    if (data == nullptr || size < kPacketHeaderLength) {
        error = "packet shorter than the 8-byte Sahara header";
        return false;
    }
    command = read_le32(data);
    length = read_le32(data + 4);
    return true;
}

namespace {

/// Shared preamble: checks the header, the expected command and the length.
bool check_packet(const std::uint8_t* data, std::size_t size, SaharaCommand expected,
                  std::uint32_t expected_length, std::string& error) {
    std::uint32_t command = 0;
    std::uint32_t length = 0;
    if (!decode_header(data, size, command, length, error)) {
        return false;
    }
    if (length != expected_length) {
        error = "packet length " + std::to_string(length) + " does not match the "
                + std::to_string(expected_length) + " bytes this command carries";
        return false;
    }
    if (size < length) {
        error = "truncated packet: header claims " + std::to_string(length)
                + " bytes, only " + std::to_string(size) + " arrived";
        return false;
    }
    if (command != static_cast<std::uint32_t>(expected)) {
        error = "unexpected command " + quote_command(command) + ", expected "
                + quote_command(static_cast<std::uint32_t>(expected));
        return false;
    }
    return true;
}

}  // namespace

bool decode_hello_request(const std::uint8_t* data, std::size_t size,
                          SaharaHelloRequest& out, std::string& error) {
    if (!check_packet(data, size, SaharaCommand::Hello, kHelloLength, error)) {
        return false;
    }
    const std::uint8_t* payload = data + kPacketHeaderLength;
    out.version = read_le32(payload);
    out.compatible = read_le32(payload + 4);
    out.max_length = read_le32(payload + 8);
    out.mode = read_le32(payload + 12);
    for (std::size_t index = 0; index < 6; ++index) {
        out.reserved[index] = read_le32(payload + 16 + index * 4);
    }
    return true;
}

bool decode_hello_response(const std::uint8_t* data, std::size_t size,
                           SaharaHelloResponse& out, std::string& error) {
    if (!check_packet(data, size, SaharaCommand::HelloResponse, kHelloLength, error)) {
        return false;
    }
    const std::uint8_t* payload = data + kPacketHeaderLength;
    out.version = read_le32(payload);
    out.compatible = read_le32(payload + 4);
    out.status = read_le32(payload + 8);
    out.mode = read_le32(payload + 12);
    for (std::size_t index = 0; index < 6; ++index) {
        out.reserved[index] = read_le32(payload + 16 + index * 4);
    }
    return true;
}

bool decode_read_data_request(const std::uint8_t* data, std::size_t size,
                              SaharaReadDataRequest& out, std::string& error) {
    if (!check_packet(data, size, SaharaCommand::ReadData, kReadDataLength, error)) {
        return false;
    }
    const std::uint8_t* payload = data + kPacketHeaderLength;
    out.image = read_le32(payload);
    out.offset = read_le32(payload + 4);
    out.length = read_le32(payload + 8);
    return true;
}

bool decode_read_data64_request(const std::uint8_t* data, std::size_t size,
                                SaharaReadData64Request& out, std::string& error) {
    if (!check_packet(data, size, SaharaCommand::ReadData64, kReadData64Length, error)) {
        return false;
    }
    const std::uint8_t* payload = data + kPacketHeaderLength;
    out.image = read_le64(payload);
    out.offset = read_le64(payload + 8);
    out.length = read_le64(payload + 16);
    return true;
}

bool decode_execute_response(const std::uint8_t* data, std::size_t size,
                             SaharaExecuteResponse& out, std::string& error) {
    if (!check_packet(data, size, SaharaCommand::ExecuteResponse, kExecuteResponseLength, error)) {
        return false;
    }
    const std::uint8_t* payload = data + kPacketHeaderLength;
    out.client_command = read_le32(payload);
    out.data_length = read_le32(payload + 4);
    return true;
}

// --- PK hash trimming --------------------------------------------------------
namespace {

/// Normalises the PK hash payload.
///
/// Transcribed from qdl's sahara_pkhash_trim(). Devices return the digest
/// repeated to fill the transfer buffer and/or zero-padded, so: cut the buffer
/// down to one repeat if it is periodic, drop trailing zeros (which are padding,
/// not part of the digest), then round up to the nearest real digest size -
/// without ever growing past what was actually received.
std::size_t trim_pk_hash(const std::uint8_t* buffer, std::size_t length) {
    const std::size_t original = length;

    for (std::size_t index = 4; index * 2 <= length; ++index) {
        if (std::memcmp(buffer, buffer + index, index) == 0) {
            length = index;
            break;
        }
    }

    while (length > 0 && buffer[length - 1] == 0) {
        --length;
    }

    for (std::size_t digest : kDigestSizes) {
        if (length <= digest && digest <= original) {
            length = digest;
            break;
        }
    }
    return length;
}

}  // namespace

// --- session -----------------------------------------------------------------
SaharaSession::SaharaSession(IByteTransport& transport, Callbacks callbacks)
    : m_transport(transport), m_callbacks(std::move(callbacks)) {}

void SaharaSession::log(const std::string& level, const std::string& message) const {
    if (m_callbacks.log) {
        m_callbacks.log(level, message);
    }
}

void SaharaSession::check_cancelled() const {
    if (m_callbacks.cancelled && m_callbacks.cancelled()) {
        throw ProtocolError("cancelled by the operator");
    }
}

namespace {

/// Reads one complete packet: header first, then the payload the header declares.
std::vector<std::uint8_t> read_packet(IByteTransport& transport, unsigned int timeout_ms) {
    std::vector<std::uint8_t> packet(kPacketHeaderLength);
    transport.read_exact(packet.data(), kPacketHeaderLength, timeout_ms);

    std::uint32_t command = 0;
    std::uint32_t length = 0;
    std::string error;
    if (!decode_header(packet.data(), packet.size(), command, length, error)) {
        throw ProtocolError("bad Sahara header: " + error);
    }
    if (length < kPacketHeaderLength) {
        throw ProtocolError("Sahara packet claims a length of " + std::to_string(length)
                            + ", shorter than its own header");
    }
    if (length > kMaxPacketLength) {
        throw ProtocolError("Sahara packet length " + std::to_string(length)
                            + " exceeds the " + std::to_string(kMaxPacketLength) + " byte limit");
    }

    packet.resize(length);
    if (length > kPacketHeaderLength) {
        transport.read_exact(packet.data() + kPacketHeaderLength, length - kPacketHeaderLength, timeout_ms);
    }
    return packet;
}

void describe_hello(const SaharaHelloRequest& hello, const std::string& level,
                    const SaharaSession::Callbacks& callbacks) {
    if (!callbacks.log) {
        return;
    }
    callbacks.log(level, "HELLO: version=" + std::to_string(hello.version)
                             + " compatible=" + std::to_string(hello.compatible)
                             + " max_len=" + std::to_string(hello.max_length)
                             + " mode=" + std::to_string(hello.mode));
}

}  // namespace

SaharaDeviceInfo SaharaSession::run(const ImageTable& images) {
    log("info", "waiting for the device to announce itself (SAHARA_HELLO_REQ)");
    std::vector<std::uint8_t> packet = read_packet(m_transport, kSaharaHelloTimeoutMs);

    std::uint32_t command = 0;
    std::uint32_t length = 0;
    std::string error;
    decode_header(packet.data(), packet.size(), command, length, error);
    if (command != static_cast<std::uint32_t>(SaharaCommand::Hello)) {
        throw ProtocolError("expected SAHARA_HELLO_REQ (0x01) but the device sent " + quote_command(command));
    }

    SaharaHelloRequest hello;
    if (!decode_hello_request(packet.data(), packet.size(), hello, error)) {
        throw ProtocolError("malformed SAHARA_HELLO_REQ: " + error);
    }
    m_info.protocol_version = hello.version;
    m_info.device_mode = hello.mode;
    describe_hello(hello, "info", m_callbacks);

    // Echo the mode the device asked for. For a normal image transfer that is
    // IMAGE_TX_PENDING (0), which tells it to start requesting data.
    SaharaHelloResponse response;
    response.version = (hello.version < kSaharaVersion) ? hello.version : kSaharaVersion;
    response.compatible = 1;
    response.status = kSaharaSuccess;
    response.mode = hello.mode;
    const std::vector<std::uint8_t> hello_response = encode_hello_response(response);
    m_transport.write_all(hello_response.data(), hello_response.size(), kSaharaCommandTimeoutMs);
    log("debug", "sent SAHARA_HELLO_RSP (version " + std::to_string(response.version)
                     + ", mode " + std::to_string(response.mode) + ")");

    if (images.empty()) {
        log("warn", "no images are loaded, so any READ_DATA request will fail");
    }

    bool done = false;
    while (!done) {
        check_cancelled();
        packet = read_packet(m_transport, kSaharaCommandTimeoutMs);
        decode_header(packet.data(), packet.size(), command, length, error);

        switch (static_cast<SaharaCommand>(command)) {
            case SaharaCommand::ReadData:
            case SaharaCommand::ReadData64: {
                const bool wide = command == static_cast<std::uint32_t>(SaharaCommand::ReadData64);
                std::uint32_t image_id = 0;
                std::uint64_t offset = 0;
                std::uint64_t count = 0;

                if (wide) {
                    SaharaReadData64Request request;
                    if (!decode_read_data64_request(packet.data(), packet.size(), request, error)) {
                        throw ProtocolError("malformed SAHARA_READ_DATA64: " + error);
                    }
                    image_id = static_cast<std::uint32_t>(request.image);
                    offset = request.offset;
                    count = request.length;
                } else {
                    SaharaReadDataRequest request;
                    if (!decode_read_data_request(packet.data(), packet.size(), request, error)) {
                        throw ProtocolError("malformed SAHARA_READ_DATA: " + error);
                    }
                    image_id = request.image;
                    offset = request.offset;
                    count = request.length;
                }

                const auto found = images.find(image_id);
                if (found == images.end()) {
                    std::string available;
                    for (const auto& entry : images) {
                        available += (available.empty() ? "" : ", ") + std::to_string(entry.first);
                    }
                    throw ProtocolError("the device asked for image id " + std::to_string(image_id)
                                        + " which is not loaded (loaded ids: "
                                        + (available.empty() ? "none" : available) + ")");
                }

                const std::vector<std::uint8_t>& image = found->second;
                if (offset > image.size() || count > image.size() - offset) {
                    throw ProtocolError("the device asked for " + std::to_string(count)
                                        + " bytes at offset " + std::to_string(offset)
                                        + " of image " + std::to_string(image_id)
                                        + ", which is only " + std::to_string(image.size()) + " bytes");
                }

                log("debug", "sending image " + std::to_string(image_id) + " offset "
                                 + std::to_string(offset) + " length " + std::to_string(count));
                if (count > 0) {
                    m_transport.write_all(image.data() + offset, static_cast<std::size_t>(count),
                                          kSaharaCommandTimeoutMs);
                }

                SaharaEndOfImage end_of_image;
                end_of_image.image = image_id;
                end_of_image.status = kSaharaSuccess;
                const std::vector<std::uint8_t> eoi = encode_end_of_image(end_of_image);
                m_transport.write_all(eoi.data(), eoi.size(), kSaharaCommandTimeoutMs);

                if (m_callbacks.progress && !image.empty()) {
                    const std::size_t sent = static_cast<std::size_t>(offset + count);
                    const int percent = static_cast<int>(std::min<std::size_t>(100, sent * 100 / image.size()));
                    m_callbacks.progress(percent, "image " + std::to_string(image_id));
                }
                break;
            }

            case SaharaCommand::Done: {
                log("info", "device reported DONE_REQ, finishing the handshake");
                const std::vector<std::uint8_t> done_response = encode_done_response();
                m_transport.write_all(done_response.data(), done_response.size(), kSaharaCommandTimeoutMs);
                done = true;
                break;
            }

            case SaharaCommand::ResetResponse: {
                // Some devices answer with RESET_RESP instead of DONE_REQ. qdl
                // treats that as a completed image transfer too.
                log("info", "device answered RESET_RESP; treating the transfer as complete");
                done = true;
                break;
            }

            default:
                // Unknown or unexpected packets are logged, not fatal: the device
                // is the one driving this conversation.
                log("warn", "ignoring unexpected Sahara packet " + quote_command(command)
                                + " (" + std::to_string(length) + " bytes)");
                break;
        }
    }

    log("ok", "Sahara handshake complete");
    return m_info;
}

static std::vector<std::uint8_t> execute_command(IByteTransport& transport, SaharaExecCommand client,
                                                 std::size_t max_payload,
                                                 const SaharaSession::Callbacks& callbacks) {
    // EXECUTE carries the client command; the device answers EXECUTE_RESP with
    // the length of the data it is willing to hand over, and only then does
    // EXECUTE_DATA actually transfer it. Both steps are required - the device
    // ignores EXECUTE_DATA that was not preceded by a matching EXECUTE.
    SaharaExecuteRequest request;
    request.client_command = static_cast<std::uint32_t>(client);
    const std::vector<std::uint8_t> encoded = encode_execute(request);
    transport.write_all(encoded.data(), encoded.size(), kSaharaCommandTimeoutMs);

    const std::vector<std::uint8_t> response = read_packet(transport, kSaharaCommandTimeoutMs);
    SaharaExecuteResponse execute_response;
    std::string error;
    if (!decode_execute_response(response.data(), response.size(), execute_response, error)) {
        throw ProtocolError("EXECUTE " + quote_command(request.client_command)
                            + " was not answered with EXECUTE_RESP: " + error);
    }
    if (execute_response.data_length == 0) {
        if (callbacks.log) {
            callbacks.log("warn", "device reports no data for EXECUTE command "
                                      + std::to_string(execute_response.client_command));
        }
        return {};
    }
    if (execute_response.data_length > max_payload) {
        throw ProtocolError("EXECUTE response claims " + std::to_string(execute_response.data_length)
                            + " bytes, more than the " + std::to_string(max_payload) + " byte limit");
    }

    const std::vector<std::uint8_t> data_request = encode_execute_data(request);
    transport.write_all(data_request.data(), data_request.size(), kSaharaCommandTimeoutMs);

    std::vector<std::uint8_t> payload(execute_response.data_length);
    transport.read_exact(payload.data(), payload.size(), kSaharaCommandTimeoutMs);
    return payload;
}

SaharaDeviceInfo SaharaSession::query_device_identity(SaharaDeviceInfo info, bool return_to_image_transfer) {
    // Command mode is a separate conversation from the image transfer: the host
    // must announce it in HELLO_RESP. The device then accepts EXECUTE commands
    // instead of asking for image data. Leaving command mode (SWITCH_MODE back to
    // IMAGE_TX_PENDING) makes the device re-send HELLO, which is what run() then
    // picks up - so this must be called *before* run(), never in the middle of it.
    log("info", "entering Sahara command mode to read device identity");
    std::vector<std::uint8_t> packet = read_packet(m_transport, kSaharaHelloTimeoutMs);

    std::uint32_t command = 0;
    std::uint32_t length = 0;
    std::string error;
    decode_header(packet.data(), packet.size(), command, length, error);
    if (command != static_cast<std::uint32_t>(SaharaCommand::Hello)) {
        throw ProtocolError("expected SAHARA_HELLO_REQ before entering command mode, got "
                            + quote_command(command));
    }

    SaharaHelloRequest hello;
    if (!decode_hello_request(packet.data(), packet.size(), hello, error)) {
        throw ProtocolError("malformed SAHARA_HELLO_REQ: " + error);
    }
    info.protocol_version = hello.version;
    info.device_mode = hello.mode;
    describe_hello(hello, "info", m_callbacks);

    SaharaHelloResponse response;
    response.version = (hello.version < kSaharaVersion) ? hello.version : kSaharaVersion;
    response.compatible = 1;
    response.status = kSaharaSuccess;
    response.mode = static_cast<std::uint32_t>(SaharaMode::Command);
    const std::vector<std::uint8_t> encoded = encode_hello_response(response);
    m_transport.write_all(encoded.data(), encoded.size(), kSaharaCommandTimeoutMs);
    log("debug", "requested SAHARA_MODE_COMMAND");

    constexpr std::size_t kMaxPayload = 4096;

    // 1. Serial number - answered by every protocol version.
    try {
        const std::vector<std::uint8_t> payload =
            execute_command(m_transport, SaharaExecCommand::SerialNumber, kMaxPayload, m_callbacks);
        if (payload.size() >= 4) {
            info.serial = read_le32(payload.data());
            info.have_serial = true;
            log("ok", "chip serial: 0x" + hex(payload.data(), 4));
        }
    } catch (const ProtocolError& failure) {
        log("warn", std::string("could not read the serial number: ") + failure.what());
    }

    // 2. Chip identity. Pre-v3 targets answer MSM_HW_ID_READ; from v3 that
    //    command was removed in favour of READ_CHIP_ID_V3, whose layout is
    //    different - reading the wrong one yields plausible nonsense, so the
    //    protocol version decides which to ask for.
    try {
        if (info.protocol_version < 3) {
            const std::vector<std::uint8_t> payload =
                execute_command(m_transport, SaharaExecCommand::MsmHardwareId, kMaxPayload, m_callbacks);
            if (payload.size() >= 8) {
                info.hardware_id = read_le64(payload.data());
                info.msm_id = static_cast<std::uint32_t>(info.hardware_id >> 32);
                info.oem_id = static_cast<std::uint32_t>((info.hardware_id >> 16) & 0xFFFF);
                info.model_id = static_cast<std::uint32_t>(info.hardware_id & 0xFFFF);
                info.have_hardware_id = true;
            }
        } else {
            const std::vector<std::uint8_t> payload =
                execute_command(m_transport, SaharaExecCommand::ReadChipIdV3, kMaxPayload, m_callbacks);
            if (payload.size() >= 44) {
                info.msm_id = read_le32(payload.data() + 36);
                info.oem_id = read_le16(payload.data() + 40);
                info.model_id = read_le16(payload.data() + 42);
                // Some v3 targets carry the OEM id in an alternate slot.
                if (info.oem_id == 0 && payload.size() >= 46) {
                    info.oem_id = read_le16(payload.data() + 44);
                }
                info.hardware_id = (static_cast<std::uint64_t>(info.msm_id) << 32)
                                   | (static_cast<std::uint64_t>(info.oem_id) << 16) | info.model_id;
                info.have_hardware_id = true;
            }
        }
    } catch (const ProtocolError& failure) {
        log("warn", std::string("could not read the chip identity: ") + failure.what());
    }

    // 3. OEM public key hash.
    try {
        const std::vector<std::uint8_t> payload =
            execute_command(m_transport, SaharaExecCommand::OemPublicKeyHash, kMaxPayload, m_callbacks);
        if (!payload.empty()) {
            const std::size_t length = trim_pk_hash(payload.data(), payload.size());
            info.pk_hash = hex(payload.data(), length);
            info.have_pk_hash = true;
        }
    } catch (const ProtocolError& failure) {
        log("warn", std::string("could not read the OEM PK hash: ") + failure.what());
    }

    // TODO: map info.msm_id to a marketing chip name (e.g. "Snapdragon 855").
    // Deliberately left empty: the MSM id table has not been verified against a
    // primary source, and a wrong chip name is worse than none.

    if (return_to_image_transfer) {
        // Hand the device back to image-transfer mode so a following run() sees
        // a fresh HELLO.
        const std::vector<std::uint8_t> switch_mode = encode_switch_mode(SaharaMode::ImageTransferPending);
        m_transport.write_all(switch_mode.data(), switch_mode.size(), kSaharaCommandTimeoutMs);
        log("debug", "left command mode, back to image transfer");
    } else {
        // Nothing is going to be uploaded, so put the device back to its initial
        // state instead of leaving it waiting for image data that will never come.
        const std::vector<std::uint8_t> reset = encode_reset();
        m_transport.write_all(reset.data(), reset.size(), kSaharaCommandTimeoutMs);
        log("debug", "sent SAHARA_RESET to return the device to its initial state");
    }

    m_info = info;
    return info;
}

}  // namespace huaxin::protocols::qualcomm
