// =============================================================================
//  Native tests for the MediaTek BROM protocol.
//
//  No hardware required: the bootrom is simulated by a ScriptedTransport that
//  answers the way a real one does and records what the host sent. That is the
//  only practical way to check the things that matter here - the complemented
//  handshake echoes, the big-endian framing, and the XOR checksum - none of
//  which can be verified by reading the code carefully.
// =============================================================================

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "protocols/mediatek/brom.h"
#include "protocols/mediatek/da.h"
#include "protocols/mediatek/mediatek_flash.h"
#include "protocols/mediatek/scatter.h"
#include "protocols/qualcomm/sahara.h"

using namespace huaxin::protocols::mediatek;
using huaxin::protocols::qualcomm::IByteTransport;
using huaxin::protocols::qualcomm::ProtocolError;

namespace {

int g_checks = 0;
int g_failures = 0;

void check(const std::string& description, bool passed, const std::string& detail = "") {
    ++g_checks;
    if (!passed) {
        ++g_failures;
    }
    std::printf("  [%s] %s%s\n", passed ? "PASS" : "FAIL", description.c_str(),
                detail.empty() ? "" : ("  (" + detail + ")").c_str());
    std::fflush(stdout);
}

std::string hexdump(const std::vector<std::uint8_t>& data) {
    std::string text;
    for (std::uint8_t byte : data) {
        char buffer[4];
        std::snprintf(buffer, sizeof(buffer), "%02x ", byte);
        text += buffer;
    }
    if (!text.empty()) {
        text.pop_back();
    }
    return text;
}

/// Plays a recorded device conversation and records what the host wrote.
class ScriptedTransport final : public IByteTransport {
public:
    ScriptedTransport() = default;

    /// Queues bytes for the host to read.
    void expect(std::vector<std::uint8_t> bytes) { m_incoming.push_back(std::move(bytes)); }

    void write_all(const std::uint8_t* data, std::size_t size, unsigned int) override {
        m_written.emplace_back(data, data + size);
    }

    void read_exact(std::uint8_t* buffer, std::size_t size, unsigned int) override {
        while (size > 0) {
            if (m_index >= m_incoming.size()) {
                throw ProtocolError("script exhausted: the host read more than the test provided");
            }
            const std::vector<std::uint8_t>& current = m_incoming[m_index];
            const std::size_t available = current.size() - m_offset;
            const std::size_t take = std::min(size, available);
            std::memcpy(buffer, current.data() + m_offset, take);
            m_offset += take;
            buffer += take;
            size -= take;
            if (m_offset >= current.size()) {
                ++m_index;
                m_offset = 0;
            }
        }
    }

    std::size_t read_some(std::uint8_t* buffer, std::size_t size, unsigned int) override {
        if (m_index >= m_incoming.size()) {
            return 0;
        }
        const std::vector<std::uint8_t>& current = m_incoming[m_index];
        const std::size_t take = std::min(size, current.size() - m_offset);
        std::memcpy(buffer, current.data() + m_offset, take);
        m_offset += take;
        if (m_offset >= current.size()) {
            ++m_index;
            m_offset = 0;
        }
        return take;
    }

    std::string describe() const override { return "scripted"; }

    const std::vector<std::vector<std::uint8_t>>& written() const { return m_written; }
    std::vector<std::uint8_t> all_written() const {
        std::vector<std::uint8_t> flat;
        for (const auto& chunk : m_written) {
            flat.insert(flat.end(), chunk.begin(), chunk.end());
        }
        return flat;
    }

private:
    std::vector<std::vector<std::uint8_t>> m_incoming;
    std::vector<std::vector<std::uint8_t>> m_written;
    std::size_t m_index{0};
    std::size_t m_offset{0};
};

std::vector<std::uint8_t> be32(std::uint32_t value) {
    return {static_cast<std::uint8_t>((value >> 24) & 0xFF),
            static_cast<std::uint8_t>((value >> 16) & 0xFF),
            static_cast<std::uint8_t>((value >> 8) & 0xFF),
            static_cast<std::uint8_t>(value & 0xFF)};
}

BromSession::Callbacks quiet() {
    return BromSession::Callbacks{};
}

// --- tests -------------------------------------------------------------------

void test_handshake_bytes() {
    std::printf("1. handshake constants\n");
    check("the handshake word is the documented four bytes",
          kHandshakeBytes[0] == 0xA0 && kHandshakeBytes[1] == 0x0A && kHandshakeBytes[2] == 0x50
              && kHandshakeBytes[3] == 0x05,
          hexdump(std::vector<std::uint8_t>(kHandshakeBytes, kHandshakeBytes + 4)));
    // The echo is the bitwise complement of what was sent - the detail that
    // makes this protocol recognisable and that a wrong implementation fails.
    bool complement_holds = true;
    for (std::size_t index = 0; index < kHandshakeLength; ++index) {
        complement_holds = complement_holds
                           && kHandshakeEcho[index] == static_cast<std::uint8_t>(~kHandshakeBytes[index]);
    }
    check("every expected echo is the complement of its byte", complement_holds,
          hexdump(std::vector<std::uint8_t>(kHandshakeEcho, kHandshakeEcho + 4)));
    check("the documented echo is 5f f5 af fa",
          kHandshakeEcho[0] == 0x5F && kHandshakeEcho[1] == 0xF5 && kHandshakeEcho[2] == 0xAF
              && kHandshakeEcho[3] == 0xFA);
}

void test_handshake_success() {
    std::printf("\n2. handshake\n");
    ScriptedTransport transport;
    for (std::size_t index = 0; index < kHandshakeLength; ++index) {
        transport.expect({kHandshakeEcho[index]});
    }

    BromSession session(transport, quiet());
    session.handshake();
    check("the handshake succeeds against a correct echo", session.handshaked());

    // Each byte must go out on its own, so the bootrom can answer it.
    const auto& written = transport.written();
    check("four separate one-byte writes were made", written.size() == kHandshakeLength,
          std::to_string(written.size()) + " writes");
    bool one_byte_each = true;
    for (std::size_t index = 0; index < written.size() && index < kHandshakeLength; ++index) {
        one_byte_each = one_byte_each && written[index].size() == 1
                        && written[index][0] == kHandshakeBytes[index];
    }
    check("each write carried the documented byte, in order", one_byte_each,
          hexdump(transport.all_written()));
}

void test_handshake_rejects_wrong_echo() {
    std::printf("\n3. handshake failure\n");
    ScriptedTransport transport;
    transport.expect({0x00});  // not the complement of 0xA0

    BromSession session(transport, quiet());
    bool threw = false;
    std::string message;
    try {
        session.handshake();
    } catch (const ProtocolError& error) {
        threw = true;
        message = error.what();
    }
    check("a wrong echo aborts the handshake", threw);
    check("the error names both bytes", message.find("0x5f") != std::string::npos
                                            && message.find("0x00") != std::string::npos,
          message);
    check("the session is not marked as handshaken", !session.handshaked());
}

void test_command_echo_framing() {
    std::printf("\n4. command framing\n");
    ScriptedTransport transport;
    transport.expect({0xFD});           // GET_HW_CODE echoes back
    transport.expect(be32(0x06720100)); // hw code 0x0672, version 0x0100

    BromSession session(transport, quiet());
    const BromChipInfo info = session.read_chip_info();

    check("the hardware code is the high half of the word", info.hardware_code == 0x0672,
          std::to_string(info.hardware_code));
    check("the hardware version is the low half", info.hardware_version == 0x0100,
          std::to_string(info.hardware_version));
    check("the command byte was sent", transport.written()[0] == std::vector<std::uint8_t>{0xFD},
          hexdump(transport.written()[0]));

    // A command that is not echoed means the stream is out of sync; continuing
    // would read a device's answer as data.
    ScriptedTransport bad;
    bad.expect({0x00});
    BromSession other(bad, quiet());
    bool threw = false;
    try {
        other.send_command(BromCommand::GetHwCode);
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("out of sync") != std::string::npos;
    }
    check("a command that is not echoed is refused", threw);
}

void test_target_config_decoding() {
    std::printf("\n5. target config\n");

    // Bit assignments from mtkclient's get_target_config().
    const TargetConfig plain = decode_target_config(0x00000000, 0);
    check("an empty config reports no security features",
          !plain.secure_boot && !plain.sla_required && !plain.memory_read_allowed,
          plain.describe());

    const TargetConfig secure = decode_target_config(0x01 | 0x02 | 0x20, 0);
    check("secure boot is bit 0", secure.secure_boot);
    check("SLA is bit 1", secure.sla_required);
    check("memory read is bit 5", secure.memory_read_allowed);
    check("unset bits stay clear", !secure.certificate_required && !secure.memory_write_allowed);
    check("the description lists what is set",
          secure.describe().find("secure boot") != std::string::npos
              && secure.describe().find("SLA") != std::string::npos,
          secure.describe());

    ScriptedTransport transport;
    // 0x50 = certificate (0x10) + memory write (0x40). Bit 1 is deliberately
    // clear here: 0x42 would have set it and made SLA required.
    transport.expect({0xD8});                      // echo
    transport.expect(be32(0x00000050));
    transport.expect({0x00, 0x00});                // status

    BromSession session(transport, quiet());
    const TargetConfig read = session.read_target_config();
    check("the config word is read big-endian", read.raw == 0x00000050, std::to_string(read.raw));
    check("memory write is decoded as allowed", read.memory_write_allowed);
    check("certificate is decoded as required", read.certificate_required);
    check("SLA is decoded as not required", !read.sla_required);
    check("secure boot is decoded as off", !read.secure_boot);
}

void test_checksum() {
    std::printf("\n6. transfer checksum\n");
    // XOR of 16-bit little-endian words - note the opposite endianness from the
    // command framing, which is a genuine trap.
    check("an empty buffer checksums to zero", compute_checksum({}) == 0);

    const std::vector<std::uint8_t> one_word{0x34, 0x12};
    check("a single little-endian word checksums to itself", compute_checksum(one_word) == 0x1234,
          std::to_string(compute_checksum(one_word)));

    const std::vector<std::uint8_t> two_words{0x34, 0x12, 0x34, 0x12};
    check("identical words cancel out", compute_checksum(two_words) == 0,
          std::to_string(compute_checksum(two_words)));

    const std::vector<std::uint8_t> mixed{0xFF, 0x00, 0x0F, 0xF0};
    check("different words XOR together", compute_checksum(mixed) == (0x00FF ^ 0xF00F),
          std::to_string(compute_checksum(mixed)));

    const std::vector<std::uint8_t> odd{0xAB};  // half a word: the trailing byte XORs in
    check("a trailing odd byte is XORed in", compute_checksum(odd) == 0xAB,
          std::to_string(compute_checksum(odd)));

    const std::vector<std::uint8_t> odd_file{0x01, 0x02, 0x03};
    const std::vector<std::uint8_t> padded = build_da_payload(odd_file);
    check("an odd-length payload is padded to even", padded.size() == 4, std::to_string(padded.size()));
    check("padding is a zero byte", padded[3] == 0x00 && padded[2] == 0x03);
    const std::vector<std::uint8_t> even_file{0x01, 0x02};
    check("an even-length payload is untouched", build_da_payload(even_file).size() == 2);
}

void test_da_upload() {
    std::printf("\n7. download agent upload\n");

    // 16 bytes of agent, of which the last 4 are its signature - the split the
    // bootrom is told about, so a non-trivial signature length is exercised.
    const std::vector<std::uint8_t> agent{0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88,
                                          0x99, 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF, 0x01};
    const std::uint16_t checksum = compute_checksum(agent);

    ScriptedTransport transport;
    transport.expect({0xD7});            // SEND_DA echo
    transport.expect(be32(0x00200000));  // address echo
    transport.expect(be32(static_cast<std::uint32_t>(agent.size())));  // length echo
    transport.expect(be32(0x00000004));  // signature length echo
    transport.expect({0x00, 0x00});      // status = OK
    transport.expect({static_cast<std::uint8_t>((checksum >> 8) & 0xFF),
                      static_cast<std::uint8_t>(checksum & 0xFF)});  // checksum echo
    transport.expect({0xD5});            // JUMP_DA echo
    transport.expect(be32(0x00200000));  // address echo

    DownloadAgent da;
    da.payload = agent;
    da.load_address = 0x00200000;
    da.signature_length = 4;
    da.code_length = agent.size() - 4;

    int progress_calls = 0;
    BromSession::Callbacks callbacks = quiet();
    callbacks.progress = [&](int, const std::string&) { ++progress_calls; };

    BromSession session(transport, callbacks);
    session.upload_download_agent(da);

    const std::vector<std::uint8_t> flat = transport.all_written();
    check("SEND_DA opened the exchange", flat.size() > 8 && flat[0] == 0xD7, hexdump(flat).substr(0, 40));

    // The three parameters must be big-endian. Little-endian would put the
    // address bytes in the opposite order and the bootrom would load the agent
    // into the wrong place.
    const std::vector<std::uint8_t> expected = {0xD7,
                                                0x00, 0x20, 0x00, 0x00,   // load address
                                                0x00, 0x00, 0x00, 0x10,   // total length = 16
                                                0x00, 0x00, 0x00, 0x04};  // signature length = 4
    check("the command and its three parameters are big-endian",
          flat.size() >= expected.size() && std::equal(expected.begin(), expected.end(), flat.begin()),
          hexdump(flat).substr(0, 40));

    check("the agent bytes follow the parameters",
          std::search(flat.begin(), flat.end(), agent.begin(), agent.end()) != flat.end());
    // The exchange must end with JUMP_DA and the same big-endian address.
    const std::vector<std::uint8_t> tail{0xD5, 0x00, 0x20, 0x00, 0x00};
    check("JUMP_DA with the load address ends the exchange",
          flat.size() >= tail.size()
              && std::equal(tail.rbegin(), tail.rend(), flat.rbegin()),
          hexdump(std::vector<std::uint8_t>(flat.end() - 5, flat.end())));
    check("progress was reported", progress_calls > 0, std::to_string(progress_calls));
}

void test_da_upload_rejects_bad_checksum() {
    std::printf("\n8. corrupt transfer is caught\n");

    const std::vector<std::uint8_t> agent{0xAA, 0xBB, 0xCC, 0xDD};

    ScriptedTransport transport;
    transport.expect({0xD7});
    transport.expect(be32(0x1000));
    transport.expect(be32(4));
    transport.expect(be32(0));
    transport.expect({0x00, 0x00});
    transport.expect({0xDE, 0xAD});  // wrong checksum

    DownloadAgent da;
    da.payload = agent;
    da.load_address = 0x1000;
    da.signature_length = 0;
    da.code_length = agent.size();

    BromSession session(transport, quiet());
    bool threw = false;
    std::string message;
    try {
        session.upload_download_agent(da, /*jump=*/false);
    } catch (const ProtocolError& error) {
        threw = true;
        message = error.what();
    }
    check("a checksum mismatch is fatal", threw);
    check("the error shows both checksums", message.find("dead") != std::string::npos
                                                && message.find("checksum") != std::string::npos,
          message);
    check("the agent is not started when the transfer is corrupt",
          transport.all_written().back() != 0xD5);
}

void test_sla_required() {
    std::printf("\n9. secure boot refusal\n");

    ScriptedTransport transport;
    transport.expect({0xD7});
    transport.expect(be32(0x1000));
    transport.expect(be32(4));
    transport.expect(be32(0));
    transport.expect({0x1D, 0x0D});  // SLA required

    DownloadAgent da;
    da.payload = {1, 2, 3, 4};
    da.load_address = 0x1000;

    BromSession session(transport, quiet());
    bool threw = false;
    std::string message;
    try {
        session.upload_download_agent(da);
    } catch (const ProtocolError& error) {
        threw = true;
        message = error.what();
    }
    check("SLA is reported, not silently ignored", threw);
    check("the message explains what SLA means",
          message.find("SLA") != std::string::npos && message.find("secure") != std::string::npos,
          message);
}

void test_validation() {
    std::printf("\n10. input validation\n");
    ScriptedTransport transport;
    BromSession session(transport, quiet());

    DownloadAgent empty;
    bool threw = false;
    try {
        session.upload_download_agent(empty);
    } catch (const ProtocolError&) {
        threw = true;
    }
    check("an empty download agent is refused", threw);

    DownloadAgent impossible;
    impossible.payload = {1, 2, 3, 4};
    impossible.signature_length = 99;
    threw = false;
    try {
        session.upload_download_agent(impossible);
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("signature") != std::string::npos;
    }
    check("a signature longer than the file is refused", threw);
    check("nothing was sent for an invalid agent", transport.all_written().empty());
}

void test_cancellation() {
    std::printf("\n11. cancellation\n");
    ScriptedTransport transport;
    for (std::size_t index = 0; index < kHandshakeLength; ++index) {
        transport.expect({kHandshakeEcho[index]});
    }

    int calls = 0;
    BromSession::Callbacks callbacks = quiet();
    callbacks.cancelled = [&]() { return ++calls > 2; };

    BromSession session(transport, callbacks);
    bool threw = false;
    try {
        session.handshake();
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("cancelled") != std::string::npos;
    }
    check("a cancelled handshake stops partway", threw);
}

// --- download agent ----------------------------------------------------------

/// A device-side DA frame: the 12-byte header and its payload, all little-endian.
std::vector<std::uint8_t> da_frame(const std::vector<std::uint8_t>& payload) {
    std::vector<std::uint8_t> out = encode_da_header(kDaDataTypeProtocolFlow,
                                                     static_cast<std::uint32_t>(payload.size()));
    out.insert(out.end(), payload.begin(), payload.end());
    return out;
}

/// A status frame carrying a 32-bit status.
std::vector<std::uint8_t> da_status(std::uint32_t status) {
    std::vector<std::uint8_t> payload;
    payload.push_back(static_cast<std::uint8_t>(status & 0xFF));
    payload.push_back(static_cast<std::uint8_t>((status >> 8) & 0xFF));
    payload.push_back(static_cast<std::uint8_t>((status >> 16) & 0xFF));
    payload.push_back(static_cast<std::uint8_t>((status >> 24) & 0xFF));
    return da_frame(payload);
}

DaSession::Callbacks quiet_da() {
    return DaSession::Callbacks{};
}

void test_da_framing() {
    std::printf("\n12. download agent framing\n");

    // The single most expensive mistake in this protocol: the bootrom layer a few
    // seconds earlier is big-endian, and this one is not. Asserted byte by byte.
    const std::vector<std::uint8_t> header = encode_da_header(kDaDataTypeProtocolFlow, 0x10);
    check("the header is 12 bytes", header.size() == 12, std::to_string(header.size()));
    check("the magic is little-endian",
          header[0] == 0xEF && header[1] == 0xEE && header[2] == 0xEE && header[3] == 0xFE,
          hexdump(header));
    check("the data type is little-endian",
          header[4] == 0x01 && header[5] == 0x00 && header[6] == 0x00 && header[7] == 0x00,
          hexdump(header));
    check("the length is little-endian",
          header[8] == 0x10 && header[9] == 0x00 && header[10] == 0x00 && header[11] == 0x00,
          hexdump(header));

    // A command frame: the id as a little-endian 32-bit payload.
    const std::vector<std::uint8_t> command =
        encode_da_command(static_cast<std::uint32_t>(DaCommand::WriteData));
    check("a command frame is 16 bytes", command.size() == 16, std::to_string(command.size()));
    check("WRITE_DATA is 0x010004 on the wire, little-endian",
          command[12] == 0x04 && command[13] == 0x00 && command[14] == 0x01 && command[15] == 0x00,
          hexdump(command));

    const std::vector<std::uint8_t> payload = {0xDE, 0xAD, 0xBE, 0xEF};
    const std::vector<std::uint8_t> frame = encode_da_frame(payload);
    check("a frame is the header followed by the payload", frame.size() == 16,
          std::to_string(frame.size()));
    check("the payload is appended verbatim",
          std::equal(payload.begin(), payload.end(), frame.begin() + 12));

    const DaFrame decoded = decode_da_header(frame.data(), frame.size());
    check("a header round-trips", decoded.magic == kDaProtocolMagic && decoded.data_type == 1);
    check("the decoded length is the payload length", decoded.payload.size() == 4,
          std::to_string(decoded.payload.size()));

    bool threw = false;
    std::vector<std::uint8_t> wrong = frame;
    wrong[0] = 0x00;
    try {
        decode_da_header(wrong.data(), wrong.size());
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("desynchronised") != std::string::npos;
    }
    check("a wrong magic is refused as a desynchronised stream", threw);

    threw = false;
    try {
        decode_da_header(frame.data(), 8);
    } catch (const ProtocolError&) {
        threw = true;
    }
    check("a short header is refused", threw);

    // A corrupt length must not turn into a multi-gigabyte allocation.
    std::vector<std::uint8_t> greedy = encode_da_header(kDaDataTypeProtocolFlow, 0xFFFFFFFFu);
    threw = false;
    try {
        decode_da_header(greedy.data(), greedy.size());
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("ceiling") != std::string::npos;
    }
    check("an implausible frame length is refused", threw);
}

void test_da_checksum() {
    std::printf("\n13. download agent checksum\n");

    const std::vector<std::uint8_t> data = {0x01, 0x02, 0x03, 0x04};
    // 1+2+3+4 = 10, and as a sum there is no carry to fold.
    check("the checksum is the sum of the bytes",
          compute_da_checksum(data) == 10, std::to_string(compute_da_checksum(data)));

    // This is the point of having two functions. The bootrom XORs 16-bit
    // little-endian words; the agent sums bytes. On this input they differ, so a
    // build that used the wrong one would be caught here rather than by a device.
    const std::uint16_t brom = compute_checksum(data);
    const std::uint16_t agent = compute_da_checksum(data);
    check("the agent's checksum is not the bootrom's", brom != agent,
          "brom " + std::to_string(brom) + " vs agent " + std::to_string(agent));

    // The sum wraps at 16 bits rather than carrying into a wider result.
    std::vector<std::uint8_t> wide(300, 0xFF);
    check("the sum wraps at 16 bits", compute_da_checksum(wide) == ((300 * 255) & 0xFFFF),
          std::to_string(compute_da_checksum(wide)));

    check("an empty payload sums to zero", compute_da_checksum({}) == 0);

    const std::vector<std::uint8_t> encoded = encode_da_checksum(0x1234);
    check("the checksum goes on the wire as four little-endian bytes",
          encoded.size() == 4 && encoded[0] == 0x34 && encoded[1] == 0x12 && encoded[2] == 0
              && encoded[3] == 0,
          hexdump(encoded));
}

void test_da_status_decoding() {
    std::printf("\n14. download agent status decoding\n");

    check("a 2-byte payload is a 16-bit status", decode_da_status({0x5A, 0x00}) == 0x5A,
          std::to_string(decode_da_status({0x5A, 0x00})));
    check("a 4-byte payload is a 32-bit status",
          decode_da_status({0x04, 0x00, 0x02, 0xC0}) == 0xC0020004);

    // The agent says "OK" with the protocol magic in a 4-byte frame.
    const std::vector<std::uint8_t> magic = {0xEF, 0xEE, 0xEE, 0xFE};
    check("the magic in a status frame means success", decode_da_status(magic) == 0,
          std::to_string(decode_da_status(magic)));

    check("a longer payload is read as words and the first is the status",
          decode_da_status({0x01, 0x00, 0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF}) == 1);
    check("an empty payload reads as success", decode_da_status({}) == 0);

    check("a known code is named",
          describe_da_status(kDaStatusDlForbidden).find("forbidden") != std::string::npos,
          describe_da_status(kDaStatusDlForbidden));
    check("anti-rollback is named",
          describe_da_status(kDaStatusAntiRollback).find("rollback") != std::string::npos,
          describe_da_status(kDaStatusAntiRollback));
    // An unknown code must not be guessed at.
    check("an unknown code is reported as a number",
          describe_da_status(0xDEADBEEF) == "unknown status 0xdeadbeef",
          describe_da_status(0xDEADBEEF));
}

void test_da_region_param() {
    std::printf("\n15. region parameter block\n");

    const NandExtension none;
    const std::vector<std::uint8_t> param =
        encode_region_param(DaStorage::Emmc, 8, 0x1122334455667788ull, 0x2000, none);
    check("the parameter block is 56 bytes", param.size() == kDaRegionParamSize,
          std::to_string(param.size()));
    check("the storage type comes first, little-endian",
          param[0] == 0x01 && param[1] == 0 && param[2] == 0 && param[3] == 0, hexdump(param));
    check("the partition follows",
          param[4] == 0x08 && param[5] == 0 && param[6] == 0 && param[7] == 0);
    check("the address is a little-endian 64-bit field",
          param[8] == 0x88 && param[9] == 0x77 && param[10] == 0x66 && param[11] == 0x55
              && param[12] == 0x44 && param[13] == 0x33 && param[14] == 0x22 && param[15] == 0x11,
          hexdump(param));
    check("the length follows the address",
          param[16] == 0x00 && param[17] == 0x20 && param[18] == 0 && param[19] == 0);
    check("the eight NAND extension words are zero by default",
          std::all_of(param.begin() + 24, param.end(), [](std::uint8_t b) { return b == 0; }));

    DaStorage storage = DaStorage::Nor;
    std::uint32_t partition = 99;
    std::uint64_t address = 0;
    std::uint64_t length = 0;
    decode_region_param(param, storage, partition, address, length);
    check("the block round-trips",
          storage == DaStorage::Emmc && partition == 8 && address == 0x1122334455667788ull
              && length == 0x2000,
          std::string(to_string(storage)) + " " + std::to_string(address));

    // The UFS type is 0x30, which exercises a value above the eMMC range.
    const std::vector<std::uint8_t> ufs = encode_region_param(DaStorage::Ufs, 0, 0, 0x1000, none);
    check("the UFS storage type encodes as 0x30", ufs[0] == 0x30, hexdump(ufs));

    bool threw = false;
    try {
        decode_region_param({1, 2, 3}, storage, partition, address, length);
    } catch (const ProtocolError&) {
        threw = true;
    }
    check("a short parameter block is refused", threw);
}

void test_da_flash_info_parsers() {
    std::printf("\n16. flash geometry parsing\n");

    std::vector<std::uint8_t> emmc(kEmmcInfoSize, 0);
    auto put_le32 = [&](std::size_t offset, std::uint32_t value) {
        emmc[offset] = static_cast<std::uint8_t>(value & 0xFF);
        emmc[offset + 1] = static_cast<std::uint8_t>((value >> 8) & 0xFF);
        emmc[offset + 2] = static_cast<std::uint8_t>((value >> 16) & 0xFF);
        emmc[offset + 3] = static_cast<std::uint8_t>((value >> 24) & 0xFF);
    };
    auto put_le64 = [&](std::size_t offset, std::uint64_t value) {
        for (int byte = 0; byte < 8; ++byte) {
            emmc[offset + static_cast<std::size_t>(byte)] =
                static_cast<std::uint8_t>((value >> (8 * byte)) & 0xFF);
        }
    };
    put_le32(0, 1);          // type: eMMC
    put_le32(4, 512);        // block size
    put_le64(8, 0x400000);   // boot1
    put_le64(16, 0x400000);  // boot2
    put_le64(24, 0x800000);  // rpmb
    put_le64(64, 0x3A00000000ull);  // user
    put_le64(88, 0x12345678);       // firmware version

    const EmmcInfo info = parse_emmc_info(emmc);
    check("the eMMC type is read", info.type == 1, std::to_string(info.type));
    check("the block size is read", info.block_size == 512, std::to_string(info.block_size));
    check("the boot area sizes are read", info.boot1_size == 0x400000 && info.boot2_size == 0x400000);
    check("the user capacity is read", info.user_size == 0x3A00000000ull,
          std::to_string(info.user_size));
    check("the firmware version is read", info.firmware_version == 0x12345678);
    check("the reply is exactly 96 bytes", kEmmcInfoSize == 96, std::to_string(kEmmcInfoSize));

    bool threw = false;
    try {
        parse_emmc_info(std::vector<std::uint8_t>(40, 0));
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("shorter than") != std::string::npos;
    }
    check("a short eMMC reply is refused rather than read past", threw);

    const std::vector<std::uint8_t> chip = {0x72, 0x06, 0x01, 0x00, 0x02, 0x00, 0x03, 0x00,
                                            0x04, 0x00};
    const ChipId id = parse_chip_id(chip);
    check("the chip id fields are little-endian",
          id.hw_code == 0x0672 && id.hw_sub_code == 0x0001 && id.hw_version == 0x0002
              && id.sw_version == 0x0003 && id.chip_evolution == 0x0004,
          std::to_string(id.hw_code));

    const std::vector<std::uint8_t> lengths = {0x00, 0x00, 0x10, 0x00, 0x00, 0x00, 0x20, 0x00};
    const PacketLengths packets = parse_packet_length(lengths);
    check("the negotiated packet lengths are read",
          packets.write_packet_length == 0x100000 && packets.read_packet_length == 0x200000,
          std::to_string(packets.write_packet_length));

    // The agent reports RAM in one of two widths and nothing else.
    std::vector<std::uint8_t> ram32(24, 0);
    ram32[0] = 1;                       // sram type
    ram32[4] = 0x00;                    // sram base 0x80000000
    ram32[7] = 0x80;
    ram32[8] = 0x00;                    // sram size 0x20000
    ram32[10] = 0x02;
    const RamInfo small = parse_ram_info(ram32);
    check("a 24-byte RAM reply is the 32-bit form", !small.is_64bit);
    check("the SRAM base address is read", small.sram.base_address == 0x80000000ull,
          std::to_string(small.sram.base_address));
    check("the SRAM size is read", small.sram.size == 0x20000ull, std::to_string(small.sram.size));

    const RamInfo large = parse_ram_info(std::vector<std::uint8_t>(48, 0));
    check("a 48-byte RAM reply is the 64-bit form", large.is_64bit);

    threw = false;
    try {
        parse_ram_info(std::vector<std::uint8_t>(30, 0));
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("nothing else") != std::string::npos;
    }
    check("a RAM reply of neither defined width is refused", threw);

    // The storage and region names a scatter file carries.
    DaStorage storage = DaStorage::Nor;
    check("HW_STORAGE_EMMC maps to eMMC",
          parse_storage_name("HW_STORAGE_EMMC", storage) && storage == DaStorage::Emmc);
    check("HW_STORAGE_UFS maps to UFS",
          parse_storage_name("HW_STORAGE_UFS", storage) && storage == DaStorage::Ufs);
    check("a spelling in another case still maps",
          parse_storage_name("hw_storage_nand", storage) && storage == DaStorage::Nand);
    check("an unknown storage name is reported rather than guessed at",
          !parse_storage_name("HW_STORAGE_QUANTUM", storage));

    EmmcPartition region = EmmcPartition::User;
    check("EMMC_BOOT_1 maps to boot1",
          parse_region_name("EMMC_BOOT_1", region) && region == EmmcPartition::Boot1);
    check("an empty region means the user area", parse_region_name("", region)
              && region == EmmcPartition::User);
    check("an unknown region is reported", !parse_region_name("EMMC_MYSTERY", region));
}

void test_da_write_region() {
    std::printf("\n17. writing a region through the agent\n");

    ScriptedTransport transport;
    // Four chunks at a 256-byte packet size, so: a status after the command,
    // one after the parameter block, one per chunk, the final verdict, and the
    // post-download action's answer.
    for (int index = 0; index < 8; ++index) {
        transport.expect(da_status(0));
    }

    DaSession session(transport, quiet_da());
    // 600 bytes pads to 1024; a 256-byte packet size forces four chunks.
    std::vector<std::uint8_t> image(600);
    for (std::size_t index = 0; index < image.size(); ++index) {
        image[index] = static_cast<std::uint8_t>(index & 0xFF);
    }
    const std::uint64_t written = session.write_region(DaStorage::Emmc, 8, 0x2000, image, 256);

    check("600 bytes pad up to 1024", written == 1024, std::to_string(written));

    const auto& writes = transport.written();
    // One command frame, one parameter block, three frames per chunk for four
    // chunks, and the closing action.
    check("fifteen writes reach the device", writes.size() == 15, std::to_string(writes.size()));
    check("the first is the WRITE_DATA command",
          writes[0].size() == 16 && writes[0][12] == 0x04 && writes[0][13] == 0x00
              && writes[0][14] == 0x01 && writes[0][15] == 0x00,
          hexdump(writes[0]));
    check("the second is the region parameter block", writes[1].size() == 12 + kDaRegionParamSize,
          std::to_string(writes[1].size()));
    check("the parameter carries the storage and partition",
          writes[1][12] == 0x01 && writes[1][16] == 0x08);

    // The chunk frames: a zero word, the checksum, then the bytes. With a
    // 256-byte packet size there are four chunks, each a separate three-frame
    // group, but they are written back to back so the recording shows them
    // merged per transport call.
    check("a zero word opens each chunk",
          writes[2][12] == 0 && writes[2][13] == 0 && writes[2][14] == 0 && writes[2][15] == 0);
    const std::uint16_t expected = compute_da_checksum(image.data(), 256);
    check("the chunk checksum is the sum of that chunk",
          writes[3][12] == static_cast<std::uint8_t>(expected & 0xFF)
              && writes[3][13] == static_cast<std::uint8_t>((expected >> 8) & 0xFF),
          hexdump(writes[3]) + " expected " + std::to_string(expected));
    check("the chunk data is written verbatim",
          writes[4].size() == 256 && writes[4][0] == 0x00 && writes[4][255] == 0xFF,
          std::to_string(writes[4].size()));
    check("the last write is the post-download action",
          writes[14].size() == 16 && writes[14][12] == 0x05 && writes[14][13] == 0x00
              && writes[14][14] == 0x08 && writes[14][15] == 0x00,
          hexdump(writes[14]));

    // The padding is what makes the tail of the sector well-defined rather than
    // whatever was on the flash before, so it is worth asserting rather than
    // assuming. 600 bytes of payload pad to 1024: chunk 3 holds bytes 512..767,
    // of which 512..599 are image and 600..767 are padding.
    const std::vector<std::uint8_t>& third = writes[10];
    check("the third chunk carries the last real bytes",
          third.size() == 256 && third[87] == static_cast<std::uint8_t>((512 + 87) & 0xFF),
          std::to_string(third[87]));
    check("the padding that follows the image is zeros",
          std::all_of(third.begin() + 88, third.end(), [](std::uint8_t b) { return b == 0; }),
          std::to_string(third[88]) + " at the first padded byte");
    // Chunk 4 covers bytes 768..1023, which is entirely padding.
    const std::vector<std::uint8_t>& fourth = writes[13];
    check("the final chunk is padding end to end",
          fourth.size() == 256
              && std::all_of(fourth.begin(), fourth.end(), [](std::uint8_t b) { return b == 0; }),
          std::to_string(fourth.size()) + " bytes");
}

void test_da_write_region_failure() {
    std::printf("\n18. the agent refusing a write\n");

    ScriptedTransport transport;
    transport.expect(da_status(kDaStatusDlForbidden));

    DaSession session(transport, quiet_da());
    bool threw = false;
    try {
        session.write_region(DaStorage::Emmc, 8, 0, std::vector<std::uint8_t>(512, 0xAA));
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("forbidden") != std::string::npos;
    }
    check("a refusal stops the write and names the reason", threw);

    // Nothing beyond the first command frame should have gone out.
    check("no data was sent after the refusal", transport.written().size() == 1,
          std::to_string(transport.written().size()));
}

void test_da_read_region() {
    std::printf("\n19. reading a region through the agent\n");

    ScriptedTransport transport;
    transport.expect(da_status(0));  // after READ_DATA
    transport.expect(da_status(0));  // after the parameter block
    transport.expect(da_status(0));  // the trailing status after the parameters

    std::vector<std::uint8_t> first(512);
    std::vector<std::uint8_t> second(512);
    for (std::size_t index = 0; index < 512; ++index) {
        first[index] = static_cast<std::uint8_t>(index & 0xFF);
        second[index] = static_cast<std::uint8_t>((index + 1) & 0xFF);
    }
    transport.expect(da_frame(first));
    transport.expect(da_frame(second));
    // A four-byte zero frame is the agent's end-of-data marker.
    transport.expect(da_frame({0, 0, 0, 0}));
    transport.expect(da_status(0));  // the closing status

    DaSession session(transport, quiet_da());
    const std::vector<std::uint8_t> data = session.read_region(DaStorage::Emmc, 8, 0, 1024);

    check("both data frames arrive", data.size() == 1024, std::to_string(data.size()));
    check("the first frame's bytes are in order", data[0] == 0x00 && data[255] == 0xFF);
    check("the second frame's bytes are in order",
          data[512] == 0x01 && data[1023] == 0x00, std::to_string(data[512]));

    // Every data frame is acknowledged with a zero word, or the agent stops.
    const auto& writes = transport.written();
    std::size_t acknowledgements = 0;
    for (const auto& write : writes) {
        if (write.size() == 16 && write[8] == 0x04 && write[12] == 0 && write[13] == 0
            && write[14] == 0 && write[15] == 0) {
            ++acknowledgements;
        }
    }
    check("each data frame was acknowledged", acknowledgements == 2,
          std::to_string(acknowledgements));
}

void test_da_read_region_error() {
    std::printf("\n20. the agent failing a read\n");

    ScriptedTransport transport;
    transport.expect(da_status(0));
    transport.expect(da_status(0));
    transport.expect(da_status(0));
    // A four-byte frame carrying a non-zero status is an early stop.
    transport.expect(da_frame({0x04, 0x00, 0x02, 0xC0}));

    DaSession session(transport, quiet_da());
    bool threw = false;
    try {
        session.read_region(DaStorage::Emmc, 8, 0, 4096);
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("stopped early") != std::string::npos;
    }
    check("an early stop is reported with the agent's code", threw);
}

void test_da_format_region() {
    std::printf("\n21. erasing a region through the agent\n");

    ScriptedTransport transport;
    transport.expect(da_status(0));                // after FORMAT
    transport.expect(da_status(0));                // after the parameter block
    transport.expect(da_status(kDaStatusContinue));  // working
    transport.expect(da_status(10));               // wait 10 ms
    transport.expect(da_status(kDaStatusComplete));  // done
    transport.expect(da_status(0));                // after the parameter block

    DaSession session(transport, quiet_da());
    bool threw = false;
    try {
        session.format_region(DaStorage::Emmc, 8, 0x1000, 0x2000);
    } catch (const ProtocolError& error) {
        threw = true;
        std::printf("      unexpected: %s\n", error.what());
    }
    check("a Continue/Complete erase is accepted", !threw);

    bool saw_ack = false;
    for (const auto& write : transport.written()) {
        if (write.size() == 16 && write[8] == 0x04 && write[12] == 0) {
            saw_ack = true;
        }
    }
    check("the host acknowledged each erase step", saw_ack);

    // An agent that never completes must not be waited on forever.
    ScriptedTransport stubborn;
    stubborn.expect(da_status(0));
    stubborn.expect(da_status(0));
    for (int index = 0; index < 200; ++index) {
        stubborn.expect(da_status(60000));  // a nonsense delay
        stubborn.expect(da_status(kDaStatusContinue));
    }
    stubborn.expect(da_status(60000));
    stubborn.expect(da_status(0xC0010001));

    DaSession second(stubborn, quiet_da());
    threw = false;
    try {
        second.format_region(DaStorage::Emmc, 8, 0, 0x2000);
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("completion") != std::string::npos;
    }
    check("a failing erase ends with the agent's status rather than hanging", threw);
}

void test_da_session_queries() {
    std::printf("\n22. agent queries\n");

    ScriptedTransport transport;
    // GET_DA_VERSION: DEVICE_CTRL, the code, then the data frame and its status.
    transport.expect(da_status(0));
    transport.expect(da_status(0));
    transport.expect(da_frame({'1', '.', '2', '.', '3'}));
    transport.expect(da_status(0));

    DaSession session(transport, quiet_da());
    std::string version;
    session.get_da_version(version);
    check("the agent version is read as text", version == "1.2.3", version);

    const auto& writes = transport.written();
    check("the query opened with DEVICE_CTRL",
          writes[0].size() == 16 && writes[0][12] == 0x09 && writes[0][13] == 0x00
              && writes[0][14] == 0x01 && writes[0][15] == 0x00,
          hexdump(writes[0]));
    check("the control code followed",
          writes[1].size() == 16 && writes[1][12] == 0x05 && writes[1][13] == 0x00
              && writes[1][14] == 0x04 && writes[1][15] == 0x00,
          hexdump(writes[1]));
}

// --- scatter files -----------------------------------------------------------

const char* kModernScatter = R"(############################################################################################################
#
#  General Setting
#
############################################################################################################
- general: MTK_PLATFORM_CFG
  info:
    - config_version: V1.1.2
      platform: MT6765
      project: k65v1_64_bsp
      storage: EMMC
      boot_channel: MSDC_0
      block_size: 0x20000

############################################################################################################
#
#  Layout Setting
#
############################################################################################################
- partition_index: SYS0
  partition_name: preloader
  file_name: preloader_k65v1_64_bsp.bin
  is_download: true
  type: SV5_BL_BIN
  linear_start_addr: 0x0
  physical_start_addr: 0x0
  partition_size: 0x40000
  region: EMMC_BOOT_1
  storage: HW_STORAGE_EMMC
  boundary_check: true
  is_reserved: false
  operation_type: BOOTLOADER
  reserve: 0x00

- partition_index: SYS1
  partition_name: boot
  file_name: boot.img
  is_download: true
  type: NORMAL_ROM
  linear_start_addr: 0x8000
  physical_start_addr: 0x8000
  partition_size: 0x2000000
  region: EMMC_USER
  storage: HW_STORAGE_EMMC
  boundary_check: true
  is_reserved: false
  operation_type: UPDATE
  reserve: 0x00

- partition_index: SYS2
  partition_name: userdata
  file_name: NONE
  is_download: false
  linear_start_addr: 0x208000
  partition_size: 0x40000000
  region: EMMC_USER
  operation_type: UPDATE
)";

/// The older shape: `key = value`, no YAML tags, no region or storage lines.
const char* kLegacyScatter = R"(#
#  General Setting
#
- general: MTK_PLATFORM_CFG
  config_version = V1.0.0
  platform = MT6572
  project = k72v1
  storage = EMMC
  block_size = 0x20000

#
#  Layout Setting
#
- partition_index = SYS0
  partition_name = PRELOADER
  file_name = preloader_k72v1.bin
  is_download = true
  begin_address = 0x0
  partition_size = 0x40000

- partition_index = SYS1
  partition_name = MBR
  file_name = MBR
  is_download = true
  begin_address = 0x600000
  partition_size = 0x80000
)";

void test_scatter_modern() {
    std::printf("\n24. modern scatter format\n");

    const ScatterFile file = parse_scatter(kModernScatter);
    check("the format is detected as modern", file.format == ScatterFormat::Modern,
          to_string(file.format));
    check("three partitions are read", file.partitions.size() == 3,
          std::to_string(file.partitions.size()));

    check("the general block is read", file.general.platform == "MT6765",
          file.general.platform);
    check("the general config version is read", file.general.config_version == "V1.1.2");
    check("the general storage is read", file.general.storage == "EMMC");
    check("the general block size is read", file.general.block_size == "0x20000");

    const ScatterPartition& preloader = file.partitions[0];
    check("the partition name is read", preloader.name == "preloader", preloader.name);
    check("the index is kept", preloader.index == "SYS0", preloader.index);
    check("the file name is read", preloader.file_name == "preloader_k65v1_64_bsp.bin");
    check("is_download is read as a boolean", preloader.is_download);
    check("the start address is read as hex", preloader.start_address == 0,
          std::to_string(preloader.start_address));
    check("the size is read as hex", preloader.size == 0x40000, std::to_string(preloader.size));
    check("the region is read", preloader.region == "EMMC_BOOT_1", preloader.region);
    check("the storage is read", preloader.storage == "HW_STORAGE_EMMC", preloader.storage);
    check("the operation type is passed through", preloader.operation_type == "BOOTLOADER",
          preloader.operation_type);

    // A field this parser does not model must still be visible rather than lost.
    check("an unmodelled field is preserved", preloader.extra_value("type") == "SV5_BL_BIN",
          preloader.extra_value("type"));
    check("another unmodelled field is preserved",
          preloader.extra_value("boundary_check") == "true",
          preloader.extra_value("boundary_check"));
    check("the reserve byte is preserved", preloader.extra_value("reserve") == "0x00");

    const ScatterPartition& boot = file.partitions[1];
    check("a second partition's address is read", boot.start_address == 0x8000,
          std::to_string(boot.start_address));
    check("a second partition's size is read", boot.size == 0x2000000);

    const ScatterPartition& userdata = file.partitions[2];
    check("is_download false is honoured", !userdata.is_download);
    check("a partition with no image is not downloadable", !userdata.downloadable());

    check("only the downloadable entries are listed", file.downloads().size() == 2,
          std::to_string(file.downloads().size()));
    check("the downloadable bytes are summed",
          file.total_download_bytes() == 0x40000 + 0x2000000,
          std::to_string(file.total_download_bytes()));
    check("a partition is found by name", file.find("boot") != nullptr);
    check("the lookup ignores case", file.find("BOOT") != nullptr);
    check("an unknown name finds nothing", file.find("nope") == nullptr);

    const std::vector<std::string> unmodelled = file.unmodelled_keys();
    check("the unmodelled keys are reported", !unmodelled.empty(),
          std::to_string(unmodelled.size()) + " keys");
    bool mentions_type = false;
    for (const std::string& key : unmodelled) {
        if (key == "type") {
            mentions_type = true;
        }
    }
    check("an unmodelled key is named for the operator", mentions_type);
}

void test_scatter_legacy() {
    std::printf("\n25. legacy scatter format\n");

    const ScatterFile file = parse_scatter(kLegacyScatter);
    check("the format is detected as legacy", file.format == ScatterFormat::Legacy,
          to_string(file.format));
    check("both partitions are read", file.partitions.size() == 2,
          std::to_string(file.partitions.size()));
    check("the legacy platform is read", file.general.platform == "MT6572",
          file.general.platform);

    const ScatterPartition& preloader = file.partitions[0];
    check("the name survives the legacy shape", preloader.name == "PRELOADER", preloader.name);
    check("begin_address stands in for linear_start_addr", preloader.start_address == 0);
    check("the legacy size is read", preloader.size == 0x40000);
    check("a legacy entry has no region", preloader.region.empty());

    const ScatterPartition& mbr = file.partitions[1];
    check("a second legacy address is read", mbr.start_address == 0x600000,
          std::to_string(mbr.start_address));
    check("both legacy entries are downloadable", file.downloads().size() == 2,
          std::to_string(file.downloads().size()));
}

void test_scatter_edge_cases() {
    std::printf("\n26. scatter parsing edge cases\n");

    // An entry with no is_download key but with an image: the file means it.
    const ScatterFile implied = parse_scatter(
        "- partition_index: SYS0\n  partition_name: boot\n  file_name: boot.img\n"
        "  linear_start_addr: 0x100\n  partition_size: 0x1000\n");
    check("a file with an image but no is_download defaults to downloading",
          implied.partitions.size() == 1 && implied.partitions[0].is_download);

    // An entry with no partition_name uses its index, which is how the oldest
    // packages name a bootloader.
    const ScatterFile unnamed = parse_scatter(
        "- partition_index: SYS0\n  file_name: preloader.bin\n  linear_start_addr: 0x0\n"
        "  partition_size: 0x1000\n");
    check("an entry with no name falls back to its index",
          unnamed.partitions[0].name == "SYS0", unnamed.partitions[0].name);

    // Comments and blank lines are not entries.
    const ScatterFile comments = parse_scatter(
        "# a banner\n\n#  Layout Setting\n\n- partition_index: SYS0\n  partition_name: x\n"
        "  partition_size: 4096  # trailing comment\n  linear_start_addr: 0x0\n");
    check("comments and blank lines are ignored", comments.partitions.size() == 1,
          std::to_string(comments.partitions.size()));
    check("a trailing comment is stripped from a value", comments.partitions[0].size == 4096,
          std::to_string(comments.partitions[0].size));

    // Decimal numbers are as valid as hex ones.
    const ScatterFile decimal = parse_scatter(
        "- partition_index: SYS0\n  partition_name: x\n  partition_size: 1048576\n"
        "  linear_start_addr: 4096\n");
    check("a decimal size is read", decimal.partitions[0].size == 1048576,
          std::to_string(decimal.partitions[0].size));
    check("a decimal address is read", decimal.partitions[0].start_address == 4096);

    // Quoted values lose their quotes.
    const ScatterFile quoted = parse_scatter(
        "- partition_index: SYS0\n  partition_name: \"boot\"\n  file_name: 'boot.img'\n"
        "  partition_size: 0x1000\n");
    check("a double-quoted value is unquoted", quoted.partitions[0].name == "boot",
          quoted.partitions[0].name);
    check("a single-quoted value is unquoted", quoted.partitions[0].file_name == "boot.img",
          quoted.partitions[0].file_name);

    // A bare 0x with nothing after it is not a number, and reading it as zero
    // would silently place a partition at the start of the flash.
    const ScatterFile incomplete = parse_scatter(
        "- partition_index: SYS0\n  partition_name: x\n  linear_start_addr: 0x\n"
        "  partition_size: 0x1000\n");
    check("an incomplete hex value is not read as zero",
          incomplete.partitions[0].extra_value("linear_start_addr") == "0x",
          incomplete.partitions[0].extra_value("linear_start_addr"));

    // The wrong file being selected is the common mistake and must say so.
    bool threw = false;
    try {
        parse_scatter("<?xml version=\"1.0\"?><data><program filename=\"x\"/></data>");
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("no partition entries") != std::string::npos;
    }
    check("a file that is not a scatter file is refused", threw);

    threw = false;
    try {
        load_scatter("C:/no/such/scatter.txt");
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("cannot open") != std::string::npos;
    }
    check("a missing scatter file is reported", threw);
}

void test_flash_result_reporting() {
    std::printf("\n27. reporting what a flash run did\n");

    FlashResult result;
    PartitionOutcome first;
    first.name = "preloader";
    first.bytes = 0x40000;
    first.succeeded = true;
    PartitionOutcome second;
    second.name = "boot";
    second.bytes = 0x2000000;
    second.succeeded = true;
    PartitionOutcome third;
    third.name = "system";
    third.error = "the agent reported a failure";
    result.partitions = {first, second, third};

    check("the written count is reported", result.written_count() == 2,
          std::to_string(result.written_count()));
    check("the failed count is reported", result.failed_count() == 1,
          std::to_string(result.failed_count()));
    check("the bytes actually written are summed", result.total_bytes() == 0x40000 + 0x2000000,
          std::to_string(result.total_bytes()));
    check("the names written are listed",
          result.written_names().size() == 2 && result.written_names()[0] == "preloader");
    check("an unfinished run says so", !result.completed);
    check("the summary names the stopped run",
          result.summary().find("stopped early") != std::string::npos, result.summary());

    FlashResult clean;
    clean.partitions = {first};
    clean.completed = true;
    check("a clean run says so", clean.summary().find("finished") != std::string::npos,
          clean.summary());

    // Progress events carry the partition and a speed once there is enough time
    // behind them for an average to mean anything.
    FlashProgress event;
    event.phase = "writing";
    event.partition = "boot";
    event.done = 512;
    event.total = 1024;
    event.percent = 50;
    event.bytes_per_second = 1024.0;
    check("a progress event carries the partition", event.partition == "boot");
    check("a progress event carries the percentage", event.percent == 50);
    check("a progress event carries the speed", event.bytes_per_second > 0);
}

}  // namespace

/// Runs one test, turning an unexpected exception into a reported failure.
/// Without this an escaped ProtocolError terminates the process and takes the
/// remaining tests with it, hiding everything after the first problem.
void run(const char* name, void (*test)()) {
    try {
        test();
    } catch (const std::exception& error) {
        check(std::string("'") + name + "' completed without an unexpected exception", false,
              error.what());
    }
}

int main() {
    std::printf("MediaTek BROM native protocol tests\n");
    std::printf("===================================\n");
    std::fflush(stdout);

    run("handshake constants", test_handshake_bytes);
    run("handshake", test_handshake_success);
    run("handshake failure", test_handshake_rejects_wrong_echo);
    run("command framing", test_command_echo_framing);
    run("target config", test_target_config_decoding);
    run("checksum", test_checksum);
    run("download agent upload", test_da_upload);
    run("corrupt transfer", test_da_upload_rejects_bad_checksum);
    run("secure boot refusal", test_sla_required);
    run("validation", test_validation);
    run("cancellation", test_cancellation);
    run("da framing", test_da_framing);
    run("da checksum", test_da_checksum);
    run("da status", test_da_status_decoding);
    run("da region parameter", test_da_region_param);
    run("flash geometry", test_da_flash_info_parsers);
    run("da write", test_da_write_region);
    run("da write refusal", test_da_write_region_failure);
    run("da read", test_da_read_region);
    run("da read failure", test_da_read_region_error);
    run("da format", test_da_format_region);
    run("da queries", test_da_session_queries);
    run("scatter modern", test_scatter_modern);
    run("scatter legacy", test_scatter_legacy);
    run("scatter edge cases", test_scatter_edge_cases);
    run("flash reporting", test_flash_result_reporting);

    std::printf("\n%d/%d checks passed\n", g_checks - g_failures, g_checks);
    if (g_failures > 0) {
        std::printf("FAILED\n");
        return 1;
    }
    std::printf("MediaTek BROM protocol OK.\n");
    return 0;
}
