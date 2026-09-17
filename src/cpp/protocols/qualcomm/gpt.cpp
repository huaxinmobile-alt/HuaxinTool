#include "protocols/qualcomm/gpt.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <set>

#include "protocols/qualcomm/sahara.h"  // ProtocolError

namespace huaxin::protocols::qualcomm {

namespace {

// read_le32 / read_le64 come from sahara.h: the whole Qualcomm codec is
// little-endian, and the GPT is no exception, so there is no reason for a second
// copy of the same four lines.

GptGuid read_guid(const std::uint8_t* data) {
    GptGuid guid;
    guid.data1 = read_le32(data);
    guid.data2 = static_cast<std::uint16_t>(data[4] | (data[5] << 8));
    guid.data3 = static_cast<std::uint16_t>(data[6] | (data[7] << 8));
    std::memcpy(guid.data4, data + 8, 8);
    return guid;
}

/// The UEFI spec caps the table at 128 entries per LBA, and a real entry is
/// never smaller than 128 bytes. Anything outside that is a corrupt header, and
/// believing it would mean reading the wrong sectors.
constexpr std::uint32_t kMinEntrySize = 128;
constexpr std::uint32_t kMaxEntrySize = 4096;
/// 32 LBAs of 128 entries is far beyond any real table.
constexpr std::uint32_t kMaxEntries = 4096;

}  // namespace

bool GptGuid::is_zero() const noexcept {
    if (data1 != 0 || data2 != 0 || data3 != 0) {
        return false;
    }
    for (std::uint8_t byte : data4) {
        if (byte != 0) {
            return false;
        }
    }
    return true;
}

std::string GptGuid::to_string() const {
    char buffer[40];
    std::snprintf(buffer, sizeof(buffer), "%08x-%04x-%04x-%02x%02x-%02x%02x%02x%02x%02x%02x",
                  data1, data2, data3, data4[0], data4[1], data4[2], data4[3], data4[4], data4[5],
                  data4[6], data4[7]);
    return buffer;
}

std::string GptHeader::revision_string() const {
    // The high 16 bits are the major revision, the low 16 the minor.
    char buffer[16];
    std::snprintf(buffer, sizeof(buffer), "%u.%u", (revision >> 16) & 0xFFFF, revision & 0xFFFF);
    return buffer;
}

std::uint64_t GptEntry::sector_count() const noexcept {
    return last_lba >= first_lba ? last_lba - first_lba + 1 : 0;
}

std::uint64_t GptEntry::size_bytes(std::uint64_t sector_size) const noexcept {
    return sector_count() * sector_size;
}

std::string utf16le_to_utf8(const std::uint16_t* units, std::size_t count) {
    std::string text;
    for (std::size_t index = 0; index < count; ++index) {
        const std::uint32_t code = units[index];
        if (code == 0) {
            break;  // NUL-terminated
        }
        if (code < 0x80) {
            text.push_back(static_cast<char>(code));
        } else if (code < 0x800) {
            text.push_back(static_cast<char>(0xC0 | (code >> 6)));
            text.push_back(static_cast<char>(0x80 | (code & 0x3F)));
        } else {
            // Surrogate pairs are not combined: partition names are ASCII in
            // practice, and mangling a rare non-BMP name is better than
            // corrupting the common case.
            text.push_back(static_cast<char>(0xE0 | (code >> 12)));
            text.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
            text.push_back(static_cast<char>(0x80 | (code & 0x3F)));
        }
    }
    return text;
}

GptHeader parse_gpt_header(const std::uint8_t* data, std::size_t size) {
    if (data == nullptr || size < GptHeader::kSize) {
        throw ProtocolError("the GPT header needs " + std::to_string(GptHeader::kSize)
                            + " bytes, got " + std::to_string(size));
    }
    if (std::memcmp(data, GptHeader::kSignature, 8) != 0) {
        throw ProtocolError("this is not a GPT header: the first eight bytes are not \"EFI PART\"");
    }

    GptHeader header;
    header.revision = read_le32(data + 8);
    header.header_size = read_le32(data + 12);
    header.header_crc32 = read_le32(data + 16);
    header.current_lba = read_le64(data + 24);
    header.backup_lba = read_le64(data + 32);
    header.first_usable_lba = read_le64(data + 40);
    header.last_usable_lba = read_le64(data + 48);
    header.disk_guid = read_guid(data + 56);
    header.part_entry_lba = read_le64(data + 72);
    header.num_part_entries = read_le32(data + 80);
    header.part_entry_size = read_le32(data + 84);
    header.part_array_crc32 = read_le32(data + 88);

    if (header.header_size < GptHeader::kSize || header.header_size > 512) {
        throw ProtocolError("the GPT header claims a size of "
                            + std::to_string(header.header_size)
                            + " bytes, outside the 92 to 512 the spec allows");
    }
    if (header.part_entry_size < kMinEntrySize || header.part_entry_size > kMaxEntrySize
        || header.part_entry_size % 8 != 0) {
        throw ProtocolError("the GPT entry size is " + std::to_string(header.part_entry_size)
                            + " bytes, which is not a plausible partition entry size");
    }
    // The spec allows 128 entries per LBA; real tables hold 128, occasionally
    // 256. The cap rejects a header that would have the caller read gigabytes of
    // partition array, without second-guessing a legitimate table.
    if (header.num_part_entries == 0 || header.num_part_entries > kMaxEntries) {
        throw ProtocolError("the GPT claims " + std::to_string(header.num_part_entries)
                            + " partition entries, outside the 1 to "
                            + std::to_string(kMaxEntries) + " that is plausible");
    }
    return header;
}

std::vector<GptEntry> parse_gpt_entries(const std::uint8_t* data, std::size_t size,
                                        std::uint32_t count, std::uint32_t entry_size) {
    if (entry_size < kMinEntrySize) {
        throw ProtocolError("the GPT entry size (" + std::to_string(entry_size)
                            + ") is below the 128 bytes the spec requires");
    }
    std::vector<GptEntry> entries;
    if (count == 0) {
        return entries;
    }

    // The count comes from the device, so it is checked against the buffer
    // before anything is parsed: a header claiming a million entries must not
    // make us walk off the end.
    const std::size_t available = size / entry_size;
    const std::uint32_t usable = static_cast<std::uint32_t>(std::min<std::size_t>(count, available));
    if (usable < count) {
        throw ProtocolError("the GPT claims " + std::to_string(count) + " entries but only "
                            + std::to_string(available) + " fit in the "
                            + std::to_string(size) + " bytes provided");
    }

    entries.reserve(count);
    for (std::uint32_t index = 0; index < count; ++index) {
        const std::uint8_t* raw = data + static_cast<std::size_t>(index) * entry_size;
        GptEntry entry;
        entry.type_guid = read_guid(raw);
        entry.unique_guid = read_guid(raw + 16);
        entry.first_lba = read_le64(raw + 32);
        entry.last_lba = read_le64(raw + 40);
        entry.attributes = read_le64(raw + 48);

        std::uint16_t units[GptEntry::kNameUnits] = {};
        std::memcpy(units, raw + 56, sizeof(units));
        entry.name = utf16le_to_utf8(units, GptEntry::kNameUnits);

        entries.push_back(std::move(entry));
    }
    return entries;
}

GptTable parse_gpt(const std::uint8_t* header_data, std::size_t header_size,
                   const std::uint8_t* entries_data, std::size_t entries_size) {
    GptTable table;
    table.header = parse_gpt_header(header_data, header_size);
    table.entries = parse_gpt_entries(entries_data, entries_size, table.header.num_part_entries,
                                      table.header.part_entry_size);
    return table;
}

std::vector<GptEntry> GptTable::used_entries() const {
    std::vector<GptEntry> used;
    for (const GptEntry& entry : entries) {
        if (!entry.is_unused()) {
            used.push_back(entry);
        }
    }
    return used;
}

const GptEntry* GptTable::find(const std::string& name) const {
    for (const GptEntry& entry : entries) {
        if (!entry.is_unused() && entry.name == name) {
            return &entry;
        }
    }
    return nullptr;
}

std::uint64_t GptTable::total_bytes(std::uint64_t sector_size) const {
    std::uint64_t total = 0;
    for (const GptEntry& entry : used_entries()) {
        total += entry.size_bytes(sector_size);
    }
    return total;
}

}  // namespace huaxin::protocols::qualcomm
