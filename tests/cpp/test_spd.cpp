// =============================================================================
//  Native tests for the Unisoc / Spreadtrum layer.
//
//  Two halves, and neither needs hardware.
//
//  The PAC parser is pure: a package is built byte for byte here, to the layout
//  in the vendor's own header, and the parser is checked against it. That covers
//  the field offsets, the two versions, both CRCs, and every way a damaged
//  package can be refused.
//
//  The BSL protocol runs through a scripted device that plays back frames a real
//  boot ROM sends and records what the host wrote. That is the only practical
//  way to check the HDLC framing, the escaping, the two checksums, and the
//  checksum detection - none of which can be verified by reading the code.
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
#include "protocols/spd/bsl.h"
#include "protocols/spd/checksum.h"
#include "protocols/spd/pac.h"

using namespace huaxin::protocols::spd;
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

void run(const char* name, void (*test)());

// --- PAC construction ---------------------------------------------------------

void put_u16(std::vector<std::uint8_t>& out, std::size_t offset, std::uint16_t value) {
    out[offset] = static_cast<std::uint8_t>(value & 0xFF);
    out[offset + 1] = static_cast<std::uint8_t>((value >> 8) & 0xFF);
}

void put_u32(std::vector<std::uint8_t>& out, std::size_t offset, std::uint32_t value) {
    out[offset] = static_cast<std::uint8_t>(value & 0xFF);
    out[offset + 1] = static_cast<std::uint8_t>((value >> 8) & 0xFF);
    out[offset + 2] = static_cast<std::uint8_t>((value >> 16) & 0xFF);
    out[offset + 3] = static_cast<std::uint8_t>((value >> 24) & 0xFF);
}

/// Writes a NUL-terminated UTF-16LE string into a fixed-width field.
void put_utf16(std::vector<std::uint8_t>& out, std::size_t offset, std::size_t length,
               const std::string& text) {
    for (std::size_t index = 0; index < text.size() && (index + 1) * 2 <= length; ++index) {
        put_u16(out, offset + index * 2, static_cast<std::uint16_t>(text[index]));
    }
}

/// Writes bytes to a path, for the tests that read from a file rather than a buffer.
void write_file(const std::string& path, const std::vector<std::uint8_t>& data) {
    std::ofstream out(path, std::ios::binary | std::ios::trunc);
    out.write(reinterpret_cast<const char*>(data.data()),
              static_cast<std::streamsize>(data.size()));
}

/// One file entry to place in a test package.
struct TestEntry {
    std::string file_id;
    std::string file_name;
    std::uint32_t address{0};
    std::vector<std::uint8_t> payload;
    bool marker{false};
    std::uint32_t flag{1};
};

/// Builds a PAC to the vendor's layout. `version_two` selects the V2 layout:
/// a 44-byte version field with the size split across 44 and 48, and per-entry
/// high halves at 1532 and 1536.
std::vector<std::uint8_t> build_pac(const std::vector<TestEntry>& entries,
                                    bool version_two = false) {
    const std::size_t header_size = kPacHeaderSize;
    const std::size_t entry_size = kPacEntrySize;
    const std::size_t table_size = entry_size * entries.size();

    std::size_t payload_size = 0;
    for (const TestEntry& entry : entries) {
        if (!entry.marker) {
            payload_size += entry.payload.size();
        }
    }
    std::vector<std::uint8_t> pac(header_size + table_size + payload_size, 0);

    put_utf16(pac, 0, version_two ? 44 : 48, "BP_R1.0.0");
    put_utf16(pac, 52, 512, "TEST_PRODUCT");
    put_utf16(pac, 564, 512, "V1.2.3");
    put_utf16(pac, 1104, 200, "TEST_ALIAS");
    put_u32(pac, 1076, static_cast<std::uint32_t>(entries.size()));
    put_u32(pac, 1080, static_cast<std::uint32_t>(header_size));
    put_u32(pac, 1084, 1);          // mode
    put_u32(pac, 1088, 1);          // flash type
    put_u32(pac, 1312, 0);          // is preload

    std::size_t cursor = header_size + table_size;
    for (std::size_t index = 0; index < entries.size(); ++index) {
        const TestEntry& entry = entries[index];
        const std::size_t base = header_size + index * entry_size;
        put_u32(pac, base + 0, static_cast<std::uint32_t>(entry_size));
        put_utf16(pac, base + 4, 512, entry.file_id);
        put_utf16(pac, base + 516, 512, entry.file_name);
        put_u32(pac, base + 1544, entry.flag);
        put_u32(pac, base + 1548, 1);   // check flag
        put_u32(pac, base + 1556, 0);   // can omit
        put_u32(pac, base + 1560, 1);   // address count
        put_u32(pac, base + 1564, entry.address);

        if (entry.marker) {
            continue;
        }
        put_u32(pac, base + 1540, static_cast<std::uint32_t>(entry.payload.size()));
        put_u32(pac, base + 1552, static_cast<std::uint32_t>(cursor));
        std::copy(entry.payload.begin(), entry.payload.end(), pac.begin() + cursor);
        cursor += entry.payload.size();
    }

    // The size field the header declares is the whole file. In V2 the high half
    // stays zero for anything under 4 GiB, which is exactly why the parser can
    // treat such a file as V1 without losing anything.
    put_u32(pac, 44, static_cast<std::uint32_t>(pac.size() >> 32));
    put_u32(pac, 48, static_cast<std::uint32_t>(pac.size() & 0xFFFFFFFFu));

    put_u32(pac, 2116, kPacMagic);
    put_u16(pac, 2120, crc16_arc(pac.data(), 2120));
    put_u16(pac, 2122, crc16_arc(pac.data() + header_size, pac.size() - header_size));
    return pac;
}

std::vector<TestEntry> sample_entries() {
    TestEntry fdl1;
    fdl1.file_id = "HOST_FDL";
    fdl1.file_name = "fdl1-sign.bin";
    fdl1.address = 0x5500;
    fdl1.payload.assign(64, 0x11);

    TestEntry fdl2;
    fdl2.file_id = "FDL2";
    fdl2.file_name = "fdl2-sign.bin";
    fdl2.address = 0x40004000;
    fdl2.payload.assign(96, 0x22);

    TestEntry ap;
    ap.file_id = "AP";
    ap.file_name = "boot.img";
    ap.address = 0x10000000;
    ap.payload.assign(128, 0x33);

    TestEntry fmt;
    fmt.file_id = "FMT_FSSYS";
    fmt.marker = true;
    fmt.address = 0x0;
    fmt.flag = 0;

    return {fdl1, fdl2, ap, fmt};
}

// --- scripted device ----------------------------------------------------------

class ScriptedTransport final : public IByteTransport {
public:
    void expect(std::vector<std::uint8_t> bytes) { m_incoming.push_back(std::move(bytes)); }

    void write_all(const std::uint8_t* data, std::size_t size, unsigned int) override {
        m_written.insert(m_written.end(), data, data + size);
        m_writes.emplace_back(data, data + size);
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

    void control_transfer(std::uint8_t request_type, std::uint8_t request, std::uint16_t value,
                          std::uint16_t index, unsigned int) override {
        m_control_calls.push_back({request_type, request, value, index});
    }

    std::string describe() const override { return "scripted"; }

    const std::vector<std::uint8_t>& written() const { return m_written; }
    /// One entry per transport write, which for this protocol is one frame.
    const std::vector<std::vector<std::uint8_t>>& writes() const { return m_writes; }

    struct ControlCall {
        std::uint8_t request_type;
        std::uint8_t request;
        std::uint16_t value;
        std::uint16_t index;
    };
    const std::vector<ControlCall>& control_calls() const { return m_control_calls; }

    void clear_written() { m_written.clear(); m_writes.clear(); }

private:
    std::vector<std::vector<std::uint8_t>> m_incoming;
    std::vector<std::uint8_t> m_written;
    std::vector<std::vector<std::uint8_t>> m_writes;
    std::vector<ControlCall> m_control_calls;
    std::size_t m_index{0};
    std::size_t m_offset{0};
};

BslSession::Callbacks quiet() {
    return BslSession::Callbacks{};
}

/// A device-side frame, as the boot ROM would send it.
std::vector<std::uint8_t> device_frame(std::uint16_t type, const std::vector<std::uint8_t>& data,
                                       ChecksumKind kind) {
    return build_frame(type, data, kind);
}

// --- tests --------------------------------------------------------------------

void test_checksums() {
    std::printf("1. the four checksums\n");

    const std::string digits = "123456789";
    const std::vector<std::uint8_t> data(digits.begin(), digits.end());

    // The two check values are published for these named algorithms, so they
    // pin the implementations to the standards rather than to each other.
    // Init 0, not 0xFFFF: the protocol's variant is the one catalogued as
    // CRC-16/XMODEM. Getting this wrong is invisible until a device rejects
    // every frame, which is exactly why the check value is pinned here.
    check("CRC-16 with init 0 matches its published check value",
          crc16_ccitt(data) == 0x31C3,
          [&] { char b[24]; std::snprintf(b, sizeof(b), "0x%04X, want 0x31C3", crc16_ccitt(data)); return b; }());
    check("CRC-16/ARC matches its published check value",
          crc16_arc(data) == 0xBB3D,
          [&] { char b[16]; std::snprintf(b, sizeof(b), "0x%04X", crc16_arc(data)); return b; }());
    check("CRC-32/IEEE matches its published check value",
          crc32_ieee(data) == 0xCBF43926u,
          [&] { char b[16]; std::snprintf(b, sizeof(b), "0x%08X", crc32_ieee(data)); return b; }());

    // The two CRC-16s are different algorithms that share a name; a build that
    // used one where the other belongs would fail against every device.
    check("the two CRC-16s are not the same function", crc16_ccitt(data) != crc16_arc(data));

    // The Spreadtrum sum, spelled two ways in the sources. Summing big-endian
    // words and swapping at the end is the same as summing little-endian words
    // and not swapping, which is what the other reference implementation does.
    const std::vector<std::uint8_t> pair = {0x01, 0x02, 0x03, 0x04};
    std::uint32_t little_endian_total = 0;
    little_endian_total += (pair[0] | (pair[1] << 8));
    little_endian_total += (pair[2] | (pair[3] << 8));
    little_endian_total = (little_endian_total >> 16) + (little_endian_total & 0xFFFF);
    const std::uint16_t folded =
        static_cast<std::uint16_t>(~(little_endian_total + (little_endian_total >> 16)) & 0xFFFF);
    const std::uint16_t expected_swapped =
        static_cast<std::uint16_t>((folded >> 8) | ((folded & 0xFF) << 8));
    check("the Spreadtrum sum agrees with the other spelling of itself",
          sprd_sum(pair) == expected_swapped,
          [&] { char b[32]; std::snprintf(b, sizeof(b), "0x%04X vs 0x%04X", sprd_sum(pair), expected_swapped); return b; }());

    check("an empty buffer has a zero CRC", crc16_ccitt({}) == 0 && crc16_arc({}) == 0);
    check("the sum of nothing is the complement of zero",
          sprd_sum({}) == 0xFFFF, [&] { char b[16]; std::snprintf(b, sizeof(b), "0x%04X", sprd_sum({})); return b; }());
    check("sum32 adds the bytes", sum32(pair) == 10, std::to_string(sum32(pair)));

    // Resuming has to give the same answer as doing it in one go, or a large
    // PAC's payload CRC would depend on how it was chunked.
    std::vector<std::uint8_t> big(5000);
    for (std::size_t index = 0; index < big.size(); ++index) {
        big[index] = static_cast<std::uint8_t>(index * 7);
    }
    const std::uint16_t whole = crc16_arc(big);
    std::uint16_t piecewise = 0;
    for (std::size_t offset = 0; offset < big.size(); offset += 512) {
        const std::size_t count = std::min<std::size_t>(512, big.size() - offset);
        piecewise = crc16_arc_continue(piecewise, big.data() + offset, count);
    }
    check("a streamed CRC-16 equals the one-shot result", piecewise == whole);

    const std::uint32_t whole32 = crc32_ieee(big);
    std::uint32_t piecewise32 = 0xFFFFFFFFu;
    for (std::size_t offset = 0; offset < big.size(); offset += 512) {
        const std::size_t count = std::min<std::size_t>(512, big.size() - offset);
        piecewise32 = crc32_ieee_continue(piecewise32, big.data() + offset, count);
    }
    check("a streamed CRC-32 equals the one-shot result", (piecewise32 ^ 0xFFFFFFFFu) == whole32);
}

void test_pac_parsing() {
    std::printf("\n2. PAC container\n");

    const std::vector<std::uint8_t> data = build_pac(sample_entries());
    const PacFile pac = parse_pac(data);

    check("the magic is recognised", pac.header.magic == kPacMagic,
          [&] { char b[16]; std::snprintf(b, sizeof(b), "0x%08X", pac.header.magic); return b; }());
    check("the version string is read", pac.header.version_string == "BP_R1.0.0",
          pac.header.version_string);
    check("the product name is read", pac.header.product_name == "TEST_PRODUCT",
          pac.header.product_name);
    check("the product version is read", pac.header.product_version == "V1.2.3",
          pac.header.product_version);
    check("the product alias is read", pac.header.product_alias == "TEST_ALIAS");
    check("the declared size matches the file", pac.header.declared_size == data.size());
    check("the header is 2124 bytes", kPacHeaderSize == 2124, std::to_string(kPacHeaderSize));
    check("a file entry is 2580 bytes", kPacEntrySize == 2580, std::to_string(kPacEntrySize));

    check("every entry is read", pac.entries.size() == 4, std::to_string(pac.entries.size()));
    check("a package under 4 GiB reads as the 32-bit layout",
          pac.header.version == PacVersion::V1, to_string(pac.header.version));

    const PacEntry& fdl1 = pac.entries[0];
    check("the file id is read", fdl1.file_id == "HOST_FDL", fdl1.file_id);
    check("the file name is read", fdl1.file_name == "fdl1-sign.bin", fdl1.file_name);
    check("the load address is read", fdl1.address == 0x5500,
          [&] { char b[16]; std::snprintf(b, sizeof(b), "0x%llx", (unsigned long long)fdl1.address); return b; }());
    check("the payload size is read", fdl1.size == 64, std::to_string(fdl1.size));
    check("the payload offset points at the payload", fdl1.data_offset == kPacHeaderSize + 4 * kPacEntrySize,
          std::to_string(fdl1.data_offset));

    check("HOST_FDL is classified as FDL1", fdl1.role == PacEntryRole::Fdl1, to_string(fdl1.role));
    check("FDL2 is classified as FDL2", pac.entries[1].role == PacEntryRole::Fdl2,
          to_string(pac.entries[1].role));
    check("an FDL1 id is not swallowed by the FDL2 rule", fdl1.role != PacEntryRole::Fdl2);
    check("AP is classified as an image", pac.entries[2].role == PacEntryRole::Image,
          to_string(pac.entries[2].role));
    check("an entry with no payload is a marker", pac.entries[3].role == PacEntryRole::Marker,
          to_string(pac.entries[3].role));
    check("a marker reports itself as one", pac.entries[3].is_marker());

    check("both CRCs are checked and match", pac.crc_ok() && pac.payload_crc_checked);
    check("the header CRC is over the header up to the field itself",
          pac.header.header_crc == crc16_arc(data.data(), 2120));
    check("the payload CRC is over everything after the header",
          pac.header.payload_crc == crc16_arc(data.data() + 2124, data.size() - 2124));

    check("the first-stage loader is found", pac.fdl1() != nullptr
              && pac.fdl1()->file_id == "HOST_FDL");
    check("the second-stage loader is found", pac.fdl2() != nullptr
              && pac.fdl2()->file_id == "FDL2");
    check("three entries carry a payload", pac.downloads().size() == 3,
          std::to_string(pac.downloads().size()));
    check("the total payload is summed", pac.total_download_bytes() == 64 + 96 + 128,
          std::to_string(pac.total_download_bytes()));
    check("an entry is found by id", pac.find("ap") != nullptr);
    check("an unknown id finds nothing", pac.find("nope") == nullptr);

    const std::vector<std::uint8_t> payload = extract_entry(pac, data, fdl1);
    check("an entry's payload is extracted", payload.size() == 64 && payload[0] == 0x11,
          std::to_string(payload.size()));
    check("the payload is the one the package carries",
          std::all_of(payload.begin(), payload.end(), [](std::uint8_t b) { return b == 0x11; }));

    bool threw = false;
    try {
        extract_entry(pac, data, pac.entries[3]);
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("no payload") != std::string::npos;
    }
    check("extracting a marker is refused", threw);

    const PacLoaders loaders = extract_loaders(pac, data);
    check("both loaders come out", loaders.have_fdl1 && loaders.have_fdl2);
    check("the FDL1 address comes with it", loaders.fdl1_address == 0x5500);
    check("the FDL2 address comes with it", loaders.fdl2_address == 0x40004000,
          std::to_string(loaders.fdl2_address));
}

void test_pac_version_two() {
    std::printf("\n3. the 64-bit PAC layout\n");

    // The vendor header documents V1->V2 as the version field shrinking from 24
    // to 22 words and the two freed words becoming the high half of the size,
    // with the same happening inside an entry. Both headers are still 2124
    // bytes, which is why one parser reads both.
    const std::vector<std::uint8_t> data = build_pac(sample_entries(), true);
    const PacFile pac = parse_pac(data);

    check("the entries are read the same way", pac.entries.size() == 4,
          std::to_string(pac.entries.size()));
    check("a small file still reads as the 32-bit layout",
          pac.header.version == PacVersion::V1, to_string(pac.header.version));
    check("the version string is read from the shorter field",
          pac.header.version_string == "BP_R1.0.0", pac.header.version_string);
    check("the same offsets work for both layouts",
          pac.entries[0].file_id == "HOST_FDL" && pac.entries[0].size == 64);

    // A high half that is actually used is what distinguishes a V2 package.
    std::vector<std::uint8_t> big = data;
    // 5 GiB declared: the parser must recognise the version and then reject the
    // size, because the file is not that large. Reading only the low half would
    // accept a package that is not whole.
    put_u32(big, 44, 1);
    bool threw = false;
    try {
        parse_pac(big);
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("declares") != std::string::npos;
    }
    check("a high size half is honoured, and a mismatch is refused", threw);
}

void test_pac_rejections() {
    std::printf("\n4. damaged packages are refused\n");

    // The wrong magic: the common case is pointing at some other vendor's file.
    std::vector<std::uint8_t> wrong_magic = build_pac(sample_entries());
    put_u32(wrong_magic, 2116, 0x12345678);
    bool threw = false;
    try {
        parse_pac(wrong_magic);
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("not a PAC package") != std::string::npos;
    }
    check("a file without the magic is refused", threw);

    // A truncated download: the header still says how big it should be.
    std::vector<std::uint8_t> truncated = build_pac(sample_entries());
    truncated.resize(truncated.size() - 10);
    threw = false;
    try {
        parse_pac(truncated);
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("truncated") != std::string::npos
                || std::string(error.what()).find("declares") != std::string::npos;
    }
    check("a truncated file is refused, not half-read", threw);

    // A file too small to hold a header at all.
    threw = false;
    try {
        parse_pac(std::vector<std::uint8_t>(100, 0));
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("not a PAC package") != std::string::npos;
    }
    check("a file smaller than a header is refused", threw);

    // A corrupt entry count must not drive a huge allocation.
    std::vector<std::uint8_t> greedy = build_pac(sample_entries());
    put_u32(greedy, 1076, 0xFFFFFFFFu);
    threw = false;
    try {
        parse_pac(greedy);
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("more than") != std::string::npos;
    }
    check("an implausible entry count is refused before anything is reserved", threw);

    // An entry that points past the end of the file.
    std::vector<std::uint8_t> out_of_range = build_pac(sample_entries());
    put_u32(out_of_range, 2124 + 1540, 0x00FFFFFFu);  // AP's size
    threw = false;
    try {
        parse_pac(out_of_range);
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("outside") != std::string::npos
                || std::string(error.what()).find("damaged") != std::string::npos;
    }
    check("an entry pointing outside the file is refused", threw);

    // An entry whose own struct size is not the one this build reads.
    std::vector<std::uint8_t> wrong_size = build_pac(sample_entries());
    put_u32(wrong_size, 2124 + 0, 4096);
    threw = false;
    try {
        parse_pac(wrong_size);
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("layout") != std::string::npos;
    }
    check("an entry declaring an unknown layout is refused", threw);

    // A package with no FDL1 cannot be flashed at all, and saying so early is
    // the difference between a clear error and a device that will not come up.
    std::vector<TestEntry> no_loader = sample_entries();
    no_loader.erase(no_loader.begin());
    const std::vector<std::uint8_t> data = build_pac(no_loader);
    const PacFile pac = parse_pac(data);
    threw = false;
    try {
        extract_loaders(pac, data);
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("no FDL1") != std::string::npos;
    }
    check("a package with no FDL1 is refused", threw);

    // A wrong CRC is reported rather than thrown: the bytes are all there, and
    // the operator is the one who decides whether to trust them.
    std::vector<std::uint8_t> bad_crc = build_pac(sample_entries());
    bad_crc[bad_crc.size() - 1] ^= 0xFF;
    const PacFile degraded = parse_pac(bad_crc, true);
    check("a payload CRC mismatch is reported", degraded.payload_crc_checked
              && !degraded.payload_crc_ok);
    check("a package failing its CRC reports so", !degraded.crc_ok());
    check("the entry that was changed is still described",
          degraded.entries.size() == 4);
}

void test_pac_utf16() {
    std::printf("\n5. the text fields\n");

    // One character per 16-bit unit, NUL-terminated. A unit that is not ASCII
    // is replaced rather than encoded into something that would not round-trip.
    const std::uint8_t text[] = {'A', 0, 'B', 0, 0, 0, 'C', 0};
    check("a field stops at the terminator", utf16le_field(text, sizeof(text)) == "AB",
          utf16le_field(text, sizeof(text)));

    const std::uint8_t empty[] = {0, 0};
    check("an empty field is an empty string", utf16le_field(empty, sizeof(empty)).empty());

    const std::uint8_t non_ascii[] = {0x34, 0x12};
    check("a non-ASCII unit becomes a placeholder",
          utf16le_field(non_ascii, sizeof(non_ascii)) == "?",
          utf16le_field(non_ascii, sizeof(non_ascii)));
}

void test_framing() {
    std::printf("\n6. BSL framing\n");

    const std::vector<std::uint8_t> data = {0x01, 0x02, 0x03};
    const std::vector<std::uint8_t> frame =
        build_frame(static_cast<std::uint16_t>(BslCommand::Connect), data, ChecksumKind::Crc16Ccitt);

    check("a frame opens and closes with the flag",
          frame.front() == kBslFlag && frame.back() == kBslFlag, hexdump(frame));
    check("the body is header, data and checksum",
          frame.size() == 2 + 4 + data.size() + 2, std::to_string(frame.size()));

    // Escaping: the flags are 0x7E and the escape is 0x7D, so a body containing
    // either has to grow. This is the check that catches a frame that would
    // otherwise be truncated at the wrong place by the device.
    std::vector<std::uint8_t> hostile = {0x7E, 0x7D, 0x00, 0xFF};
    const std::vector<std::uint8_t> escaped = hdlc_escape(hostile);
    check("a flag inside a body is escaped",
          escaped.size() == hostile.size() + 2 && escaped[0] == 0x7D && escaped[1] == 0x5E,
          hexdump(escaped));
    check("an escape inside a body is escaped", escaped[2] == 0x7D && escaped[3] == 0x5D,
          hexdump(escaped));
    check("the rest of the body is untouched", escaped[4] == 0x00 && escaped[5] == 0xFF);

    const std::vector<std::uint8_t> round_tripped = hdlc_unescape(escaped);
    check("escaping round-trips", round_tripped == hostile, hexdump(round_tripped));

    // A frame with no escapable bytes must not grow.
    check("a clean body is not changed by escaping",
          hdlc_escape({0x01, 0x02}).size() == 2);

    // Decoding.
    const std::vector<std::uint8_t> body = [&] {
        std::vector<std::uint8_t> out;
        const std::uint16_t ack = static_cast<std::uint16_t>(BslResponse::Ack);
        out.push_back(static_cast<std::uint8_t>((ack >> 8) & 0xFF));
        out.push_back(static_cast<std::uint8_t>(ack & 0xFF));
        out.push_back(0);
        out.push_back(0);
        const std::uint16_t crc = crc16_ccitt(out.data(), out.size());
        out.push_back(static_cast<std::uint8_t>(crc >> 8));
        out.push_back(static_cast<std::uint8_t>(crc & 0xFF));
        return out;
    }();
    const BslFrame decoded = parse_frame(body);
    check("an ACK decodes", decoded.type == static_cast<std::uint16_t>(BslResponse::Ack));
    check("the checksum that matched is reported", decoded.checksum == ChecksumKind::Crc16Ccitt,
          to_string(decoded.checksum));

    // The same frame checked with the other algorithm must be recognised as the
    // other algorithm - that is what makes detection work at all.
    std::vector<std::uint8_t> sum_body;
    sum_body.push_back(0x00);
    sum_body.push_back(static_cast<std::uint8_t>(BslResponse::Version));
    sum_body.push_back(0);
    sum_body.push_back(0);
    const std::uint16_t sum = sprd_sum(sum_body.data(), sum_body.size());
    sum_body.push_back(static_cast<std::uint8_t>(sum >> 8));
    sum_body.push_back(static_cast<std::uint8_t>(sum & 0xFF));
    check("the Spreadtrum sum is detected on a frame that uses it",
          detect_checksum(sum_body) == ChecksumKind::SprdSum,
          to_string(detect_checksum(sum_body)));

    // A body that verifies with neither is not a BSL conversation.
    bool threw = false;
    try {
        parse_frame({0x00, 0x80, 0x00, 0x00, 0xAA, 0xBB});
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("neither") != std::string::npos;
    }
    check("a frame that matches no checksum is refused", threw);

    threw = false;
    try {
        parse_frame({0x00, 0x80});
    } catch (const ProtocolError&) {
        threw = true;
    }
    check("a body shorter than a header is refused", threw);

    // A declared length that runs past what arrived.
    std::vector<std::uint8_t> truncated = {0x00, 0x80, 0x00, 0x10, 0x01, 0x02};
    const std::uint16_t crc = crc16_ccitt(truncated.data(), truncated.size());
    truncated.push_back(static_cast<std::uint8_t>(crc >> 8));
    truncated.push_back(static_cast<std::uint8_t>(crc & 0xFF));
    threw = false;
    try {
        parse_frame(truncated);
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("bytes of data") != std::string::npos;
    }
    check("a frame whose length exceeds its bytes is refused", threw);
}

void test_handshake() {
    std::printf("\n7. the handshake\n");

    ScriptedTransport transport;
    transport.expect(device_frame(static_cast<std::uint16_t>(BslResponse::Version),
                                  {'S', 'P', 'R', 'D', '3'}, ChecksumKind::Crc16Ccitt));
    transport.expect(device_frame(static_cast<std::uint16_t>(BslResponse::Ack), {},
                                  ChecksumKind::Crc16Ccitt));

    BslSession session(transport, quiet());
    check("the checksum is unknown before anything is said",
          !session.checksum_known() && session.checksum() == ChecksumKind::Unknown);

    const std::string version = session.usb_hello();
    check("the version string comes back", version == "SPRD3", version);
    check("the reply settles which checksum is in use",
          session.checksum_known() && session.checksum() == ChecksumKind::Crc16Ccitt,
          to_string(session.checksum()));

    // The control transfer is not optional: without it the device never answers.
    check("the USB hello opened with a control transfer",
          transport.control_calls().size() == 1, std::to_string(transport.control_calls().size()));
    if (!transport.control_calls().empty()) {
        const auto& call = transport.control_calls().front();
        check("the control transfer is the one the device expects",
              call.request_type == 0x21 && call.request == 0 && call.value == 1 && call.index == 0,
              std::to_string(call.request_type) + "/" + std::to_string(call.value));
    }
    check("a lone flag was sent as the hello",
          transport.writes().size() == 1 && transport.writes()[0].size() == 1
              && transport.writes()[0][0] == kBslFlag,
          std::to_string(transport.writes().size()) + " writes");

    session.connect();
    check("connect() marks the session connected", session.connected());

    // A device that answers the hello with something else is not in this mode.
    ScriptedTransport wrong;
    wrong.expect(device_frame(static_cast<std::uint16_t>(BslResponse::Ack), {},
                              ChecksumKind::Crc16Ccitt));
    BslSession second(wrong, quiet());
    bool threw = false;
    try {
        second.usb_hello();
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("version string") != std::string::npos;
    }
    check("a device that does not answer with its version is refused", threw);

    // Connecting before the checksum is known cannot work, because every frame
    // the host sends depends on it.
    ScriptedTransport fresh;
    BslSession third(fresh, quiet());
    threw = false;
    try {
        third.connect();
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("checksum is not known") != std::string::npos;
    }
    check("connecting before the handshake is refused", threw);
}

void test_checksum_detection_from_device() {
    std::printf("\n8. a device using the other checksum\n");

    // The RDA8910/UIS8910 family checks its frames with the Spreadtrum sum. The
    // host has to notice rather than assume, because the two are not
    // interchangeable and a wrong one gets no reply at all.
    ScriptedTransport transport;
    transport.expect(device_frame(static_cast<std::uint16_t>(BslResponse::Version),
                                  {'R', 'D', 'A'}, ChecksumKind::SprdSum));
    transport.expect(device_frame(static_cast<std::uint16_t>(BslResponse::Ack), {},
                                  ChecksumKind::SprdSum));

    BslSession session(transport, quiet());
    session.usb_hello();
    check("the Spreadtrum sum is picked up from the reply",
          session.checksum() == ChecksumKind::SprdSum, to_string(session.checksum()));

    session.connect();
    check("the session then works with that checksum", session.connected());

    // And the frame the host sent for connect must verify with that algorithm
    // when the device checks it - which is the whole point of detecting.
    const std::vector<std::vector<std::uint8_t>>& writes = transport.writes();
    // The hello writes a single flag byte; connect writes a whole frame.
    check("both writes reached the device", writes.size() == 2, std::to_string(writes.size()));
    check("the hello was a single flag byte",
          !writes.empty() && writes[0].size() == 1 && writes[0][0] == kBslFlag);
    check("the second write was a whole frame",
          writes.size() > 1 && writes[1].size() > 4 && writes[1].front() == kBslFlag);
}

void test_payload_download() {
    std::printf("\n9. sending a loader\n");

    ScriptedTransport transport;
    const ChecksumKind kind = ChecksumKind::Crc16Ccitt;

    // A payload big enough for more than one chunk: 528 is the chunk size, so
    // 1200 bytes is three chunks.
    std::vector<std::uint8_t> payload(1200);
    for (std::size_t index = 0; index < payload.size(); ++index) {
        payload[index] = static_cast<std::uint8_t>(index & 0xFF);
    }

    transport.expect(device_frame(static_cast<std::uint16_t>(BslResponse::Version), {'S'}, kind));
    transport.expect(device_frame(static_cast<std::uint16_t>(BslResponse::Ack), {}, kind));
    // START_DATA, three MIDST_DATA chunks, END_DATA, EXEC_DATA.
    for (int index = 0; index < 6; ++index) {
        transport.expect(device_frame(static_cast<std::uint16_t>(BslResponse::Ack), {}, kind));
    }

    BslSession session(transport, quiet());
    session.usb_hello();
    session.connect();
    transport.clear_written();
    session.execute_payload(0x5500, payload, "FDL1");

    const std::vector<std::uint8_t>& written = transport.written();
    // Each frame is written with one transport call, so the recording is one
    // frame per entry: START_DATA, 3 chunks, END_DATA, EXEC.
    check("six frames reached the device", transport.writes().size() == 6,
          std::to_string(transport.writes().size()));

    // Decode each recorded frame back and check what it carries. This is the
    // check that the addresses and lengths are big-endian, which is the mistake
    // that produces plausible nonsense rather than an error.
    std::vector<std::vector<std::uint8_t>> bodies;
    for (const std::vector<std::uint8_t>& frame : transport.writes()) {
        check("a frame is flag-delimited", frame.size() > 2 && frame.front() == kBslFlag
                  && frame.back() == kBslFlag, hexdump(frame).substr(0, 24));
        const std::vector<std::uint8_t> body(frame.begin() + 1, frame.end() - 1);
        bodies.push_back(hdlc_unescape(body));
    }
    check("six frames were decoded", bodies.size() == 6, std::to_string(bodies.size()));

    const BslFrame start = parse_frame(bodies[0]);
    check("the transfer opens with START_DATA",
          start.type == static_cast<std::uint16_t>(BslCommand::StartData),
          describe_response(start.type));
    check("START_DATA carries the address and the length, big-endian",
          start.data.size() == 8 && start.data[0] == 0x00 && start.data[1] == 0x00
              && start.data[2] == 0x55 && start.data[3] == 0x00 && start.data[7] == 0xB0,
          hexdump(start.data));

    const BslFrame chunk_one = parse_frame(bodies[1]);
    check("the first chunk is MIDST_DATA",
          chunk_one.type == static_cast<std::uint16_t>(BslCommand::MidstData));
    check("the first chunk is the 528-byte maximum", chunk_one.data.size() == 528,
          std::to_string(chunk_one.data.size()));
    check("the chunk carries the payload from its start",
          chunk_one.data[0] == 0x00 && chunk_one.data[1] == 0x01);

    const BslFrame chunk_two = parse_frame(bodies[2]);
    check("the second chunk continues where the first stopped",
          chunk_two.data.size() == 528 && chunk_two.data[0] == 0x10,
          hexdump(std::vector<std::uint8_t>(chunk_two.data.begin(), chunk_two.data.begin() + 2)));

    const BslFrame chunk_three = parse_frame(bodies[3]);
    check("the last chunk is the remainder", chunk_three.data.size() == 1200 - 2 * 528,
          std::to_string(chunk_three.data.size()));

    check("the transfer closes with END_DATA",
          parse_frame(bodies[4]).type == static_cast<std::uint16_t>(BslCommand::EndData));
    check("the payload is run with EXEC_DATA",
          parse_frame(bodies[5]).type == static_cast<std::uint16_t>(BslCommand::ExecData));

    // Every recorded frame must verify with the algorithm the device uses, or
    // the device would have discarded it.
    bool all_verify = true;
    for (const std::vector<std::uint8_t>& body : bodies) {
        if (detect_checksum(body) != kind) {
            all_verify = false;
        }
    }
    check("every frame the host sent verifies with the device's checksum", all_verify);
}

void test_refusal_paths() {
    std::printf("\n10. refusals\n");

    const ChecksumKind kind = ChecksumKind::Crc16Ccitt;

    // A device that refuses an operation must stop it, not be retried into.
    ScriptedTransport transport;
    transport.expect(device_frame(static_cast<std::uint16_t>(BslResponse::Version), {'S'}, kind));
    transport.expect(device_frame(static_cast<std::uint16_t>(BslResponse::Ack), {}, kind));
    transport.expect(device_frame(static_cast<std::uint16_t>(BslResponse::OperationFailed), {}, kind));

    BslSession session(transport, quiet());
    session.usb_hello();
    session.connect();
    bool threw = false;
    try {
        session.execute_payload(0x5500, std::vector<std::uint8_t>(16, 0xAA), "FDL1");
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("operation failed") != std::string::npos;
    }
    check("a refused transfer is reported with the device's own words", threw);
    check("nothing was sent after the refusal", transport.writes().size() == 3,
          std::to_string(transport.writes().size()));

    // The chip-id mismatch is the one worth naming: it means the package is for
    // a different device, and continuing would write the wrong firmware.
    ScriptedTransport mismatch;
    mismatch.expect(device_frame(static_cast<std::uint16_t>(BslResponse::Version), {'S'}, kind));
    mismatch.expect(device_frame(static_cast<std::uint16_t>(BslResponse::Ack), {}, kind));
    mismatch.expect(device_frame(static_cast<std::uint16_t>(BslResponse::ChipIdNotMatch), {}, kind));
    BslSession second(mismatch, quiet());
    second.usb_hello();
    second.connect();
    threw = false;
    try {
        second.execute_payload(0x5500, std::vector<std::uint8_t>(16, 0xAA), "FDL1");
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("chip id does not match") != std::string::npos;
    }
    check("a chip id mismatch is reported as the wrong package", threw);

    // An empty payload is a host-side mistake and must not reach the device.
    ScriptedTransport empty;
    BslSession third(empty, quiet());
    third.set_checksum(kind);
    threw = false;
    try {
        third.execute_payload(0x5500, {}, "FDL1");
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("empty") != std::string::npos;
    }
    check("an empty payload is refused before anything is sent", threw);

    // Building a frame without a known checksum cannot work, and says why.
    threw = false;
    try {
        build_frame(0x00, {}, ChecksumKind::Unknown);
    } catch (const ProtocolError& error) {
        threw = std::string(error.what()).find("which checksum") != std::string::npos;
    }
    check("building a frame without a checksum is refused", threw);

    threw = false;
    try {
        third.erase_flash(0, 0);
    } catch (const ProtocolError&) {
        threw = true;
    }
    check("erasing nothing is refused", threw);

    threw = false;
    try {
        third.reset();
    } catch (const ProtocolError&) {
        threw = true;  // the scripted link runs out, which is the point
    }
    check("a command the device never answers times out rather than hanging", threw);
}

void test_response_names() {
    std::printf("\n11. response names\n");

    check("a known code is named",
          describe_response(0x80).find("ACK") != std::string::npos, describe_response(0x80));
    check("the chip mismatch is named",
          describe_response(0xA3).find("chip id") != std::string::npos,
          describe_response(0xA3));
    check("the write protection is named",
          describe_response(0xB3).find("write protected") != std::string::npos,
          describe_response(0xB3));
    check("an unknown code is reported as a number",
          describe_response(0x1234) == "unknown response 0x1234", describe_response(0x1234));

    ChipInfo info;
    info.boot_version = "SPRD3";
    info.have_chip_type = true;
    info.chip_type = 0x9832;
    check("the chip summary names what was found",
          info.describe().find("SPRD3") != std::string::npos
              && info.describe().find("0x00009832") != std::string::npos,
          info.describe());
    check("an empty report says so rather than looking blank",
          ChipInfo().describe() == "the device reported nothing about itself");
}

void run(const char* name, void (*test)()) {
    try {
        test();
    } catch (const std::exception& error) {
        check(std::string("'") + name + "' completed without an unexpected exception", false,
              error.what());
    }
}

// -----------------------------------------------------------------------------
//  read_pac_header: reading a listing without the payload
// -----------------------------------------------------------------------------

void test_read_pac_header() {
    std::printf("\nread_pac_header\n");

    // A PAC padded out to four megabytes. Reading that to display a table is
    // what this function exists to avoid, so the file is big enough for the
    // distinction to be real.
    std::vector<std::uint8_t> data = build_pac(sample_entries());
    const std::size_t real_size = data.size() + 4u * 1024u * 1024u;
    data.resize(real_size, 0xA5);
    // The header's declared size is the whole file, and the payload CRC covers
    // everything after the header - both have to be rewritten after padding, or
    // the file is a damaged one rather than a large one.
    put_u32(data, 48, static_cast<std::uint32_t>(real_size));
    put_u16(data, 2120, crc16_arc(data.data(), 2120));
    put_u16(data, 2122, crc16_arc(data.data() + kPacHeaderSize, real_size - kPacHeaderSize));

    const std::string path =
        (std::filesystem::temp_directory_path() / "huaxin-header-test.pac").string();
    write_file(path, data);

    const PacFile streamed = read_pac_header(path);
    const PacFile whole = parse_pac(data, false);

    check("the streamed read finds the same entries",
          streamed.entries.size() == whole.entries.size(),
          std::to_string(streamed.entries.size()) + " vs "
              + std::to_string(whole.entries.size()));
    check("the entry identifiers match",
          !streamed.entries.empty() && streamed.entries[0].file_id == whole.entries[0].file_id,
          streamed.entries.empty() ? "none" : streamed.entries[0].file_id);
    check("the entry sizes match",
          !streamed.entries.empty() && streamed.entries[0].size == whole.entries[0].size);
    check("the product name is read",
          streamed.header.product_name == whole.header.product_name,
          streamed.header.product_name);
    check("the version is read", streamed.header.version == whole.header.version);
    check("the real file size is reported, not the prefix's",
          streamed.file_size == real_size,
          std::to_string(streamed.file_size) + " vs " + std::to_string(real_size));
    check("the header CRC is still verified", streamed.header.header_crc_ok);
    // The payload CRC cannot be computed from a prefix. The field says so rather
    // than reporting a check that never happened.
    check("the payload CRC is reported as unchecked", !streamed.payload_crc_checked);
    check("the listing describes itself", !streamed.summary().empty(), streamed.summary());
    check("the download total survives a prefix read",
          streamed.total_download_bytes() == whole.total_download_bytes(),
          std::to_string(streamed.total_download_bytes()));

    // The checks that do not depend on the payload are all still on.
    std::vector<std::uint8_t> damaged = data;
    put_u32(damaged, 1076, 0xFFFF);  // more entries than the file can hold
    write_file(path, damaged);
    bool refused = false;
    try {
        read_pac_header(path);
    } catch (const ProtocolError& error) {
        refused = std::string(error.what()).find("runs past the end") != std::string::npos;
    }
    check("a header claiming more entries than the file holds is refused", refused);

    std::error_code ignored;
    std::filesystem::remove(path, ignored);
}


}  // namespace

int main() {
    std::printf("Unisoc / Spreadtrum native protocol tests\n");
    std::printf("=========================================\n");
    std::fflush(stdout);

    run("checksums", test_checksums);
    run("PAC parsing", test_pac_parsing);
    run("PAC version two", test_pac_version_two);
    run("PAC rejections", test_pac_rejections);
    run("PAC text fields", test_pac_utf16);
    run("BSL framing", test_framing);
    run("handshake", test_handshake);
    run("checksum detection", test_checksum_detection_from_device);
    run("payload download", test_payload_download);
    run("refusals", test_refusal_paths);
    run("response names", test_response_names);
    run("listing a PAC without its payload", test_read_pac_header);

    std::printf("\n%d/%d checks passed\n", g_checks - g_failures, g_checks);
    if (g_failures > 0) {
        std::printf("FAILED\n");
        return 1;
    }
    std::printf("Unisoc / Spreadtrum protocols OK.\n");
    return 0;
}
