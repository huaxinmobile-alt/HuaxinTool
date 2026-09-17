#pragma once

// =============================================================================
//  GUID Partition Table parsing.
//
//  GPT is a public UEFI specification, and the structures here were transcribed
//  from Qualcomm's upstream EDL tool linux-msm/qdl (BSD-3-Clause), src/gpt.c,
//  which is itself a straightforward implementation of that spec. Unlike the
//  vendor protocols elsewhere in this project, nothing here is reverse
//  engineered: on a Qualcomm device the GPT is simply the first thing you read
//  to learn what the flash is laid out as.
//
//  All fields are LITTLE-endian, on the wire and on disk.
// =============================================================================

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace huaxin::protocols::qualcomm {

/// A GUID in its on-disk layout: 4 + 2 + 2 + 8 bytes, little-endian for the
/// first three fields, as the UEFI spec defines it.
struct GptGuid {
    std::uint32_t data1{0};
    std::uint16_t data2{0};
    std::uint16_t data3{0};
    std::uint8_t data4[8]{};

    /// True for the all-zero GUID, which marks an unused entry.
    bool is_zero() const noexcept;
    /// Canonical "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" text.
    std::string to_string() const;
};

/// The 92-byte GPT header that starts LBA 1 (LBA 0 holds the protective MBR).
struct GptHeader {
    static constexpr std::size_t kSize = 92;
    /// The eight bytes that identify a GPT header.
    static constexpr char kSignature[8] = {'E', 'F', 'I', ' ', 'P', 'A', 'R', 'T'};

    std::uint32_t revision{0};
    std::uint32_t header_size{0};
    std::uint32_t header_crc32{0};
    std::uint64_t current_lba{0};
    std::uint64_t backup_lba{0};
    std::uint64_t first_usable_lba{0};
    std::uint64_t last_usable_lba{0};
    GptGuid disk_guid;
    std::uint64_t part_entry_lba{0};
    std::uint32_t num_part_entries{0};
    std::uint32_t part_entry_size{0};
    std::uint32_t part_array_crc32{0};

    /// Revision as "1.0" style text.
    std::string revision_string() const;
};

/// One 128-byte partition entry.
struct GptEntry {
    static constexpr std::size_t kSize = 128;
    /// The name field is 36 UTF-16LE code units.
    static constexpr std::size_t kNameUnits = 36;

    GptGuid type_guid;
    GptGuid unique_guid;
    std::uint64_t first_lba{0};
    std::uint64_t last_lba{0};
    std::uint64_t attributes{0};
    std::string name;  // decoded from UTF-16LE

    /// Inclusive sector count. Zero for an entry that claims no space.
    std::uint64_t sector_count() const noexcept;
    /// Size in bytes for a given logical sector size.
    std::uint64_t size_bytes(std::uint64_t sector_size) const noexcept;
    /// True when the type GUID is all zero, i.e. the slot is unused.
    bool is_unused() const noexcept { return type_guid.is_zero(); }
};

/// A parsed table.
struct GptTable {
    GptHeader header;
    std::vector<GptEntry> entries;

    /// Only the entries that are actually in use.
    std::vector<GptEntry> used_entries() const;

    /// Finds a partition by name. Case-sensitive, as GPT names are.
    const GptEntry* find(const std::string& name) const;

    /// Total size of the used partitions, at the given sector size.
    std::uint64_t total_bytes(std::uint64_t sector_size) const;
};

/// Parses a GPT header from its 512-byte (or larger) sector image.
///
/// Throws ProtocolError when the signature is missing, the header size is
/// implausible, or the entry geometry does not fit the spec's limits - a
/// partition table that cannot be trusted must not be acted on.
GptHeader parse_gpt_header(const std::uint8_t* data, std::size_t size);

/// Parses a partition entry array. `count` entries of `entry_size` bytes each.
///
/// Entries whose type GUID is zero are returned as unused rather than dropped,
/// so the caller can report "N of M slots in use".
std::vector<GptEntry> parse_gpt_entries(const std::uint8_t* data, std::size_t size,
                                        std::uint32_t count, std::uint32_t entry_size);

/// Convenience: header plus entries from two buffers.
GptTable parse_gpt(const std::uint8_t* header_data, std::size_t header_size,
                   const std::uint8_t* entries_data, std::size_t entries_size);

/// Decodes a UTF-16LE string of `units` code units into UTF-8, stopping at the
/// first NUL. Exposed because GPT names and USB strings both need it.
std::string utf16le_to_utf8(const std::uint16_t* units, std::size_t count);

}  // namespace huaxin::protocols::qualcomm
