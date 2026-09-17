#include "protocols/spd/pac.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <fstream>
#include <set>

#include "protocols/spd/checksum.h"

namespace huaxin::protocols::spd {

using protocols::qualcomm::ProtocolError;

namespace {

// Header field offsets. Identical in V1 and V2 except where noted, which is the
// whole reason one parser reads both.
constexpr std::size_t kOffVersionString = 0;
constexpr std::size_t kVersionStringLengthV1 = 48;
constexpr std::size_t kVersionStringLengthV2 = 44;
constexpr std::size_t kOffSizeHigh = 44;   // V2 only
constexpr std::size_t kOffSizeLow = 48;    // V1's dwSize, V2's dwLoSize
constexpr std::size_t kOffProductName = 52;
constexpr std::size_t kProductNameLength = 512;
constexpr std::size_t kOffProductVersion = 564;
constexpr std::size_t kOffFileCount = 1076;
constexpr std::size_t kOffFileOffset = 1080;
constexpr std::size_t kOffMode = 1084;
constexpr std::size_t kOffFlashType = 1088;
constexpr std::size_t kOffNandStrategy = 1092;
constexpr std::size_t kOffIsNvBackup = 1096;
constexpr std::size_t kOffNandPageType = 1100;
constexpr std::size_t kOffProductAlias = 1104;
constexpr std::size_t kProductAliasLength = 200;
constexpr std::size_t kOffIsPreload = 1312;
constexpr std::size_t kOffMagic = 2116;
constexpr std::size_t kOffHeaderCrc = 2120;
constexpr std::size_t kOffPayloadCrc = 2122;

// File entry offsets. Same in both versions; V2 adds the two high halves that
// V1 kept as reserved space inside szFileVersion.
constexpr std::size_t kEntryOffStructSize = 0;
constexpr std::size_t kEntryOffFileId = 4;
constexpr std::size_t kEntryIdLength = 512;
constexpr std::size_t kEntryOffFileName = 516;
constexpr std::size_t kEntryNameLength = 512;
constexpr std::size_t kEntryOffFileVersion = 1028;
constexpr std::size_t kEntryFileVersionLengthV1 = 512;
constexpr std::size_t kEntryFileVersionLengthV2 = 504;
constexpr std::size_t kEntryOffSizeHigh = 1532;   // V2 only
constexpr std::size_t kEntryOffDataHigh = 1536;   // V2 only
constexpr std::size_t kEntryOffSizeLow = 1540;
constexpr std::size_t kEntryOffFlag = 1544;
constexpr std::size_t kEntryOffCheckFlag = 1548;
constexpr std::size_t kEntryOffDataLow = 1552;
constexpr std::size_t kEntryOffCanOmit = 1556;
constexpr std::size_t kEntryOffAddressCount = 1560;
constexpr std::size_t kEntryOffAddresses = 1564;
constexpr std::size_t kEntryMaxAddresses = 5;

std::uint16_t read_u16(const std::uint8_t* data) {
    return static_cast<std::uint16_t>(data[0] | (data[1] << 8));
}

std::uint32_t read_u32(const std::uint8_t* data) {
    return static_cast<std::uint32_t>(data[0]) | (static_cast<std::uint32_t>(data[1]) << 8)
           | (static_cast<std::uint32_t>(data[2]) << 16) | (static_cast<std::uint32_t>(data[3]) << 24);
}

/// Reads `size` bytes at `offset`, refusing to run off the end of `data`.
const std::uint8_t* slice(const std::vector<std::uint8_t>& data, std::size_t offset,
                          std::size_t size, const char* what) {
    if (offset > data.size() || data.size() - offset < size) {
        throw ProtocolError(std::string("a PAC ") + what + " at offset " + std::to_string(offset)
                            + " needs " + std::to_string(size) + " bytes but the file contains "
                            + std::to_string(data.size()));
    }
    return data.data() + offset;
}

std::string upper(const std::string& text) {
    std::string out;
    out.reserve(text.size());
    for (const char character : text) {
        out.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(character))));
    }
    return out;
}

bool starts_with(const std::string& text, const std::string& prefix) {
    return text.size() >= prefix.size() && text.compare(0, prefix.size(), prefix) == 0;
}

/// Classifies an entry from its file id.
///
/// The order matters and comes from the reference implementation: a marker wins
/// first (an id can be reused with no payload), then FDL2 before FDL1, because
/// every FDL2 id begins with "FDL" and would otherwise be swallowed by the FDL1
/// rule.
PacEntryRole classify(const std::string& file_id, std::size_t size) {
    if (size == 0) {
        return PacEntryRole::Marker;
    }
    const std::string id = upper(file_id);
    if (id.empty()) {
        return PacEntryRole::Unknown;
    }
    if (starts_with(id, "FDL2")) {
        return PacEntryRole::Fdl2;
    }
    if (id == "HOST_FDL" || id == "FDL" || id == "FDL1" || starts_with(id, "FDL1")) {
        return PacEntryRole::Fdl1;
    }
    // Known non-loader ids seen in packages. Anything else is still an image;
    // this list only exists so the UI can label the ones it knows.
    static const std::set<std::string> images = {
        "AP", "NV", "PREPACK", "FMT_FSSYS", "MODEM", "PACKET", "PWRON", "SML", "TEE",
        "RECOVERY", "SPL", "UBOOT", "BOOT", "SYSTEM", "USERDATA", "VENDOR", "PRODUCT",
    };
    if (images.find(id) != images.end() || starts_with(id, "FMT_")) {
        return PacEntryRole::Image;
    }
    return PacEntryRole::Unknown;
}

}  // namespace

const char* to_string(PacVersion version) noexcept {
    switch (version) {
        case PacVersion::V1:      return "V1 (32-bit sizes)";
        case PacVersion::V2:      return "V2 (64-bit sizes)";
        case PacVersion::Unknown: return "unrecognised";
    }
    return "unrecognised";
}

const char* to_string(PacEntryRole role) noexcept {
    switch (role) {
        case PacEntryRole::Fdl1:   return "FDL1";
        case PacEntryRole::Fdl2:   return "FDL2";
        case PacEntryRole::Marker: return "marker";
        case PacEntryRole::Image:  return "image";
        case PacEntryRole::Unknown:return "unknown";
    }
    return "unknown";
}

std::string utf16le_field(const std::uint8_t* data, std::size_t size) {
    std::string out;
    for (std::size_t index = 0; index + 1 < size; index += 2) {
        const std::uint16_t unit = read_u16(data + index);
        if (unit == 0) {
            break;
        }
        // The fields hold one character per 16-bit unit. A unit above 0x7F is
        // outside the ASCII a package actually uses, so it is replaced rather
        // than encoded into something that would not round-trip.
        out.push_back(unit < 0x80 ? static_cast<char>(unit) : '?');
    }
    return out;
}

std::string PacEntry::describe() const {
    char buffer[256];
    std::snprintf(buffer, sizeof(buffer), "%s [%s] at 0x%llx, %llu bytes, offset 0x%llx",
                  file_id.empty() ? "(unnamed)" : file_id.c_str(), to_string(role),
                  static_cast<unsigned long long>(address),
                  static_cast<unsigned long long>(size),
                  static_cast<unsigned long long>(data_offset));
    return buffer;
}

const PacEntry* PacFile::fdl1() const {
    for (const PacEntry& entry : entries) {
        if (entry.role == PacEntryRole::Fdl1) {
            return &entry;
        }
    }
    return nullptr;
}

const PacEntry* PacFile::fdl2() const {
    for (const PacEntry& entry : entries) {
        if (entry.role == PacEntryRole::Fdl2) {
            return &entry;
        }
    }
    return nullptr;
}

std::vector<PacEntry> PacFile::downloads() const {
    std::vector<PacEntry> out;
    for (const PacEntry& entry : entries) {
        if (entry.is_downloadable()) {
            out.push_back(entry);
        }
    }
    return out;
}

const PacEntry* PacFile::find(const std::string& file_id) const {
    const std::string wanted = upper(file_id);
    for (const PacEntry& entry : entries) {
        if (upper(entry.file_id) == wanted) {
            return &entry;
        }
    }
    return nullptr;
}

std::uint64_t PacFile::total_download_bytes() const {
    std::uint64_t total = 0;
    for (const PacEntry& entry : entries) {
        if (entry.is_downloadable()) {
            total += entry.size;
        }
    }
    return total;
}

std::string PacFile::summary() const {
    char buffer[320];
    std::snprintf(buffer, sizeof(buffer), "%s %s (%s), %zu entries, %llu bytes of payload",
                  header.product_name.empty() ? "unnamed product" : header.product_name.c_str(),
                  header.product_version.empty() ? "" : header.product_version.c_str(),
                  to_string(header.version), entries.size(),
                  static_cast<unsigned long long>(total_download_bytes()));
    return buffer;
}

namespace {

/// The body of parse_pac, with the two checks that assume the whole file is
/// present made switchable.
///
/// Splitting it this way rather than writing a second parser is deliberate: the
/// header and entry-table layout is the part that was hard to get right, and a
/// second implementation of it would drift from this one. Only the two checks
/// that cannot hold for a prefix - the declared size matching the buffer, and
/// each entry's data lying inside it - are conditional.
PacFile parse_pac_impl(const std::vector<std::uint8_t>& data,
                       bool verify_payload,
                       bool check_declared_size,
                       bool check_entry_bounds) {
    if (data.size() < kPacHeaderSize) {
        throw ProtocolError("a PAC header is " + std::to_string(kPacHeaderSize)
                            + " bytes; this file is " + std::to_string(data.size())
                            + ". It is not a PAC package.");
    }

    PacFile pac;
    pac.file_size = data.size();
    const std::uint8_t* header = data.data();

    const std::uint32_t magic = read_u32(slice(data, kOffMagic, 4, "magic"));
    pac.header.magic = magic;
    if (magic != kPacMagic) {
        char message[256];
        std::snprintf(message, sizeof(message),
                      "this is not a PAC package: the header carries 0x%08X where the PAC magic "
                      "0x%08X belongs. A package from another vendor, or a truncated download, "
                      "is the usual cause.",
                      magic, kPacMagic);
        throw ProtocolError(message);
    }

    // Which version this is. V2 moved two words out of the version string to
    // hold the high half of the size, so the discriminator is whether that word
    // is used. A V2 package smaller than 4 GiB has a zero there and is
    // byte-identical to V1 for every field this parser reads, so identifying it
    // as V1 costs nothing.
    const std::uint32_t size_high = read_u32(header + kOffSizeHigh);
    const std::uint32_t size_low = read_u32(header + kOffSizeLow);
    pac.header.version = size_high != 0 ? PacVersion::V2 : PacVersion::V1;
    pac.header.declared_size = (static_cast<std::uint64_t>(size_high) << 32) | size_low;
    pac.header.version_string =
        utf16le_field(header + kOffVersionString,
                      pac.header.version == PacVersion::V2 ? kVersionStringLengthV2
                                                           : kVersionStringLengthV1);

    // The size field is the whole file's size, and a disagreement means the
    // download was truncated or the file is not what it claims.
    if (check_declared_size && pac.header.declared_size != data.size()) {
        throw ProtocolError(
            "the PAC header declares " + std::to_string(pac.header.declared_size)
            + " bytes but the file is " + std::to_string(data.size())
            + ". A truncated download is the usual cause; re-copy the package rather than "
              "flashing it.");
    }

    pac.header.product_name = utf16le_field(header + kOffProductName, kProductNameLength);
    pac.header.product_version =
        utf16le_field(header + kOffProductVersion, kProductNameLength);
    pac.header.product_alias =
        utf16le_field(header + kOffProductAlias, kProductAliasLength);
    pac.header.mode = read_u32(header + kOffMode);
    pac.header.flash_type = read_u32(header + kOffFlashType);
    pac.header.nand_strategy = read_u32(header + kOffNandStrategy);
    pac.header.is_nv_backup = read_u32(header + kOffIsNvBackup);
    pac.header.nand_page_type = read_u32(header + kOffNandPageType);
    pac.header.is_preload = read_u32(header + kOffIsPreload);

    pac.header.header_crc = read_u16(header + kOffHeaderCrc);
    pac.header.payload_crc = read_u16(header + kOffPayloadCrc);
    pac.header.header_crc_ok =
        crc16_arc(header, kOffHeaderCrc) == pac.header.header_crc;

    const std::uint32_t file_count = read_u32(header + kOffFileCount);
    const std::uint32_t file_offset = read_u32(header + kOffFileOffset);

    // A corrupt count must not turn into a multi-gigabyte reservation. The most
    // entries that could physically fit is the honest ceiling.
    const std::size_t max_entries = data.size() / kPacEntrySize + 1;
    if (file_count > max_entries) {
        throw ProtocolError("the PAC header claims " + std::to_string(file_count)
                            + " file entries, more than the " + std::to_string(max_entries)
                            + " this file has room for. The header is damaged.");
    }

    for (std::uint32_t index = 0; index < file_count; ++index) {
        const std::uint64_t base = static_cast<std::uint64_t>(file_offset)
                                   + static_cast<std::uint64_t>(index) * kPacEntrySize;
        if (base + kPacEntrySize > data.size()) {
            throw ProtocolError("PAC file entry " + std::to_string(index) + " at offset "
                                + std::to_string(base) + " runs past the end of the file");
        }
        const std::uint8_t* entry_data = data.data() + base;

        // The struct declares its own size and it has to be the one this parser
        // knows, or the offsets are meaningless.
        const std::uint32_t struct_size = read_u32(entry_data + kEntryOffStructSize);
        if (struct_size != kPacEntrySize) {
            throw ProtocolError("PAC file entry " + std::to_string(index) + " declares a size of "
                                + std::to_string(struct_size) + " bytes; this build reads "
                                + std::to_string(kPacEntrySize)
                                + ". The package uses a layout that is not supported.");
        }

        PacEntry entry;
        entry.file_id = utf16le_field(entry_data + kEntryOffFileId, kEntryIdLength);
        entry.file_name = utf16le_field(entry_data + kEntryOffFileName, kEntryNameLength);

        const std::uint32_t size_hi = read_u32(entry_data + kEntryOffSizeHigh);
        const std::uint32_t offset_hi = read_u32(entry_data + kEntryOffDataHigh);
        entry.size = (static_cast<std::uint64_t>(size_hi) << 32)
                     | read_u32(entry_data + kEntryOffSizeLow);
        entry.data_offset = (static_cast<std::uint64_t>(offset_hi) << 32)
                            | read_u32(entry_data + kEntryOffDataLow);

        entry.flag = read_u32(entry_data + kEntryOffFlag);
        entry.check_flag = read_u32(entry_data + kEntryOffCheckFlag);
        entry.can_omit = read_u32(entry_data + kEntryOffCanOmit);

        const std::uint32_t address_count =
            std::min(read_u32(entry_data + kEntryOffAddressCount),
                     static_cast<std::uint32_t>(kEntryMaxAddresses));
        for (std::uint32_t slot = 0; slot < address_count; ++slot) {
            entry.addresses.push_back(
                read_u32(entry_data + kEntryOffAddresses + slot * 4));
        }
        entry.address = entry.addresses.empty() ? 0 : entry.addresses.front();

        entry.role = classify(entry.file_id, static_cast<std::size_t>(entry.size));

        // An entry that carries data has to point at data that is there. This is
        // the check that catches a package assembled wrongly, before any of it
        // reaches a device.
        if (entry.size != 0 && check_entry_bounds) {
            if (entry.data_offset > data.size()
                || data.size() - entry.data_offset < entry.size) {
                throw ProtocolError(
                    "PAC entry '" + entry.file_id + "' claims " + std::to_string(entry.size)
                    + " bytes at offset " + std::to_string(entry.data_offset)
                    + ", which runs past the end of a " + std::to_string(data.size())
                    + " byte file. The package is damaged; nothing should be written from it.");
            }
        }
        if (entry.role == PacEntryRole::Unknown && !entry.file_id.empty()) {
            pac.unknown_ids.push_back(entry.file_id);
        }
        pac.entries.push_back(std::move(entry));
    }

    if (verify_payload) {
        // Streamed, so a multi-gigabyte package does not need a second copy in
        // memory to be checked.
        const std::uint16_t computed =
            crc16_arc(data.data() + kPacHeaderSize, data.size() - kPacHeaderSize);
        pac.payload_crc_checked = true;
        pac.payload_crc_ok = computed == pac.header.payload_crc;
    }
    return pac;
}

}  // namespace

PacFile parse_pac(const std::vector<std::uint8_t>& data, bool verify_payload) {
    return parse_pac_impl(data, verify_payload, true, true);
}

PacFile read_pac_header(const std::string& path) {
    std::ifstream file(path, std::ios::binary | std::ios::ate);
    if (!file) {
        throw ProtocolError("cannot open the PAC " + path);
    }
    const std::streamoff total = file.tellg();
    if (total < static_cast<std::streamoff>(kPacHeaderSize)) {
        throw ProtocolError(path + " is " + std::to_string(total)
                            + " bytes, shorter than a PAC header. It is not a PAC package.");
    }

    // The first read is just the header, because it is what says how long the
    // entry table is. Reading the whole file to find that out would mean a
    // two-gigabyte package costing two gigabytes of memory to display a list.
    std::vector<std::uint8_t> header(kPacHeaderSize);
    file.seekg(0, std::ios::beg);
    if (!file.read(reinterpret_cast<char*>(header.data()),
                   static_cast<std::streamsize>(kPacHeaderSize))) {
        throw ProtocolError("could not read the header of " + path);
    }

    const std::uint32_t file_count = read_u32(header.data() + kOffFileCount);
    const std::uint32_t file_offset = read_u32(header.data() + kOffFileOffset);

    // Bound the table before allocating for it. The count comes off the wire, so
    // a damaged header must not turn into a multi-gigabyte reservation - the
    // same rule parse_pac applies, checked here against the real file size.
    const std::uint64_t table_bytes =
        static_cast<std::uint64_t>(file_count) * kPacEntrySize;
    const std::uint64_t table_end =
        static_cast<std::uint64_t>(file_offset) + table_bytes;
    if (file_offset < kPacHeaderSize || table_end > static_cast<std::uint64_t>(total)) {
        throw ProtocolError(
            "the PAC header claims " + std::to_string(file_count) + " entries at offset "
            + std::to_string(file_offset) + ", which runs past the end of a "
            + std::to_string(total) + " byte file. The header is damaged.");
    }

    std::vector<std::uint8_t> prefix(static_cast<std::size_t>(table_end));
    file.seekg(0, std::ios::beg);
    if (!file.read(reinterpret_cast<char*>(prefix.data()),
                   static_cast<std::streamsize>(prefix.size()))) {
        throw ProtocolError("could not read the header and entry table of " + path);
    }

    PacFile pac = parse_pac_impl(prefix, /*verify_payload=*/false,
                                 /*check_declared_size=*/false, /*check_entry_bounds=*/false);
    // The size that matters is the file's, not the prefix's.
    pac.file_size = static_cast<std::uint64_t>(total);
    return pac;
}

PacFile load_pac(const std::string& path, bool verify_payload) {
    std::ifstream file(path, std::ios::binary | std::ios::ate);
    if (!file) {
        throw ProtocolError("cannot open the PAC " + path);
    }
    const std::streamoff size = file.tellg();
    if (size <= 0) {
        throw ProtocolError(path + " is empty");
    }
    file.seekg(0, std::ios::beg);
    std::vector<std::uint8_t> data(static_cast<std::size_t>(size));
    if (!file.read(reinterpret_cast<char*>(data.data()), size)) {
        throw ProtocolError("could not read all of " + path);
    }
    PacFile pac = parse_pac(data, verify_payload);
    return pac;
}

std::vector<std::uint8_t> extract_entry(const PacFile& pac,
                                        const std::vector<std::uint8_t>& data,
                                        const PacEntry& entry) {
    (void)pac;
    if (entry.size == 0) {
        throw ProtocolError("'" + entry.file_id
                            + "' carries no payload: it is an operation, not a file");
    }
    if (entry.data_offset > data.size() || data.size() - entry.data_offset < entry.size) {
        throw ProtocolError("'" + entry.file_id + "' points outside the file it was read from");
    }
    const std::uint8_t* begin = data.data() + entry.data_offset;
    return std::vector<std::uint8_t>(begin, begin + entry.size);
}

PacLoaders extract_loaders(const PacFile& pac, const std::vector<std::uint8_t>& data) {
    PacLoaders loaders;

    const PacEntry* fdl1 = pac.fdl1();
    if (fdl1 == nullptr) {
        throw ProtocolError(
            "this package carries no FDL1 loader. Research Download needs one: it is what "
            "brings up the USB link and DRAM before anything else can be sent.");
    }
    loaders.fdl1 = extract_entry(pac, data, *fdl1);
    loaders.fdl1_address = fdl1->address;
    loaders.have_fdl1 = true;

    if (const PacEntry* fdl2 = pac.fdl2()) {
        loaders.fdl2 = extract_entry(pac, data, *fdl2);
        loaders.fdl2_address = fdl2->address;
        loaders.have_fdl2 = true;
    }
    return loaders;
}

}  // namespace huaxin::protocols::spd
