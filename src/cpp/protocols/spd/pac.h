#pragma once

// =============================================================================
//  Spreadtrum / Unisoc PAC firmware container.
//
//  A `.pac` is a single file holding a header, a table of file entries, and the
//  payloads those entries point at. Research Download writes the payloads, in
//  entry order, to the addresses the entries name. Nothing is compressed: the
//  payloads are raw images, which is why a PAC is roughly the size of the flash
//  it writes.
//
//  PROVENANCE - every offset below was taken from the vendor's own header plus
//  three independent implementations, all of which agree byte for byte:
//
//    * Mani-Sadhasivam/unisoc-dloader, include/BinPack.h - the *vendor's* header
//      for the packet format. This is where PAC_MAGIC (0xFFFAFFFA), the
//      BIN_PACKET_HEADER_T and FILE_T layouts, and the documented V1 -> V2 field
//      changes come from. It is the authority the others were checked against.
//    * ajsb85/sprdflash-rs (MIT), sprdflash-core/src/pac.rs - names every field
//      offset explicitly: HEADER_SIZE 2124, FILE_HEADER_SIZE 2580, magic
//      0xFFFAFFFA, and the offsets used below.
//    * iscle/unpac (GPL-3.0), main.c, and affggh/unpac_py, unpac.py - the same
//      two structures again, in C and in Python.
//
//  THE TWO VERSIONS ARE BOTH 2124 BYTES. The vendor header annotates the
//  difference as "V1->V2":
//
//    * header  `WORD szVersion[24]` -> `[22]`. V2 spends the two words it freed
//      on `dwHiSize`, which sits at offset 44; V1's single `dwSize` is V2's
//      `dwLoSize` at offset 48. Everything from 52 onwards is identical.
//    * entries `WORD szFileVersion[256]` -> `[252]`. V2 spends the space on
//      `dwHiFileSize` (offset 1532) and `dwHiDataOffset` (1536), so a file can
//      be larger than 4 GiB. V1's own size and offset fields are V2's *low*
//      halves and sit at the same offsets - 1540 and 1552.
//
//  That is why one set of offsets parses both: the fields this parser reads are
//  at the same places in either version, and the only question is whether the
//  high halves are present. `PacVersion` reports which was found.
// =============================================================================

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#include "protocols/qualcomm/sahara.h"  // ProtocolError

namespace huaxin::protocols::spd {

/// The header magic, from the vendor's `PAC_MAGIC`. The value is the complement
/// of 0x00050005, which is where the "PAC 5" name in community use comes from.
inline constexpr std::uint32_t kPacMagic = 0xFFFAFFFA;
/// The fixed header's size, and where the file table starts when `dwFileOffset`
/// is taken at its word.
inline constexpr std::size_t kPacHeaderSize = 2124;
/// One file entry's size. Both versions are the same size - V2 only moved
/// fields around inside it.
inline constexpr std::size_t kPacEntrySize = 2580;

/// Which layout a file uses, as the vendor's header names them.
enum class PacVersion {
    Unknown,
    /// `szVersion[24]`, a single 32-bit size at offset 48. Files under 4 GiB.
    V1,
    /// `szVersion[22]`, a 64-bit size split across offsets 44 and 48, and
    /// 64-bit per-entry sizes and offsets. Needed above 4 GiB.
    V2,
};

const char* to_string(PacVersion version) noexcept;

/// What a file entry is for, derived from its `szFileID`.
enum class PacEntryRole {
    Unknown,
    /// The first-stage loader. `HOST_FDL`, `FDL` or `FDL1`.
    Fdl1,
    /// The second-stage loader, which has the storage commands. `FDL2`, or any
    /// id beginning with it.
    Fdl2,
    /// An operation with no payload: a format, an erase, or a phase marker.
    Marker,
    /// A partition image.
    Image,
};

const char* to_string(PacEntryRole role) noexcept;

/// The header's `dwFlashType`, as far as it is known.
///
/// The field is present in both versions but the vendor header does not say
/// what its values mean, so nothing here interprets it: the raw word is
/// reported and the interpretation is left alone.
struct PacHeaderInfo {
    PacVersion version{PacVersion::Unknown};
    /// The version string the header opens with, e.g. "BP_R1.0.0".
    std::string version_string;
    /// `szPrdName`: the product this package is for.
    std::string product_name;
    /// `szPrdVersion`.
    std::string product_version;
    /// `szPrdAlias`.
    std::string product_alias;
    /// The size the header claims, 64-bit when the version carries a high half.
    std::uint64_t declared_size{0};
    /// `dwMode`.
    std::uint32_t mode{0};
    /// `dwFlashType`. Raw: the value set is not documented in the sources.
    std::uint32_t flash_type{0};
    /// `dwNandStrategy`, `dwIsNvBackup`, `dwNandPageType` and the OMA-DM flags,
    /// kept raw for the same reason.
    std::uint32_t nand_strategy{0};
    std::uint32_t is_nv_backup{0};
    std::uint32_t nand_page_type{0};
    std::uint32_t is_preload{0};

    /// The magic as read, so a file that is not a PAC can be reported with the
    /// value it actually had rather than only "wrong".
    std::uint32_t magic{0};

    /// `wCRC1`: CRC-16/ARC over the header up to the field itself.
    std::uint16_t header_crc{0};
    /// `wCRC2`: CRC-16/ARC over everything after the fixed header.
    std::uint16_t payload_crc{0};
    /// Whether the header CRC actually matched.
    bool header_crc_ok{false};
};

/// One entry in the file table.
struct PacEntry {
    /// `szFileID`, e.g. HOST_FDL, FDL2, AP, NV, FMT_FSSYS.
    std::string file_id;
    /// `szFileName`, as the package names the source file. Often empty for a
    /// marker.
    std::string file_name;
    /// What the entry is for, derived from `file_id` and its size.
    PacEntryRole role{PacEntryRole::Unknown};

    /// Byte offset of the payload within the PAC file.
    std::uint64_t data_offset{0};
    /// Payload size in bytes. Zero means there is no payload - the entry is an
    /// operation rather than a file.
    std::uint64_t size{0};

    /// `nFileFlag`: 1 means the entry needs a file, 0 means it is an operation
    /// or a list of operations. The vendor header says exactly this.
    std::uint32_t flag{0};
    /// `nCheckFlag`: 1 means the file must be downloaded, 0 that it may not be.
    std::uint32_t check_flag{0};
    /// `dwCanOmitFlag`: 1 means a "download all" run may skip it.
    std::uint32_t can_omit{0};

    /// `dwAddr[0]`, the load or flash address. The other four slots are the
    /// remaining blocks of a multi-block entry and are kept in `addresses`.
    std::uint64_t address{0};
    /// Every `dwAddr` slot the entry states, in order.
    std::vector<std::uint64_t> addresses;

    /// True when the entry carries no payload: a format, an erase or a phase
    /// marker rather than an image.
    bool is_marker() const noexcept { return size == 0; }
    /// True when a "download all" run should write it.
    bool is_downloadable() const noexcept {
        return size != 0 && flag != 0 && check_flag != 0;
    }
    /// True when the address is one of the logical operation markers rather than
    /// a flash address. The sources put the boundary at 0x80000000; the vendor
    /// header does not name it, so the classification is reported as a hint and
    /// nothing acts on it.
    bool is_logical_marker() const noexcept { return size != 0 && address >= 0x80000000u; }

    std::string describe() const;
};

/// A parsed PAC.
struct PacFile {
    PacHeaderInfo header;
    std::vector<PacEntry> entries;
    /// The file's size on disk, which the header's own size field is checked
    /// against.
    std::uint64_t file_size{0};
    /// Whether the payload CRC was checked and matched. Empty when it was not
    /// checked.
    bool payload_crc_ok{false};
    bool payload_crc_checked{false};
    /// Every entry id the parser recognises, for the operator to read.
    std::vector<std::string> unknown_ids;

    /// The first-stage loader, or nullptr when the package has none.
    const PacEntry* fdl1() const;
    /// The second-stage loader, or nullptr. A package without one can still have
    /// its partitioned images written, but the storage commands come from FDL2.
    const PacEntry* fdl2() const;
    /// The entries that carry a payload and should be written, in file order.
    std::vector<PacEntry> downloads() const;
    /// Finds an entry by file id, case-insensitively.
    const PacEntry* find(const std::string& file_id) const;
    /// Total payload bytes a full run would write.
    std::uint64_t total_download_bytes() const;
    /// True when both CRCs that were checked matched.
    bool crc_ok() const noexcept { return header.header_crc_ok && (!payload_crc_checked || payload_crc_ok); }
    std::string summary() const;
};

/// Parses a PAC from memory.
///
/// `verify_payload` streams the payload CRC, which on a multi-gigabyte package
/// costs a full read. It should be left on for a flash: the check is the only
/// thing standing between a truncated download and a half-written device.
///
/// Throws ProtocolError on a wrong magic, a size that disagrees with the buffer,
/// a version it cannot read, or an entry that points outside the file.
PacFile parse_pac(const std::vector<std::uint8_t>& data, bool verify_payload = true);

/// Reads and parses a PAC from disk.
PacFile load_pac(const std::string& path, bool verify_payload = true);

/// Reads only the header and the entry table.
///
/// What a listing needs, and nothing else: a PAC is hundreds of megabytes to a
/// few gigabytes, and loading one to display its table would cost exactly that
/// much memory to answer a question about the first few kilobytes. The payload
/// CRC cannot be checked without reading the data, so `payload_crc_checked`
/// stays false - which is what the field is for.
PacFile read_pac_header(const std::string& path);

/// Extracts one entry's payload.
///
/// Throws ProtocolError when the entry is a marker or its range is outside the
/// file.
std::vector<std::uint8_t> extract_entry(const PacFile& pac, const std::vector<std::uint8_t>& data,
                                        const PacEntry& entry);

/// Extracts the FDL1 and FDL2 loaders a PAC carries.
struct PacLoaders {
    /// The loader that brings up USB and DRAM.
    std::vector<std::uint8_t> fdl1;
    std::uint64_t fdl1_address{0};
    bool have_fdl1{false};
    /// The loader that has the storage commands.
    std::vector<std::uint8_t> fdl2;
    std::uint64_t fdl2_address{0};
    bool have_fdl2{false};
};

/// Pulls both loaders out of a PAC. Throws when the package has no FDL1, which
/// is the signature of a package that cannot be flashed at all.
PacLoaders extract_loaders(const PacFile& pac, const std::vector<std::uint8_t>& data);

/// Decodes a fixed-width UTF-16LE field, stopping at the first NUL.
std::string utf16le_field(const std::uint8_t* data, std::size_t size);

}  // namespace huaxin::protocols::spd
