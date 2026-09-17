// =============================================================================
//  Native tests for the Qualcomm EDL protocol layer.
//
//  These run without any hardware. The packet codecs are pure functions, and the
//  Sahara handshake is driven through a ScriptedTransport that plays a recorded
//  conversation back and records what the host sends - which is exactly what
//  cannot be checked by hand without a phone in EDL mode.
//
//  Every expected byte sequence here comes from the structures transcribed from
//  linux-msm/qdl, so a passing test means the host emits what the device expects.
// =============================================================================

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "protocols/qualcomm/firehose.h"
#include "protocols/qualcomm/gpt.h"
#include "protocols/qualcomm/sahara.h"

using namespace huaxin::protocols::qualcomm;

namespace {

int g_failures = 0;
int g_checks = 0;

void check(const std::string& description, bool passed, const std::string& detail = "") {
    ++g_checks;
    if (!passed) {
        ++g_failures;
    }
    std::printf("  [%s] %s%s\n", passed ? "PASS" : "FAIL", description.c_str(),
                detail.empty() ? "" : ("  (" + detail + ")").c_str());
    std::fflush(stdout);
}

std::string to_hex(const std::vector<std::uint8_t>& data) {
    return hex(data.data(), data.size(), true);
}

/// There is no write_le16 in the protocol layer - nothing on the wire needs one -
/// so the tests that build a GPT by hand carry their own.
void write_u16(std::uint8_t* out, std::uint16_t value) {
    out[0] = static_cast<std::uint8_t>(value & 0xff);
    out[1] = static_cast<std::uint8_t>((value >> 8) & 0xff);
}

/// A GPT header sector, built to the UEFI spec. `entry_count` and `entry_size`
/// are parameters because the parser's validation is part of what is under test.
std::vector<std::uint8_t> make_gpt_header(std::uint32_t entry_count = 128,
                                          std::uint32_t entry_size = 128,
                                          std::uint64_t array_lba = 2) {
    std::vector<std::uint8_t> sector(512, 0);
    std::memcpy(sector.data(), GptHeader::kSignature, 8);
    write_le32(sector.data() + 8, 0x00010000);   // revision 1.0
    write_le32(sector.data() + 12, 92);          // header size
    write_le64(sector.data() + 24, 1);           // current LBA
    write_le64(sector.data() + 32, 127);         // backup LBA
    write_le64(sector.data() + 40, 34);          // first usable
    write_le64(sector.data() + 48, 126);         // last usable
    write_le32(sector.data() + 56, 0xdeadbeef);  // disk GUID, 4+2+2+8
    write_u16(sector.data() + 60, 0x1111);
    write_u16(sector.data() + 62, 0x2222);
    const std::uint8_t tail[8] = {0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff, 0x00, 0x11};
    std::memcpy(sector.data() + 64, tail, 8);
    write_le64(sector.data() + 72, array_lba);
    write_le32(sector.data() + 80, entry_count);
    write_le32(sector.data() + 84, entry_size);
    return sector;
}

/// A transport that answers from a script and remembers what was written.
class ScriptedTransport final : public IByteTransport {
public:
    explicit ScriptedTransport(std::vector<std::vector<std::uint8_t>> responses)
        : m_responses(std::move(responses)) {}

    void write_all(const std::uint8_t* data, std::size_t size, unsigned int) override {
        m_written.emplace_back(data, data + size);
    }

    void read_exact(std::uint8_t* buffer, std::size_t size, unsigned int) override {
        if (m_read_index >= m_responses.size()) {
            throw ProtocolError("script exhausted: the host read more than the test provided");
        }
        const std::vector<std::uint8_t>& next = m_responses[m_read_index];
        if (next.size() < m_offset + size) {
            throw ProtocolError("script entry is shorter than the read");
        }
        std::memcpy(buffer, next.data() + m_offset, size);
        m_offset += size;
        if (m_offset >= next.size()) {
            ++m_read_index;
            m_offset = 0;
        }
    }

    std::size_t read_some(std::uint8_t* buffer, std::size_t size, unsigned int) override {
        if (m_read_index >= m_responses.size()) {
            return 0;
        }
        const std::vector<std::uint8_t>& next = m_responses[m_read_index];
        const std::size_t available = std::min(size, next.size() - m_offset);
        std::memcpy(buffer, next.data() + m_offset, available);
        m_offset += available;
        if (m_offset >= next.size()) {
            ++m_read_index;
            m_offset = 0;
        }
        return available;
    }

    std::string describe() const override { return "scripted"; }

    const std::vector<std::vector<std::uint8_t>>& written() const { return m_written; }

private:
    std::vector<std::vector<std::uint8_t>> m_responses;
    std::vector<std::vector<std::uint8_t>> m_written;
    std::size_t m_read_index{0};
    std::size_t m_offset{0};
};

/// Builds the device-side packet the test wants the host to receive.
std::vector<std::uint8_t> make_hello(std::uint32_t version, std::uint32_t mode) {
    std::vector<std::uint8_t> packet(kHelloLength, 0);
    write_le32(packet.data(), static_cast<std::uint32_t>(SaharaCommand::Hello));
    write_le32(packet.data() + 4, kHelloLength);
    write_le32(packet.data() + 8, version);
    write_le32(packet.data() + 12, 1);          // compatible
    write_le32(packet.data() + 16, 4096);       // max_len
    write_le32(packet.data() + 20, mode);
    return packet;
}

std::vector<std::uint8_t> make_read_data(std::uint32_t image, std::uint32_t offset,
                                         std::uint32_t length) {
    std::vector<std::uint8_t> packet(kReadDataLength, 0);
    write_le32(packet.data(), static_cast<std::uint32_t>(SaharaCommand::ReadData));
    write_le32(packet.data() + 4, kReadDataLength);
    write_le32(packet.data() + 8, image);
    write_le32(packet.data() + 12, offset);
    write_le32(packet.data() + 16, length);
    return packet;
}

std::vector<std::uint8_t> make_done() {
    std::vector<std::uint8_t> packet(kDoneLength, 0);
    write_le32(packet.data(), static_cast<std::uint32_t>(SaharaCommand::Done));
    write_le32(packet.data() + 4, kDoneLength);
    return packet;
}

std::vector<std::uint8_t> make_execute_response(std::uint32_t client_command,
                                                std::uint32_t data_length) {
    std::vector<std::uint8_t> packet(kExecuteResponseLength, 0);
    write_le32(packet.data(), static_cast<std::uint32_t>(SaharaCommand::ExecuteResponse));
    write_le32(packet.data() + 4, kExecuteResponseLength);
    write_le32(packet.data() + 8, client_command);
    write_le32(packet.data() + 12, data_length);
    return packet;
}

/// The response to EXECUTE_DATA: raw payload with no header at all.
std::vector<std::uint8_t> make_raw(const std::vector<std::uint8_t>& payload) {
    return payload;
}

SaharaSession::Callbacks quiet_callbacks() {
    return SaharaSession::Callbacks{};
}

// --- tests -------------------------------------------------------------------

void test_hello_encoding() {
    std::printf("1. SAHARA_HELLO_RSP encoding\n");
    SaharaHelloResponse response;
    response.version = 2;
    response.compatible = 1;
    response.status = 0;
    response.mode = 0;
    const std::vector<std::uint8_t> packet = encode_hello_response(response);

    check("total length is 0x30 (48) as the protocol requires", packet.size() == kHelloLength,
          std::to_string(packet.size()));
    check("header carries command 0x02", read_le32(packet.data()) == 0x2);
    check("header declares the same length", read_le32(packet.data() + 4) == kHelloLength);
    check("version is little-endian at offset 8", read_le32(packet.data() + 8) == 2);
    check("compatible is 1 at offset 12", read_le32(packet.data() + 12) == 1);
    check("status is SUCCESS at offset 16", read_le32(packet.data() + 16) == 0);
    check("mode lands at offset 20", read_le32(packet.data() + 20) == 0);
    check("the reserved words are zero", read_le32(packet.data() + 24) == 0
                                             && read_le32(packet.data() + 44) == 0);
}

void test_little_endian() {
    std::printf("\n2. little-endian codec\n");
    std::uint8_t buffer[8] = {};
    write_le32(buffer, 0x12345678u);
    check("write_le32 stores the low byte first", buffer[0] == 0x78 && buffer[3] == 0x12,
          to_hex({buffer, buffer + 4}));
    check("read_le32 round-trips", read_le32(buffer) == 0x12345678u);

    write_le64(buffer, 0x1122334455667788ull);
    check("write_le64 stores the low byte first", buffer[0] == 0x88 && buffer[7] == 0x11);
    check("read_le64 round-trips", read_le64(buffer) == 0x1122334455667788ull);

    const std::uint8_t pair[2] = {0x34, 0x12};
    check("read_le16 round-trips", read_le16(pair) == 0x1234);
}

void test_decoding_and_validation() {
    std::printf("\n3. packet validation\n");
    SaharaHelloRequest hello;
    std::string error;

    const std::vector<std::uint8_t> good = make_hello(2, 0);
    check("a well-formed HELLO decodes", decode_hello_request(good.data(), good.size(), hello, error),
          error);
    check("version is read", hello.version == 2);
    check("mode is read", hello.mode == 0);
    check("max_len is read", hello.max_length == 4096);

    // A short buffer must be refused rather than read past.
    check("a truncated header is refused",
          !decode_hello_request(good.data(), 4, hello, error), error);

    // Right length, wrong command: this is what a desynchronised stream looks like.
    std::vector<std::uint8_t> wrong = good;
    write_le32(wrong.data(), 0x99);
    check("an unexpected command code is refused",
          !decode_hello_request(wrong.data(), wrong.size(), hello, error), error);

    // Length field disagreeing with the buffer is the classic corruption symptom.
    std::vector<std::uint8_t> bad_length = good;
    write_le32(bad_length.data() + 4, 0x14);
    check("a length that does not match the command is refused",
          !decode_hello_request(bad_length.data(), bad_length.size(), hello, error), error);

    std::vector<std::uint8_t> short_packet(good.begin(), good.begin() + 20);
    check("a truncated payload is refused",
          !decode_hello_request(short_packet.data(), short_packet.size(), hello, error), error);
}

void test_read_data_request() {
    std::printf("\n4. SAHARA_READ_DATA decoding\n");
    const std::vector<std::uint8_t> packet = make_read_data(13, 0x1000, 0x800);
    SaharaReadDataRequest request;
    std::string error;
    check("READ_DATA decodes", decode_read_data_request(packet.data(), packet.size(), request, error),
          error);
    check("image id is read", request.image == 13);
    check("offset is read", request.offset == 0x1000);
    check("length is read", request.length == 0x800);
    check("the packet is 0x14 bytes", packet.size() == kReadDataLength);
}

void test_handshake_upload() {
    std::printf("\n5. Sahara handshake and image upload\n");

    // The device: announces itself, asks for 4 bytes of image 13 at offset 0,
    // then asks for the remaining 4 bytes, then reports DONE.
    const std::vector<std::uint8_t> programmer{0xAA, 0xBB, 0xCC, 0xDD, 0x11, 0x22, 0x33, 0x44};
    ScriptedTransport transport({
        make_hello(2, 0),
        make_read_data(13, 0, 4),
        make_read_data(13, 4, 4),
        make_done(),
    });

    SaharaSession::ImageTable images;
    images.emplace(13, programmer);

    int progress_calls = 0;
    SaharaSession::Callbacks callbacks = quiet_callbacks();
    callbacks.progress = [&](int, const std::string&) { ++progress_calls; };

    SaharaSession session(transport, callbacks);
    const SaharaDeviceInfo info = session.run(images);

    check("the handshake completed", info.protocol_version == 2, std::to_string(info.protocol_version));

    const auto& written = transport.written();
    check("host sent 3 packets: HELLO_RSP, 2 data chunks, 2 EOI, DONE_RSP", written.size() == 6,
          std::to_string(written.size()) + " writes");

    // 1st: HELLO_RSP echoing the device's mode.
    check("HELLO_RSP is command 0x02", read_le32(written[0].data()) == 0x2);
    check("HELLO_RSP is 0x30 bytes", written[0].size() == kHelloLength);
    check("HELLO_RSP echoes the requested mode (image transfer)",
          read_le32(written[0].data() + 20) == 0);

    // 2nd: the first half of the image, sent raw with no header.
    check("the first chunk is the raw image bytes, headerless",
          written[1] == std::vector<std::uint8_t>({0xAA, 0xBB, 0xCC, 0xDD}), to_hex(written[1]));
    // 3rd: END_OF_IMAGE for that chunk.
    check("END_OF_IMAGE follows the chunk", read_le32(written[2].data()) == 0x4);
    check("END_OF_IMAGE is 0x10 bytes", written[2].size() == kEndOfImageLength);
    check("END_OF_IMAGE names image 13", read_le32(written[2].data() + 8) == 13);
    check("END_OF_IMAGE status is SUCCESS", read_le32(written[2].data() + 12) == 0);

    // 4th/5th: second chunk and its EOI - offset was honoured.
    check("the second chunk starts at the requested offset",
          written[3] == std::vector<std::uint8_t>({0x11, 0x22, 0x33, 0x44}), to_hex(written[3]));
    check("the second END_OF_IMAGE follows", read_le32(written[4].data()) == 0x4);

    // 6th: DONE_RSP.
    check("DONE_RSP is command 0x06", read_le32(written[5].data()) == 0x6);
    check("DONE_RSP is 0x0c bytes", written[5].size() == kDoneResponseLength);
    check("progress was reported", progress_calls == 2, std::to_string(progress_calls));
}

void test_handshake_rejects_unknown_image() {
    std::printf("\n6. handshake error handling\n");
    ScriptedTransport transport({
        make_hello(2, 0),
        make_read_data(99, 0, 16),  // an id the host has no data for
    });

    SaharaSession session(transport, quiet_callbacks());
    bool threw = false;
    std::string message;
    try {
        session.run(SaharaSession::ImageTable{});
    } catch (const ProtocolError& error) {
        threw = true;
        message = error.what();
    }
    check("an unknown image id is refused, not faked", threw);
    check("the error names the missing image", message.find("99") != std::string::npos, message);
}

void test_handshake_rejects_out_of_range_read() {
    std::printf("\n7. out-of-range read\n");
    ScriptedTransport transport({
        make_hello(2, 0),
        make_read_data(13, 0, 4096),  // more than the 4-byte image holds
    });
    SaharaSession::ImageTable images;
    images.emplace(13, std::vector<std::uint8_t>{1, 2, 3, 4});

    SaharaSession session(transport, quiet_callbacks());
    bool threw = false;
    try {
        session.run(images);
    } catch (const ProtocolError&) {
        threw = true;
    }
    check("a read past the end of the image is refused", threw);
}

void test_handshake_rejects_oversized_packet() {
    std::printf("\n8. absurd packet length\n");
    std::vector<std::uint8_t> absurd(kPacketHeaderLength, 0);
    write_le32(absurd.data(), 0x1);
    write_le32(absurd.data() + 4, 0x7FFFFFFF);  // 2 GB claimed
    ScriptedTransport transport({absurd});

    SaharaSession session(transport, quiet_callbacks());
    bool threw = false;
    try {
        session.run(SaharaSession::ImageTable{});
    } catch (const ProtocolError&) {
        threw = true;
    }
    check("a packet claiming 2 GB is refused before allocating", threw);
}

void test_cancellation() {
    std::printf("\n9. cancellation\n");
    ScriptedTransport transport({make_hello(2, 0), make_read_data(13, 0, 4), make_done()});
    SaharaSession::ImageTable images;
    images.emplace(13, std::vector<std::uint8_t>{1, 2, 3, 4});

    int calls = 0;
    SaharaSession::Callbacks callbacks = quiet_callbacks();
    callbacks.cancelled = [&]() { return ++calls > 1; };  // cancel after the first check

    SaharaSession session(transport, callbacks);
    bool threw = false;
    try {
        session.run(images);
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("cancelled") != std::string::npos;
    }
    check("a cancelled handshake stops promptly", threw);
}

void test_execute_device_info() {
    std::printf("\n10. command mode: serial, MSM id and PK hash\n");

    // Serial 0xDEADBEEF, HW id 0x0000_008C_00AB_1234, PK hash of 32 bytes.
    // The layout of the HW id is from qdl: MSM = hw >> 32, OEM = (hw >> 16) & 0xffff,
    // model = hw & 0xffff.
    const std::uint64_t hardware_id = 0x0000008C00AB1234ull;
    std::vector<std::uint8_t> serial_payload(4);
    write_le32(serial_payload.data(), 0xDEADBEEFu);
    std::vector<std::uint8_t> hw_payload(8);
    write_le64(hw_payload.data(), hardware_id);
    std::vector<std::uint8_t> hash_payload(64, 0);
    for (std::size_t index = 0; index < 32; ++index) {
        hash_payload[index] = static_cast<std::uint8_t>(index + 1);
    }

    ScriptedTransport transport({
        make_hello(2, 1),
        make_execute_response(0x01, 4), make_raw(serial_payload),
        make_execute_response(0x02, 8), make_raw(hw_payload),
        make_execute_response(0x03, 64), make_raw(hash_payload),
    });

    SaharaSession::Callbacks callbacks = quiet_callbacks();
    callbacks.log = [](const std::string& level, const std::string& message) {
        if (level == "warn" || level == "error") {
            std::printf("       [%s] %s\n", level.c_str(), message.c_str());
        }
    };
    SaharaSession session(transport, callbacks);
    const SaharaDeviceInfo info = session.query_device_identity(SaharaDeviceInfo{}, false);

    check("serial number is read", info.have_serial && info.serial == 0xDEADBEEFu,
          std::to_string(info.serial));
    check("hardware id is read", info.have_hardware_id);
    check("MSM id is the high half", info.msm_id == 0x8C, std::to_string(info.msm_id));
    check("OEM id is bits 16-31", info.oem_id == 0xAB, std::to_string(info.oem_id));
    check("model id is the low half", info.model_id == 0x1234, std::to_string(info.model_id));
    check("PK hash is read and trimmed to a 32-byte digest",
          info.have_pk_hash && info.pk_hash.size() == 64, info.pk_hash);

    // EXECUTE must be followed by EXECUTE_DATA before any payload arrives.
    // HELLO_RSP, then EXECUTE+EXECUTE_DATA for each of the three commands, then RESET.
    const auto& written = transport.written();
    check("the host sent HELLO_RSP, EXECUTE+EXECUTE_DATA per command, then RESET",
          written.size() == 1 + 3 * 2 + 1, std::to_string(written.size()) + " writes");
    check("HELLO_RSP requested SAHARA_MODE_COMMAND",
          read_le32(written[0].data()) == 0x2 && read_le32(written[0].data() + 20) == 3);
    check("the first EXECUTE asks for the serial number",
          read_le32(written[1].data()) == 0xD && read_le32(written[1].data() + 8) == 0x01);
    check("EXECUTE_DATA repeats the same client command",
          read_le32(written[2].data()) == 0xF && read_le32(written[2].data() + 8) == 0x01);
    check("the last packet is SAHARA_RESET (nothing is being uploaded)",
          read_le32(written[written.size() - 1].data()) == 0x7);
}

void test_pk_hash_trimming() {
    std::printf("\n11. PK hash normalisation\n");

    // A device that repeats a 32-byte digest to fill a 64-byte buffer must come
    // back as 32 bytes, not 64.
    std::vector<std::uint8_t> digest(32);
    for (std::size_t index = 0; index < 32; ++index) {
        digest[index] = static_cast<std::uint8_t>(index + 1);
    }
    std::vector<std::uint8_t> repeated = digest;
    repeated.insert(repeated.end(), digest.begin(), digest.end());
    std::vector<std::uint8_t> padded = repeated;
    padded.resize(96, 0);

    ScriptedTransport transport({
        make_hello(2, 1),
        make_execute_response(0x01, 4), make_raw(std::vector<std::uint8_t>{0, 0, 0, 0}),
        make_execute_response(0x02, 8), make_raw(std::vector<std::uint8_t>(8, 0)),
        make_execute_response(0x03, static_cast<std::uint32_t>(padded.size())), make_raw(padded),
    });

    SaharaSession session(transport, quiet_callbacks());
    const SaharaDeviceInfo info = session.query_device_identity(SaharaDeviceInfo{}, false);
    check("a repeated, zero-padded digest collapses to one 32-byte hash",
          info.pk_hash.size() == 64 && info.pk_hash.substr(0, 8) == "01020304",
          info.pk_hash.substr(0, 16) + "...");
}

// --- Firehose ----------------------------------------------------------------
void test_firehose_builders() {
    std::printf("\n12. Firehose XML builders\n");

    const std::string configure = build_configure_xml(ConfigureRequest{});
    check("configure is wrapped in a prolog and <data>",
          configure.rfind("<?xml version=\"1.0\" ?>", 0) == 0
              && configure.find("<data>") != std::string::npos
              && configure.find("</data>") != std::string::npos,
          configure);
    check("configure carries the payload size", configure.find("MaxPayloadSizeToTargetInBytes=\"1048576\"")
                                                    != std::string::npos);
    check("configure sets ZlpAwareHost", configure.find("ZlpAwareHost=\"1\"") != std::string::npos);
    check("MemoryName is omitted when the caller did not supply one",
          configure.find("MemoryName") == std::string::npos);

    ConfigureRequest named;
    named.memory_name = "eMMC";
    check("MemoryName is emitted when supplied",
          build_configure_xml(named).find("MemoryName=\"eMMC\"") != std::string::npos);

    const std::string power = build_power_xml(PowerRequest{});
    check("power resets with a delay",
          power.find("<power value=\"reset\"") != std::string::npos
              && power.find("DelayInSeconds=\"10\"") != std::string::npos,
          power);

    const std::string ping = build_ping_xml();
    check("ping is a self-closing element", ping.find("<ping/>") != std::string::npos, ping);

    ReadRequest read;
    read.sector_size_in_bytes = 4096;
    read.num_partition_sectors = 2048;
    read.physical_partition_number = 1;
    read.start_sector = 34;
    read.filename = "boot.img";
    const std::string read_xml = build_read_xml(read);
    check("read carries every attribute the programmer expects",
          read_xml.find("SECTOR_SIZE_IN_BYTES=\"4096\"") != std::string::npos
              && read_xml.find("num_partition_sectors=\"2048\"") != std::string::npos
              && read_xml.find("physical_partition_number=\"1\"") != std::string::npos
              && read_xml.find("start_sector=\"34\"") != std::string::npos
              && read_xml.find("filename=\"boot.img\"") != std::string::npos,
          read_xml);

    // A filename with a quote must not be able to break out of the attribute.
    ReadRequest hostile;
    hostile.filename = "a\" onload=\"x";
    const std::string escaped = build_read_xml(hostile);
    check("a quote in a filename is escaped", escaped.find("&quot;") != std::string::npos, escaped);
    check("the injected attribute did not survive",
          escaped.find("onload=\"x\"") == std::string::npos);
}

void test_firehose_parser() {
    std::printf("\n13. Firehose response parsing\n");

    const std::string ack =
        "<?xml version=\"1.0\" ?><data><response value=\"ACK\" /></data>";
    FirehoseResponse response = parse_firehose_response(ack);
    check("ACK is recognised", response.status == FirehoseStatus::Ack, to_string(response.status));
    check("ACK is not raw mode", !response.raw_mode);

    const std::string nak = "<?xml version=\"1.0\" ?><data><response value=\"NAK\" /></data>";
    check("NAK is recognised", parse_firehose_response(nak).status == FirehoseStatus::Nak);

    const std::string rawmode =
        "<?xml version=\"1.0\" ?><data><response value=\"ACK\" rawmode=\"true\" /></data>";
    FirehoseResponse raw = parse_firehose_response(rawmode);
    check("rawmode is detected", raw.status == FirehoseStatus::Ack && raw.raw_mode);

    const std::string logged =
        "<?xml version=\"1.0\" ?><data>"
        "<log value=\"Calling handler for read\"/>"
        "<log value=\"Finished, 2 sectors written\"/>"
        "<response value=\"ACK\" /></data>";
    FirehoseResponse with_logs = parse_firehose_response(logged);
    check("every log line is captured", with_logs.logs.size() == 2, std::to_string(with_logs.logs.size()));
    check("log text is preserved", with_logs.logs[0] == "Calling handler for read",
          with_logs.logs.empty() ? "" : with_logs.logs[0]);
    check("the response is still seen after logs", with_logs.status == FirehoseStatus::Ack);

    // Entities must be decoded, and an escaped quote must not end the attribute.
    const std::string escaped =
        "<?xml version=\"1.0\" ?><data><log value=\"a &quot;quoted&quot; value &amp; more\"/>"
        "<response value=\"ACK\"/></data>";
    FirehoseResponse decoded = parse_firehose_response(escaped);
    check("entities are decoded",
          !decoded.logs.empty() && decoded.logs[0] == "a \"quoted\" value & more",
          decoded.logs.empty() ? "" : decoded.logs[0]);

    // A tag containing the word "value" elsewhere must not confuse the scanner.
    const std::string tricky =
        "<?xml version=\"1.0\" ?><data><response rawmode_value=\"x\" value=\"NAK\"/></data>";
    check("an attribute whose name ends in 'value' is not mistaken for it",
          parse_firehose_response(tricky).status == FirehoseStatus::Nak);

    const std::string only_logs = "<?xml version=\"1.0\" ?><data><log value=\"working\"/></data>";
    check("a document with only logs is not a verdict",
          parse_firehose_response(only_logs).status == FirehoseStatus::Log);

    const std::string unknown_element =
        "<?xml version=\"1.0\" ?><data><somethingNew x=\"1\"/><response value=\"ACK\"/></data>";
    check("an unknown element is ignored rather than fatal",
          parse_firehose_response(unknown_element).status == FirehoseStatus::Ack);

    bool threw = false;
    try {
        parse_firehose_response("this is not xml at all");
    } catch (const ProtocolError&) {
        threw = true;
    }
    check("non-XML input raises", threw);
}

void test_document_framing() {
    std::printf("\n14. response framing\n");

    std::string pending = "<?xml version=\"1.0\" ?><data><response value=\"ACK\"/>";
    check("an incomplete document yields nothing", extract_documents(pending).empty());
    check("the partial document is retained", pending.find("<?xml") == 0, pending);

    pending += "</data>";
    std::vector<std::string> documents = extract_documents(pending);
    check("completing the document yields it", documents.size() == 1, std::to_string(documents.size()));
    check("the framing stops at </data>",
          documents[0].find("</data>") == documents[0].size() - 7);

    // Two responses can arrive in one read; they must not be merged.
    std::string back_to_back =
        "<?xml version=\"1.0\" ?><data><response value=\"ACK\"/></data>"
        "<?xml version=\"1.0\" ?><data><response value=\"NAK\"/></data>";
    std::vector<std::string> both = extract_documents(back_to_back);
    check("two documents in one buffer are split", both.size() == 2, std::to_string(both.size()));
    check("each keeps its own verdict",
          parse_firehose_response(both[0]).status == FirehoseStatus::Ack
              && parse_firehose_response(both[1]).status == FirehoseStatus::Nak);
}

void test_gpt_header() {
    std::printf("\n15. GPT header parsing\n");

    // Built by hand to the UEFI spec: little-endian throughout, 92-byte header
    // at LBA 1, entry array located by the header rather than assumed at LBA 2.
    const std::vector<std::uint8_t> sector = make_gpt_header();

    const GptHeader header = parse_gpt_header(sector.data(), sector.size());
    check("the signature is recognised", header.header_size == 92,
          std::to_string(header.header_size));
    check("the revision renders as 1.0", header.revision_string() == "1.0",
          header.revision_string());
    check("the entry array location comes from the header", header.part_entry_lba == 2,
          std::to_string(header.part_entry_lba));
    check("the usable range is read", header.first_usable_lba == 34 && header.last_usable_lba == 126);
    check("the GUID renders canonically",
          header.disk_guid.to_string() == "deadbeef-1111-2222-aabb-ccddeeff0011",
          header.disk_guid.to_string());

    bool threw = false;
    const std::vector<std::uint8_t> blank(512, 0);
    try {
        parse_gpt_header(blank.data(), blank.size());
    } catch (const ProtocolError&) {
        threw = true;
    }
    check("a sector without the signature is refused", threw);

    threw = false;
    const std::vector<std::uint8_t> truncated(sector.begin(), sector.begin() + 16);
    try {
        parse_gpt_header(truncated.data(), truncated.size());
    } catch (const ProtocolError&) {
        threw = true;
    }
    check("a buffer shorter than the header is refused", threw);

    // An entry size outside the spec's limits means the header cannot be trusted.
    const std::vector<std::uint8_t> broken = make_gpt_header(128, 8);
    threw = false;
    try {
        parse_gpt_header(broken.data(), broken.size());
    } catch (const ProtocolError&) {
        threw = true;
    }
    check("an implausible entry size is refused", threw);
}

void test_gpt_entries() {
    std::printf("\n16. GPT entry parsing\n");

    std::vector<std::uint8_t> array(128 * 3, 0);

    // Slot 0: type GUID AA, name "boot", LBA 34..100.
    write_le32(array.data() + 0, 0xaabbccdd);
    write_u16(array.data() + 4, 0xeeff);
    write_u16(array.data() + 6, 0x0011);
    write_le64(array.data() + 32, 34);
    write_le64(array.data() + 40, 100);
    const std::string name = "boot";
    for (std::size_t i = 0; i < name.size(); ++i) {
        write_u16(array.data() + 56 + i * 2, static_cast<std::uint16_t>(name[i]));
    }
    // Slot 1 is left all-zero: unused, and must be reported rather than dropped.
    // Slot 2 gets a name but no type GUID, which is also unused by definition.
    write_le64(array.data() + 2 * 128 + 32, 200);
    write_le64(array.data() + 2 * 128 + 40, 299);
    write_u16(array.data() + 2 * 128 + 56, 'X');

    const std::vector<GptEntry> entries = parse_gpt_entries(array.data(), array.size(), 3, 128);
    check("every slot is returned, used or not", entries.size() == 3,
          std::to_string(entries.size()));
    check("an entry's name is decoded from UTF-16LE", entries[0].name == "boot", entries[0].name);
    check("the type GUID is not confused with the unique GUID",
          entries[0].type_guid.to_string() == "aabbccdd-eeff-0011-0000-000000000000",
          entries[0].type_guid.to_string());
    check("the sector count is inclusive", entries[0].sector_count() == 67,
          std::to_string(entries[0].sector_count()));
    check("size_bytes scales with the sector size", entries[0].size_bytes(4096) == 67 * 4096,
          std::to_string(entries[0].size_bytes(4096)));
    check("a zero type GUID marks an unused slot", entries[1].is_unused());
    check("a named slot with a zero type GUID is still unused", entries[2].is_unused());
    check("a used slot is not reported as unused", !entries[0].is_unused());

    // parse_gpt takes the entry geometry from the header, so the header has to
    // agree with the array for the combined call to be meaningful.
    const std::vector<std::uint8_t> sector = make_gpt_header(3, 128);
    const GptTable table = parse_gpt(sector.data(), sector.size(), array.data(), array.size());
    check("header and entries combine into a table", table.entries.size() == 3,
          std::to_string(table.entries.size()));
    check("only the used slots are offered separately", table.used_entries().size() == 1,
          std::to_string(table.used_entries().size()));
    check("a partition is found by name", table.find("boot") != nullptr);
    check("an unknown name finds nothing", table.find("nope") == nullptr);
    check("find is case-sensitive, as GPT names are", table.find("BOOT") == nullptr);
    check("the used bytes are summed at the given sector size",
          table.total_bytes(512) == 67 * 512, std::to_string(table.total_bytes(512)));

    bool threw = false;
    try {
        parse_gpt_entries(array.data(), array.size(), 100, 128);
    } catch (const ProtocolError&) {
        threw = true;
    }
    check("a count larger than the buffer is refused", threw);

    threw = false;
    try {
        parse_gpt_entries(array.data(), array.size(), 3, 8);
    } catch (const ProtocolError&) {
        threw = true;
    }
    check("an entry size below the spec's 128 is refused", threw);

    // A header claiming more entries than the array it points at holds: the
    // combined call must refuse rather than read past the buffer.
    const std::vector<std::uint8_t> greedy = make_gpt_header(128, 128);
    threw = false;
    try {
        parse_gpt(greedy.data(), greedy.size(), array.data(), array.size());
    } catch (const ProtocolError&) {
        threw = true;
    }
    check("a header that overstates the array is refused", threw);

    // UTF-16LE decoding stops at the terminator, which is how a 36-unit name
    // field holding 4 used units comes out as a 2-character string.
    const std::uint16_t units[4] = {'A', 'B', 0, 'C'};
    check("utf16le_to_utf8 stops at the terminator", utf16le_to_utf8(units, 4) == "AB",
          utf16le_to_utf8(units, 4));
}

void test_storage_info_extraction() {
    std::printf("\n17. storage geometry in a <getstorageinfo> reply\n");

    // This is the shape the programmer actually sends: a JSON blob escaped into
    // a <log> value, not a dedicated element.
    const std::string reply =
        "<?xml version=\"1.0\" ?><data>"
        "<log value=\"{&quot;storage_info&quot;:{&quot;total_blocks&quot;:61071360,"
        "&quot;block_size&quot;:512,&quot;storage_type&quot;:&quot;UFS&quot;}}\"/>"
        "<response value=\"ACK\"/></data>";
    const FirehoseResponse response = parse_firehose_response(reply);

    StorageInfo info;
    check("the geometry is found inside the escaped JSON", extract_storage_info(response, info));
    check("total_blocks is parsed", info.total_blocks == 61071360u,
          std::to_string(info.total_blocks));
    check("block_size is parsed", info.block_size == 512u, std::to_string(info.block_size));
    check("the storage type is carried through", info.storage_type == "UFS", info.storage_type);
    check("total_bytes multiplies the two", info.total_bytes() == 61071360ull * 512ull,
          std::to_string(info.total_bytes()));
    check("the raw JSON is retained for fields not modelled",
          info.raw_json.find("storage_info") != std::string::npos);

    const FirehoseResponse bare = parse_firehose_response(
        "<?xml version=\"1.0\" ?><data><response value=\"ACK\"/></data>");
    StorageInfo none;
    check("a reply with no geometry reports false rather than inventing one",
          !extract_storage_info(bare, none));
    check("nothing was filled in", none.total_blocks == 0 && none.block_size == 0);

    std::string value;
    check("a scalar member can be read directly", json_scalar(info.raw_json, "block_size", value)
              && value == "512", value);
    check("a quoted member loses its quotes",
          json_scalar("{\"MemoryName\":\"eMMC\"}", "MemoryName", value) && value == "eMMC", value);
    check("a member that is not there reports false",
          !json_scalar(info.raw_json, "nope", value));

    // json_scalar is depth-blind by design - it is a scalar reader, not a
    // parser, and says so. This pins that limitation, because it is the reason
    // extract_storage_info re-scopes into the storage_info object before reading
    // any member: the guarantee below depends on that step, not on this one.
    check("json_scalar is depth-blind, as documented",
          json_scalar("{\"outer\":{\"inner\":7}}", "inner", value));

    // The property that actually matters: a same-named member outside
    // storage_info must not be mistaken for the geometry. A wrong total_blocks
    // here would mis-size every partition the UI offers to write.
    const std::string decoy =
        "<?xml version=\"1.0\" ?><data><log value=\"{&quot;total_blocks&quot;:1,"
        "&quot;block_size&quot;:2,&quot;storage_info&quot;:{&quot;total_blocks&quot;:61071360,"
        "&quot;block_size&quot;:4096}}\"/><response value=\"ACK\"/></data>";
    StorageInfo scoped;
    check("the storage_info object wins over a decoy member outside it",
          extract_storage_info(parse_firehose_response(decoy), scoped)
              && scoped.total_blocks == 61071360u && scoped.block_size == 4096u,
          std::to_string(scoped.total_blocks) + "x" + std::to_string(scoped.block_size));
}

void test_rawprogram_and_patch() {
    std::printf("\n18. rawprogram and patch files\n");

    const std::string rawprogram =
        "<?xml version=\"1.0\" ?>\n<data>\n"
        "  <program SECTOR_SIZE_IN_BYTES=\"4096\" file_sector_offset=\"0\" "
        "filename=\"gpt_main0.bin\" label=\"PrimaryGPT\" num_partition_sectors=\"6\" "
        "physical_partition_number=\"0\" start_sector=\"0\" />\n"
        "  <program SECTOR_SIZE_IN_BYTES=\"4096\" filename=\"boot.img\" label=\"boot\" "
        "num_partition_sectors=\"16384\" physical_partition_number=\"0\" "
        "start_sector=\"48\" />\n"
        "</data>";
    const std::vector<RawProgramEntry> programs = parse_rawprogram_xml(rawprogram);
    check("every <program> element is read", programs.size() == 2,
          std::to_string(programs.size()));
    check("the sector geometry survives",
          programs[0].program.start_sector == 0 && programs[0].program.num_partition_sectors == 6
              && programs[0].program.sector_size_in_bytes == 4096);
    check("the GPT label is kept", programs[1].label == "boot", programs[1].label);
    check("the file name is kept", programs[1].program.filename == "boot.img",
          programs[1].program.filename);
    check("an unknown extra attribute does not stop the parse",
          programs[0].program.start_sector == 0);

    bool threw = false;
    try {
        parse_rawprogram_xml("<?xml version=\"1.0\" ?><data><erase/></data>");
    } catch (const ProtocolError&) {
        threw = true;
    }
    check("a file with no <program> element is refused", threw);

    PatchEntry patch;
    patch.filename = "boot.img";
    patch.value = "HUAXIN-TEST";
    patch.start_sector = 48;
    patch.byte_offset = 8;
    patch.size_in_bytes = 12;
    patch.sector_size_in_bytes = 4096;
    const std::string built = build_patch_xml(patch);
    check("the patch command carries the value",
          built.find("value=\"HUAXIN-TEST\"") != std::string::npos, built);
    check("the patch command carries the offset",
          built.find("byte_offset=\"8\"") != std::string::npos, built);
    check("the patch command is a complete document",
          built.rfind("<?xml") == 0 && built.find("</data>") != std::string::npos);

    const std::vector<PatchEntry> parsed =
        parse_patch_xml("<?xml version=\"1.0\" ?><data>" + built.substr(built.find("<patch"))
                        + "</data>");
    check("a patch round-trips through build and parse", parsed.size() == 1,
          std::to_string(parsed.size()));
    check("the round trip preserves the fields",
          !parsed.empty() && parsed[0].filename == "boot.img" && parsed[0].value == "HUAXIN-TEST"
              && parsed[0].byte_offset == 8 && parsed[0].size_in_bytes == 12 && parsed[0].start_sector == 48);

    // A value containing a quote must not be able to close the attribute early.
    PatchEntry hostile;
    hostile.filename = "boot.img";
    hostile.value = "a\" byte_offset=\"999";
    const std::string escaped = build_patch_xml(hostile);
    check("a quote in a patch value is escaped",
          escaped.find("&quot;") != std::string::npos && escaped.find("byte_offset=\"999\"") == std::string::npos,
          escaped);
}

}  // namespace

int main() {
    std::printf("Qualcomm EDL native protocol tests\n");
    std::printf("==================================\n");

    test_hello_encoding();
    test_little_endian();
    test_decoding_and_validation();
    test_read_data_request();
    test_handshake_upload();
    test_handshake_rejects_unknown_image();
    test_handshake_rejects_out_of_range_read();
    test_handshake_rejects_oversized_packet();
    test_cancellation();
    test_execute_device_info();
    test_pk_hash_trimming();
    test_firehose_builders();
    test_firehose_parser();
    test_document_framing();
    test_gpt_header();
    test_gpt_entries();
    test_storage_info_extraction();
    test_rawprogram_and_patch();

    std::printf("\n%d/%d checks passed\n", g_checks - g_failures, g_checks);
    if (g_failures > 0) {
        std::printf("FAILED\n");
        return 1;
    }
    std::printf("Qualcomm EDL protocols OK.\n");
    return 0;
}
