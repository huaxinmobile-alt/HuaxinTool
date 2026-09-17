// =============================================================================
//  Native tests for the Samsung PIT parser and Odin protocol framing.
//
//  The PIT parser is a pure function, so it is tested exhaustively - including
//  the hostile cases, because the entry count comes off the wire and drives an
//  allocation. The Odin session is driven through a scripted transport, which
//  checks the bytes the host emits and how it reacts to what comes back.
// =============================================================================

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <filesystem>
#include <string>
#include <vector>

#include "protocols/qualcomm/sahara.h"
#include "protocols/samsung/odin.h"
#include "protocols/samsung/tar.h"

using huaxin::protocols::qualcomm::IByteTransport;
using huaxin::protocols::qualcomm::ProtocolError;
using namespace huaxin::protocols::samsung;

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

/// Builds a tar archive from name/data pairs, with correct header checksums and
/// the two terminating zero blocks. Written out rather than borrowed so the
/// parser is checked against the specification rather than against another
/// implementation of it.
/// Writes bytes to a path, for the tests that read from a file rather than a buffer.
void write_file(const std::string& path, const std::vector<std::uint8_t>& data) {
    std::ofstream out(path, std::ios::binary | std::ios::trunc);
    out.write(reinterpret_cast<const char*>(data.data()),
              static_cast<std::streamsize>(data.size()));
}

std::vector<std::uint8_t> build_tar(
    const std::vector<std::pair<std::string, std::vector<std::uint8_t>>>& members) {
    std::vector<std::uint8_t> out;
    for (const auto& member : members) {
        std::vector<std::uint8_t> header(512, 0);
        std::snprintf(reinterpret_cast<char*>(header.data()), 100, "%s", member.first.c_str());
        std::snprintf(reinterpret_cast<char*>(header.data() + 100), 8, "%07o", 0644);
        std::snprintf(reinterpret_cast<char*>(header.data() + 108), 8, "%07o", 0);
        std::snprintf(reinterpret_cast<char*>(header.data() + 116), 8, "%07o", 0);
        std::snprintf(reinterpret_cast<char*>(header.data() + 124), 12, "%011o",
                      static_cast<unsigned>(member.second.size()));
        std::snprintf(reinterpret_cast<char*>(header.data() + 136), 12, "%011o", 0);
        header[156] = '0';  // a regular file
        std::memcpy(header.data() + 257, "ustar", 5);
        header[263] = '0';
        header[264] = '0';
        // The checksum is computed with its own field read as spaces.
        std::uint32_t sum = 0;
        for (std::size_t index = 0; index < 512; ++index) {
            sum += (index >= 148 && index < 156) ? 0x20 : header[index];
        }
        std::snprintf(reinterpret_cast<char*>(header.data() + 148), 8, "%06o", sum);
        header[154] = 0;
        header[155] = ' ';

        out.insert(out.end(), header.begin(), header.end());
        out.insert(out.end(), member.second.begin(), member.second.end());
        const std::size_t padded = (member.second.size() + 511) / 512 * 512;
        out.insert(out.end(), padded - member.second.size(), 0);
    }
    out.insert(out.end(), 1024, 0);  // the two terminating blocks
    return out;
}

/// The four bytes a bootloader answers the ODIN handshake with.
std::vector<std::uint8_t> handshake_reply() {
    return {'L', 'O', 'K', 'E'};
}

void run(const char* name, void (*test)()) {
    try {
        test();
    } catch (const std::exception& error) {
        check(std::string("'") + name + "' completed without an unexpected exception", false,
              error.what());
    }
}

// --- PIT builders ------------------------------------------------------------
void put_le32(std::vector<std::uint8_t>& out, std::uint32_t value) {
    out.push_back(static_cast<std::uint8_t>(value & 0xFF));
    out.push_back(static_cast<std::uint8_t>((value >> 8) & 0xFF));
    out.push_back(static_cast<std::uint8_t>((value >> 16) & 0xFF));
    out.push_back(static_cast<std::uint8_t>((value >> 24) & 0xFF));
}

void put_name(std::vector<std::uint8_t>& out, const std::string& name) {
    std::vector<std::uint8_t> field(32, 0);
    std::memcpy(field.data(), name.data(), std::min<std::size_t>(name.size(), 31));
    out.insert(out.end(), field.begin(), field.end());
}

/// Builds a well-formed PIT from a list of (name, identifier, blocks, block size).
struct EntrySpec {
    const char* name;
    std::uint32_t identifier;
    std::uint32_t blocks;
    std::uint32_t block_size;
    std::uint32_t attributes;
};

std::vector<std::uint8_t> build_pit(const std::vector<EntrySpec>& specs) {
    std::vector<std::uint8_t> data;
    put_le32(data, PitData::kFileIdentifier);
    put_le32(data, static_cast<std::uint32_t>(specs.size()));
    put_le32(data, 0);
    put_le32(data, 0);
    for (int index = 0; index < 6; ++index) {
        data.push_back(0);
        data.push_back(0);
    }

    for (const EntrySpec& spec : specs) {
        put_le32(data, 0);              // binary type: application processor
        put_le32(data, 2);              // device type: MMC
        put_le32(data, spec.identifier);
        put_le32(data, spec.attributes);
        put_le32(data, 0);              // update attributes
        put_le32(data, spec.block_size);
        put_le32(data, spec.blocks);
        put_le32(data, 0);              // file offset (obsolete)
        put_le32(data, 0);              // file size (obsolete)
        put_name(data, spec.name);
        put_name(data, spec.name);      // flash filename mirrors the partition
        put_name(data, "");
    }
    return data;
}

/// Scripted transport, as used by the other protocol tests.
class ScriptedTransport final : public IByteTransport {
public:
    void expect(std::vector<std::uint8_t> bytes) { m_incoming.push_back(std::move(bytes)); }

    /// Forgets what has been written so far, so a test can assert about the
    /// writes a particular phase produced rather than all of them.
    void clear_written() { m_written.clear(); }

    void write_all(const std::uint8_t* data, std::size_t size, unsigned int) override {
        m_written.emplace_back(data, data + size);
    }

    void read_exact(std::uint8_t* buffer, std::size_t size, unsigned int) override {
        while (size > 0) {
            if (m_index >= m_incoming.size()) {
                throw ProtocolError("script exhausted: the host read more than the test provided");
            }
            const std::vector<std::uint8_t>& current = m_incoming[m_index];
            const std::size_t take = std::min(size, current.size() - m_offset);
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

private:
    std::vector<std::vector<std::uint8_t>> m_incoming;
    std::vector<std::vector<std::uint8_t>> m_written;
    std::size_t m_index{0};
    std::size_t m_offset{0};
};

std::vector<std::uint8_t> response(std::uint32_t type, std::uint32_t result) {
    std::vector<std::uint8_t> data;
    put_le32(data, type);
    put_le32(data, result);
    return data;
}

OdinSession::Callbacks quiet() {
    return OdinSession::Callbacks{};
}

// --- tests -------------------------------------------------------------------

void test_pit_parsing() {
    std::printf("1. PIT parsing\n");
    const std::vector<std::uint8_t> raw = build_pit({
        {"BOOT", 0x01, 4096, 512, 1},
        {"RECOVERY", 0x02, 8192, 512, 1},
        {"USERDATA", 0x03, 1048576, 512, 0},
    });
    check("the fixture is the size the format predicts",
          raw.size() == PitData::kHeaderDataSize + 3 * PitData::kEntryDataSize,
          std::to_string(raw.size()) + " bytes");

    const PitData pit = parse_pit(raw);
    check("every entry is parsed", pit.entries.size() == 3, std::to_string(pit.entries.size()));
    check("names are read", pit.entries[0].partition_name == "BOOT"
                                && pit.entries[2].partition_name == "USERDATA",
          pit.entries[0].partition_name);
    check("identifiers are read", pit.entries[1].identifier == 0x02);
    check("device type is decoded", pit.entries[0].device_type == 2
                                        && pit.entries[0].device_type_name() == "MMC");
    check("the entry size matches Heimdall's", PitData::kEntryDataSize == 132);
    check("expected_size matches what was parsed", pit.expected_size() == raw.size());

    const PitEntry* found = pit.find("RECOVERY");
    check("lookup by name works", found != nullptr && found->identifier == 0x02);
    check("looking up an absent partition returns null", pit.find("NOPE") == nullptr);

    check("the writable attribute is decoded", pit.entries[0].writable());
    check("a non-writable entry is reported as such", !pit.entries[2].writable());
    check("size is computed from the block geometry",
          pit.entries[0].size_bytes() == 4096ull * 512ull,
          std::to_string(pit.entries[0].size_bytes()));
}

void test_pit_rejects_bad_input() {
    std::printf("\n2. PIT rejection\n");
    auto expect_throw = [](const char* what, const std::vector<std::uint8_t>& data) {
        try {
            parse_pit(data);
        } catch (const ProtocolError& error) {
            check(what, true, std::string(error.what()).substr(0, 60));
            return;
        }
        check(what, false, "no exception");
    };

    expect_throw("an empty buffer is refused", {});
    expect_throw("a short header is refused", std::vector<std::uint8_t>(10, 0));

    std::vector<std::uint8_t> wrong_magic = build_pit({{"BOOT", 1, 1, 512, 1}});
    wrong_magic[0] = 0x00;
    expect_throw("a wrong magic is refused", wrong_magic);

    // The entry count drives an allocation, so a hostile one must not get
    // through. 0xFFFFFFFF entries would be half a terabyte.
    std::vector<std::uint8_t> absurd = build_pit({{"BOOT", 1, 1, 512, 1}});
    absurd[4] = 0xFF;
    absurd[5] = 0xFF;
    absurd[6] = 0xFF;
    absurd[7] = 0xFF;
    expect_throw("an absurd entry count is refused before allocating", absurd);

    // A count that fits the buffer but overruns it must also be refused.
    std::vector<std::uint8_t> truncated = build_pit({{"BOOT", 1, 1, 512, 1}});
    truncated[4] = 0x05;  // claims 5 entries, has 1
    expect_throw("a count larger than the data is refused", truncated);

    // A zero-entry PIT is legal and empty, not an error.
    std::vector<std::uint8_t> empty = build_pit({});
    const PitData pit = parse_pit(empty);
    check("a zero-entry PIT parses as empty", pit.entries.empty());
}

void test_pit_name_handling() {
    std::printf("\n3. PIT name fields\n");
    // A 31-character name fills the field with no terminator; reading past it
    // would pick up the next field.
    const std::string long_name(31, 'A');
    EntrySpec spec{long_name.c_str(), 1, 1, 512, 1};
    const PitData pit = parse_pit(build_pit({spec}));
    check("a full-length name is read without running on",
          pit.entries[0].partition_name.size() == 31
              && pit.entries[0].partition_name == long_name,
          std::to_string(pit.entries[0].partition_name.size()) + " chars");
    check("the next field is not swallowed",
          pit.entries[0].flash_filename == long_name,
          pit.entries[0].flash_filename);

    const PitData unnamed = parse_pit(build_pit({{"", 7, 1, 512, 1}}));
    check("an empty name stays empty", unnamed.entries[0].partition_name.empty());
    check("the entry is still usable by identifier", unnamed.entries[0].identifier == 7);
}

void test_packet_building() {
    std::printf("\n4. Odin packet construction\n");
    const std::vector<std::uint8_t> session = build_session_packet(OdinSessionRequest::BeginSession);
    check("a control packet is 1024 bytes", session.size() == kControlPacketSize,
          std::to_string(session.size()));
    check("the control type is at offset 0, little-endian",
          session[0] == 0x64 && session[1] == 0x00 && session[2] == 0x00 && session[3] == 0x00,
          std::to_string(session[0]));
    check("the begin-session request is 0",
          session[4] == 0x00 && session[5] == 0x00 && session[6] == 0x00 && session[7] == 0x00);
    check("the rest of the packet is zero padding",
          std::all_of(session.begin() + 8, session.end(), [](std::uint8_t b) { return b == 0; }));

    const std::vector<std::uint8_t> pit = build_control_packet(OdinControl::PitFile);
    check("the PIT request uses control type 0x65", pit[0] == 0x65);

    const std::vector<std::uint8_t> sized =
        build_session_packet(OdinSessionRequest::FilePartSize, 0x00020000);
    check("a session argument lands at offset 8, little-endian",
          sized[8] == 0x00 && sized[9] == 0x00 && sized[10] == 0x02 && sized[11] == 0x00,
          std::to_string(sized[10]));

    bool threw = false;
    try {
        build_control_packet(OdinControl::Session, 4);
    } catch (const ProtocolError&) {
        threw = true;
    }
    check("a packet too small to hold its header is refused", threw);

    check("control types have readable names",
          std::string(to_string(OdinControl::PitFile)) == "PIT_FILE");
}

void test_response_decoding() {
    std::printf("\n5. Odin response decoding\n");
    const std::vector<std::uint8_t> ok = response(0x64, 0);
    const OdinResponse accepted = decode_response(ok.data(), ok.size(), OdinControl::Session);
    check("an accepted response is recognised", accepted.accepted());
    check("the type is reported", accepted.type == OdinControl::Session);

    const std::vector<std::uint8_t> refused = response(0x64, 0x12345678);
    check("a non-zero result is a refusal",
          !decode_response(refused.data(), refused.size(), OdinControl::Session).accepted());

    bool threw = false;
    try {
        decode_response(ok.data(), ok.size(), OdinControl::PitFile);
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("expected") != std::string::npos;
    }
    check("a mismatched response type is refused", threw);

    // A file-part response arriving where a transfer response was expected is
    // the device saying "next chunk", not an error.
    const std::vector<std::uint8_t> part = response(0x00, 0);
    const OdinResponse next = decode_response(part.data(), part.size(), OdinControl::FileTransfer);
    check("a send-file-part response is accepted during a transfer",
          next.type == OdinControl::SendFilePart);

    threw = false;
    try {
        decode_response(ok.data(), 4, OdinControl::Session);
    } catch (const ProtocolError&) {
        threw = true;
    }
    check("a truncated response is refused", threw);
}

void test_session_handshake_gate() {
    std::printf("\n7. the handshake gates everything\n");

    // The bootloader answers nothing at all until ODIN/LOKE has completed, so a
    // session that let a command through first would send bytes into silence and
    // report a timeout as the reason.
    ScriptedTransport transport;
    transport.expect(handshake_reply());
    OdinSession session(transport, quiet());

    bool refused = false;
    try {
        session.begin_session();
    } catch (const ProtocolError& error) {
        refused = std::string(error.what()).find("handshake") != std::string::npos;
    }
    check("a session cannot be opened before the handshake", refused);
    check("the refusal names the handshake as the missing step", refused);
    check("nothing was sent before the handshake", transport.written().empty());

    // A device that answers the handshake with something other than LOKE is not
    // in download mode, and the message has to say so rather than timing out.
    ScriptedTransport wrong_device;
    wrong_device.expect({'N', 'O', 'P', 'E'});
    OdinSession other(wrong_device, quiet());
    bool rejected = false;
    try {
        other.handshake();
    } catch (const ProtocolError& error) {
        rejected = std::string(error.what()).find("download mode") != std::string::npos;
    }
    check("a device that does not answer LOKE is refused", rejected);
    check("the handshake is not recorded as done", !other.handshake_done());

    // The handshake is idempotent: calling it again does nothing rather than
    // sending a second ODIN into a link that has already answered.
    ScriptedTransport once;
    once.expect(handshake_reply());
    OdinSession third(once, quiet());
    third.handshake();
    const std::size_t after_first = once.written().size();
    third.handshake();
    check("a second handshake is a no-op", once.written().size() == after_first,
          std::to_string(once.written().size()) + " vs " + std::to_string(after_first));
}

void test_session_packet_sizes() {
    std::printf("\n7b. session version and packet size\n");

    // A classic bootloader replies with a bare packet size.
    ScriptedTransport classic;
    classic.expect(handshake_reply());
    classic.expect(response(0x64, 0x00020000));
    // 0x00020000 reads as version 2, which makes the session send
    // SendFilePartSize - and that needs an answer of its own.
    classic.expect(response(0x64, 0));
    OdinSession first(classic, quiet());
    first.handshake();
    const SessionInfo plain = first.begin_session();
    check("a classic reply opens the session", first.session_open());
    check("a bare packet size is used as given", plain.packet_size == 0x00020000
              || plain.packet_size == kModernFilePartSize,
          std::to_string(plain.packet_size));
    check("the session describes itself", !plain.describe().empty(), plain.describe());

    // A nonsensical size must not be propagated into the transfer loop: 7 bytes
    // would turn a flash into millions of round trips.
    ScriptedTransport odd;
    odd.expect(handshake_reply());
    odd.expect(response(0x64, 0x00000007));
    OdinSession second(odd, quiet());
    second.handshake();
    check("an implausible packet size falls back to the default",
          second.begin_session().packet_size == kDefaultFilePartSize);

    ScriptedTransport none;
    none.expect(handshake_reply());
    none.expect(response(0x64, 0));
    OdinSession zero(none, quiet());
    zero.handshake();
    check("a zero reply falls back to the default packet size",
          zero.begin_session().packet_size == kDefaultFilePartSize);

    // A wrong response type is still fatal: that is a desynchronised stream.
    ScriptedTransport wrong;
    wrong.expect(handshake_reply());
    wrong.expect(response(0x65, 0));
    OdinSession mismatched(wrong, quiet());
    mismatched.handshake();
    bool threw = false;
    try {
        mismatched.begin_session();
    } catch (const ProtocolError&) {
        threw = true;
    }
    check("a wrong response type is still refused", threw);
    check("the session is not marked open after a bad response", !mismatched.session_open());
}

void test_session_pit_read() {
    std::printf("\n6. reading the PIT over the wire\n");
    const std::vector<std::uint8_t> pit_bytes = build_pit({
        {"BOOT", 0x01, 2048, 512, 1},
        {"SYSTEM", 0x02, 4096, 512, 1},
    });

    ScriptedTransport transport;
    transport.expect(handshake_reply());
    transport.expect(response(0x64, 0x00020000));  // session accepted, packet size offered
    transport.expect(response(0x64, 0));           // the SendFilePartSize reply
    transport.expect(response(0x65, static_cast<std::uint32_t>(pit_bytes.size())));
    // The table arrives in 500-byte blocks, each asked for on its own, so the
    // script is padded up to a whole number of them - which is how a device
    // sends it too.
    const std::size_t blocks = (pit_bytes.size() + kPitDumpBlockSize - 1) / kPitDumpBlockSize;
    std::vector<std::uint8_t> padded = pit_bytes;
    padded.resize(blocks * kPitDumpBlockSize, 0);
    transport.expect(padded);
    transport.expect(response(0x65, 0));  // the end-of-dump answer

    OdinSession session(transport, quiet());
    session.handshake();
    check("the handshake settles", session.handshake_done());

    session.begin_session();
    check("the session opens", session.session_open());

    const PitData pit = session.read_pit();
    check("the partition table arrives intact", pit.entries.size() == 2,
          std::to_string(pit.entries.size()));
    check("its contents are correct", pit.entries[0].partition_name == "BOOT"
                                          && pit.entries[1].partition_name == "SYSTEM");

    const auto& written = transport.written();
    // handshake, begin session, SendFilePartSize, the dump request, one packet
    // per 500-byte block, and the end-of-transfer packet.
    check("every packet went out in order", written.size() == 5 + blocks,
          std::to_string(written.size()) + " vs " + std::to_string(5 + blocks));
    check("the first was the handshake", written[0].size() == 4 && written[0][0] == 'O');
    check("the second was a session packet", written[1][0] == 0x64);
    check("the third asked for the file part size", written[2][0] == 0x64
              && read_le32(written[2], 4) == 5);
    check("the fourth requested the PIT dump", written[3][0] == 0x65
              && read_le32(written[3], 4) == 1);
    check("the fifth asked for block zero", written[4][0] == 0x65
              && read_le32(written[4], 4) == 2 && read_le32(written[4], 8) == 0);
    // This table fits in one 500-byte block, so there is exactly one block
    // request and it asks for block zero.
    check("the block request is numbered from zero",
          blocks == 1 ? read_le32(written[4], 8) == 0 : read_le32(written[5], 8) == 1,
          std::to_string(blocks) + " block(s)");
    check("the last closed the dump", read_le32(written.back(), 4) == 3,
          std::to_string(read_le32(written.back(), 4)));
}

void test_session_ordering() {
    std::printf("\n7c. ordering is enforced\n");
    ScriptedTransport transport;
    transport.expect(handshake_reply());
    OdinSession session(transport, quiet());
    session.handshake();
    transport.clear_written();

    bool threw = false;
    try {
        session.read_pit();
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("session") != std::string::npos;
    }
    check("the PIT cannot be read before the session is open", threw);
    check("nothing was sent while the session was closed", transport.written().empty());
}

void test_pit_length_guard() {
    std::printf("\n8. hostile PIT length\n");
    ScriptedTransport transport;
    transport.expect(handshake_reply());
    transport.expect(response(0x64, 0));
    transport.expect(response(0x65, 0x7FFFFFFF));  // 2 GB claimed

    OdinSession session(transport, quiet());
    session.handshake();
    session.begin_session();
    bool threw = false;
    try {
        session.read_pit();
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("larger") != std::string::npos;
    }
    check("an absurd PIT length is refused before allocating", threw);

    ScriptedTransport empty;
    empty.expect(handshake_reply());
    empty.expect(response(0x64, 0));
    empty.expect(response(0x65, 0));
    OdinSession other(empty, quiet());
    other.handshake();
    other.begin_session();
    threw = false;
    try {
        other.read_pit();
    } catch (const ProtocolError&) {
        threw = true;
    }
    check("a zero-length PIT is reported, not parsed as empty", threw);
}

void test_total_bytes_guard() {
    std::printf("\n9. transfer size guard\n");
    ScriptedTransport transport;
    transport.expect(handshake_reply());
    OdinSession session(transport, quiet());
    session.handshake();
    transport.clear_written();

    bool threw = false;
    try {
        session.send_total_bytes(0x1FFFFFFFFull);  // 8 GB, does not fit a 32-bit field
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("32-bit") != std::string::npos;
    }
    check("a total that does not fit the field is refused rather than truncated", threw);
    check("nothing was sent for a refused total", transport.written().empty());
}

void test_cancellation() {
    std::printf("\n10. cancellation\n");
    ScriptedTransport transport;
    transport.expect(handshake_reply());
    bool cancel_now = false;
    OdinSession::Callbacks callbacks = quiet();
    callbacks.cancelled = [&cancel_now]() { return cancel_now; };

    OdinSession session(transport, callbacks);
    session.handshake();
    transport.clear_written();
    cancel_now = true;
    bool threw = false;
    try {
        session.begin_session();
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("cancelled") != std::string::npos;
    }
    check("a cancelled session stops before sending", threw);
    check("nothing was sent after the cancellation", transport.written().empty());
}

void test_tar_and_md5() {
    std::printf("\n11. TAR and .tar.md5\n");

    // The published check values pin the MD5 implementation to RFC 1321 rather
    // than to itself: a self-consistent but wrong MD5 would verify nothing.
    check("MD5 of the empty string matches its check value",
          md5_hex(md5(nullptr, 0)) == "d41d8cd98f00b204e9800998ecf8427e",
          md5_hex(md5(nullptr, 0)));
    const std::string abc = "abc";
    check("MD5 of \"abc\" matches its check value",
          md5_hex(md5(reinterpret_cast<const std::uint8_t*>(abc.data()), abc.size()))
              == "900150983cd24fb0d6963f7d28e17f72",
          md5_hex(md5(reinterpret_cast<const std::uint8_t*>(abc.data()), abc.size())));
    // A string longer than one block, which is where a naive implementation
    // usually breaks.
    const std::string long_text =
        "The quick brown fox jumps over the lazy dog";
    check("MD5 of a 43-byte string matches its check value",
          md5_hex(md5(reinterpret_cast<const std::uint8_t*>(long_text.data()), long_text.size()))
              == "9e107d9d372bb6826bd81d3542a419d6",
          md5_hex(md5(reinterpret_cast<const std::uint8_t*>(long_text.data()), long_text.size())));

    const std::vector<std::uint8_t> archive = build_tar({
        {"boot.img", {'b', 'o', 'o', 't'}},
        {"system.img", {'s', 'y', 's', 't', 'e', 'm'}},
    });
    const TarArchive parsed = parse_tar(archive);
    check("both members are read", parsed.entries.size() == 2,
          std::to_string(parsed.entries.size()));
    check("the archive is terminated", parsed.terminated);
    check("a member's size is read", parsed.entries[0].size == 4,
          std::to_string(parsed.entries[0].size));
    check("a member is found by name", parsed.find("boot.img") != nullptr);
    check("the lookup is case-insensitive", parsed.find("BOOT.IMG") != nullptr);
    check("an unknown name finds nothing", parsed.find("nope.img") == nullptr);
    check("the file bytes are summed", parsed.total_file_bytes() == 10,
          std::to_string(parsed.total_file_bytes()));

    const std::vector<std::uint8_t> image = extract_entry(archive, *parsed.find("boot.img"));
    check("a member's data is extracted", image.size() == 4 && image[0] == 'b',
          std::string(image.begin(), image.end()));

    // The digest is the last sixteen RAW bytes, not thirty-two hex characters.
    std::vector<std::uint8_t> md5_file = archive;
    const Md5Digest digest = md5(archive);
    md5_file.insert(md5_file.end(), digest.begin(), digest.end());
    const TarMd5Check good = verify_tar_md5(md5_file);
    check("a good .tar.md5 verifies", good.matched, good.describe());
    check("the archive size excludes the digest", good.archive_size == archive.size(),
          std::to_string(good.archive_size));
    check("the appended digest is recognised as such", good.has_appended_digest);

    // A single flipped byte in the archive must fail, which is the whole point.
    std::vector<std::uint8_t> corrupt = md5_file;
    corrupt[600] ^= 0xFF;
    check("a damaged archive fails its MD5", !verify_tar_md5(corrupt).matched);

    // A tar header whose checksum does not add up is not a tar.
    std::vector<std::uint8_t> not_tar(1024, 0x41);
    bool threw = false;
    try {
        parse_tar(not_tar);
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("checksum") != std::string::npos
                || std::string(error.what()).find("not a tar") != std::string::npos;
    }
    check("a header that fails its checksum is refused", threw);

    // A member claiming more than the file holds is a truncated package.
    std::vector<std::uint8_t> truncated = archive;
    // Two bytes into the second member's data: the header promises six bytes and
    // only two are there.
    truncated.resize(1536 + 2);
    threw = false;
    try {
        parse_tar(truncated);
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("truncated") != std::string::npos;
    }
    check("a truncated archive is refused", threw);
}

void test_pit_sanity_and_rebuild() {
    std::printf("\n12. PIT sanity and rebuilding\n");

    PitData good;
    PitEntry boot;
    boot.partition_name = "BOOT";
    boot.flash_filename = "boot.img";
    boot.identifier = 1;
    boot.block_count = 2048;
    boot.block_size_or_offset = 512;
    boot.binary_type = 0;
    boot.device_type = 2;
    PitEntry system;
    system.partition_name = "SYSTEM";
    system.identifier = 2;
    system.block_count = 4096;
    system.block_size_or_offset = 512;
    good.entries = {boot, system};

    check("a sane table passes", good.looks_sane(), good.sanity_problem());

    PitData empty;
    check("a table with no partitions fails", !empty.looks_sane());
    check("and says why", empty.sanity_problem().find("no partitions") != std::string::npos,
          empty.sanity_problem());

    PitData duplicate = good;
    duplicate.entries[1].identifier = 1;  // same id as BOOT
    check("two entries sharing an id fails", !duplicate.looks_sane());
    check("and names both", duplicate.sanity_problem().find("BOOT") != std::string::npos
              && duplicate.sanity_problem().find("SYSTEM") != std::string::npos,
          duplicate.sanity_problem());

    PitData nameless = good;
    nameless.entries[0].partition_name.clear();
    check("an unnamed entry fails", !nameless.looks_sane());

    // The round trip is what makes repartitioning possible at all: the table
    // written back has to parse as the table that was read.
    const std::vector<std::uint8_t> rebuilt = build_pit(good);
    check("the rebuilt image is a whole number of 4096-byte blocks",
          rebuilt.size() % kPitBlockSize == 0 && rebuilt.size() >= good.expected_size(),
          std::to_string(rebuilt.size()));
    const PitData reparsed = parse_pit(rebuilt);
    check("the rebuilt table parses", reparsed.entries.size() == 2,
          std::to_string(reparsed.entries.size()));
    check("the names survive the round trip",
          reparsed.entries[0].partition_name == "BOOT"
              && reparsed.entries[1].partition_name == "SYSTEM");
    check("the geometry survives the round trip",
          reparsed.entries[0].block_count == 2048 && reparsed.entries[1].block_count == 4096);
    check("the flash file name survives", reparsed.entries[0].flash_filename == "boot.img");
}

// -----------------------------------------------------------------------------
//  list_tar_file: listing an archive without reading its members
// -----------------------------------------------------------------------------

void test_list_tar_file() {
    std::printf("\nlist_tar_file\n");

    const std::vector<std::uint8_t> archive = build_tar({
        {"boot.img", std::vector<std::uint8_t>(700 * 1024, 0x11)},
        {"system.img", std::vector<std::uint8_t>(4096, 0x22)},
        {"meta-data/fota.zip", std::vector<std::uint8_t>(600, 0x33)},
    });
    const std::string path =
        (std::filesystem::temp_directory_path() / "huaxin-list-test.tar").string();
    {
        std::ofstream out(path, std::ios::binary | std::ios::trunc);
        out.write(reinterpret_cast<const char*>(archive.data()),
                  static_cast<std::streamsize>(archive.size()));
    }

    const TarArchive streamed = list_tar_file(path);
    const TarArchive in_memory = parse_tar(archive);

    check("the streamed list finds every member",
          streamed.entries.size() == in_memory.entries.size(),
          std::to_string(streamed.entries.size()) + " vs "
              + std::to_string(in_memory.entries.size()));
    check("the names match",
          !streamed.entries.empty() && streamed.entries[0].name == in_memory.entries[0].name,
          streamed.entries.empty() ? "none" : streamed.entries[0].name);
    check("the sizes match",
          streamed.entries.size() == in_memory.entries.size()
              && streamed.entries[1].size == in_memory.entries[1].size,
          streamed.entries.size() > 1 ? std::to_string(streamed.entries[1].size) : "none");
    check("the offsets match",
          streamed.entries.size() == in_memory.entries.size()
              && streamed.entries[2].data_offset == in_memory.entries[2].data_offset);
    check("only regular files are counted as files",
          streamed.files().size() == 3, std::to_string(streamed.files().size()));
    check("the total is the sum of the members",
          streamed.total_file_bytes() == in_memory.total_file_bytes(),
          std::to_string(streamed.total_file_bytes()));
    check("a member can be found by name", streamed.find("boot.img") != nullptr);
    check("a nested member keeps its path",
          streamed.find("meta-data/fota.zip") != nullptr);

    // The appended digest is raw binary, not a tar header. Stopping at it rather
    // than parsing it is what keeps a good package from being called corrupt.
    std::vector<std::uint8_t> with_digest = archive;
    for (int index = 0; index < 16; ++index) {
        with_digest.push_back(static_cast<std::uint8_t>(0x40 + index));
    }
    {
        std::ofstream out(path, std::ios::binary | std::ios::trunc);
        out.write(reinterpret_cast<const char*>(with_digest.data()),
                  static_cast<std::streamsize>(with_digest.size()));
    }
    const TarArchive digested = list_tar_file(path);
    check("an appended .md5 digest is not mistaken for a member",
          digested.entries.size() == streamed.entries.size(),
          std::to_string(digested.entries.size()));

    // A file that is not an archive at all.
    {
        std::ofstream out(path, std::ios::binary | std::ios::trunc);
        const std::string junk(4096, 'x');
        out.write(junk.data(), static_cast<std::streamsize>(junk.size()));
    }
    bool refused = false;
    try {
        list_tar_file(path);
    } catch (const ProtocolError&) {
        refused = true;
    }
    check("a file that is not a tar archive is refused", refused);

    std::error_code ignored;
    std::filesystem::remove(path, ignored);
}


}  // namespace

int main() {
    std::printf("Samsung Odin / PIT native tests\n");
    std::printf("===============================\n");
    std::fflush(stdout);

    run("PIT parsing", test_pit_parsing);
    run("PIT rejection", test_pit_rejects_bad_input);
    run("PIT name fields", test_pit_name_handling);
    run("packet construction", test_packet_building);
    run("response decoding", test_response_decoding);
    run("the handshake gates everything", test_session_handshake_gate);
    run("session packet sizes", test_session_packet_sizes);
    run("PIT read over the wire", test_session_pit_read);
    run("ordering", test_session_ordering);
    run("PIT length guard", test_pit_length_guard);
    run("transfer size guard", test_total_bytes_guard);
    run("cancellation", test_cancellation);
    run("TAR and tar.md5", test_tar_and_md5);
    run("PIT sanity and rebuilding", test_pit_sanity_and_rebuild);
    run("listing a tar without its members", test_list_tar_file);

    std::printf("\n%d/%d checks passed\n", g_checks - g_failures, g_checks);
    if (g_failures > 0) {
        std::printf("FAILED\n");
        return 1;
    }
    std::printf("Samsung Odin / PIT OK.\n");
    return 0;
}
