#pragma once

// =============================================================================
//  Samsung firmware packages: the TAR and the .tar.md5 container.
//
//  An Odin package is a plain POSIX tar archive. A `.tar.md5` is that same
//  archive with the MD5 digest of its contents appended, which is where the
//  integrity of a flash actually lives - the Odin protocol itself has no packet
//  checksum at all.
//
//  PROVENANCE - the container layout was taken from two sources that agree:
//
//    * Marza4/rodin (Rust), src/odin.rs `flash_tar_md5` - the MD5 is the **last
//      sixteen bytes** of the file, raw, and is computed over everything before
//      them. Not thirty-two ASCII hex characters, which is the common
//      assumption and would make every verification fail.
//    * the POSIX ustar layout, which is a published standard rather than a
//      reverse-engineered vendor format.
//
//  The tar reader is deliberately small: it handles the ustar headers Samsung
//  packages carry (including the `prefix` field, so names longer than 100 bytes
//  work), regular files, and the GNU long-name extension. It does not follow
//  links, does not extract to disk by itself, and does not claim to be a general
//  tar implementation.
// =============================================================================

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#include "protocols/qualcomm/sahara.h"  // ProtocolError

namespace huaxin::protocols::samsung {

// --- MD5 ---------------------------------------------------------------------
/// The MD5 digest of a buffer, 16 bytes.
///
/// MD5 is not a secure hash and is not used as one here: it is a damage check
/// against a truncated download, which is what Samsung's packages carry and
/// nothing more. It is implemented rather than pulled in because a firmware
/// tool that cannot verify its own input without a third-party dependency is
/// harder to trust, not easier.
using Md5Digest = std::array<std::uint8_t, 16>;

Md5Digest md5(const std::uint8_t* data, std::size_t size);
Md5Digest md5(const std::vector<std::uint8_t>& data);
/// Lowercase hex, as these are usually written.
std::string md5_hex(const Md5Digest& digest);

// --- TAR ---------------------------------------------------------------------
/// One member of an archive.
struct TarEntry {
    /// The name as the archive stores it. `prefix` has already been joined on,
    /// so this is the full path.
    std::string name;
    /// Size in bytes of the member's data.
    std::uint64_t size{0};
    /// Offset of the member's data within the archive.
    std::uint64_t data_offset{0};
    /// The tar type flag: '0' or '\0' for a regular file, '5' for a directory.
    char type_flag{'0'};
    std::uint32_t mode{0};

    /// True for a regular file, which is the only kind Odin flashes.
    bool is_file() const noexcept { return type_flag == '0' || type_flag == '\0'; }
    /// The member's name without any directory part.
    std::string base_name() const;
};

/// A parsed archive.
struct TarArchive {
    std::vector<TarEntry> entries;

    /// True when the archive ends with the two zero blocks the format requires.
    /// A package that does not is one that was cut short, which is exactly the
    /// failure the MD5 is meant to catch and this is the cheaper second check.
    bool terminated{false};

    /// Finds a member by name, case-insensitively, matching either the full
    /// name or its base name. Packages are inconsistent about directories, and
    /// an operator who says "flash boot.img" means the member called boot.img.
    const TarEntry* find(const std::string& name) const;
    /// The regular files, in archive order. This is what Odin would flash.
    std::vector<TarEntry> files() const;
    std::uint64_t total_file_bytes() const;
};

/// Parses an archive from memory. Throws ProtocolError when the data is not a
/// tar at all or a member points outside it.
TarArchive parse_tar(const std::vector<std::uint8_t>& data);

/// Lists an archive by streaming its headers, without reading the member data.
///
/// A firmware package is two to six gigabytes and a listing needs the member
/// names, not their contents. Reading the whole file to answer a question about
/// its headers would cost gigabytes of memory, so this seeks past each member
/// instead. It stops at the ending zero blocks, or at the raw bytes of an
/// appended .md5 digest - which is not a tar header, and treating it as one
/// would report a good package as corrupt.
TarArchive list_tar_file(const std::string& path);

/// Reads one member's data.
std::vector<std::uint8_t> extract_entry(const std::vector<std::uint8_t>& data,
                                        const TarEntry& entry);

/// The result of checking a `.tar.md5`.
struct TarMd5Check {
    /// The digest stored in the file.
    Md5Digest stored{};
    /// The digest computed over the archive part.
    Md5Digest computed{};
    bool matched{false};
    /// How many bytes of the file made up the archive.
    std::uint64_t archive_size{0};
    /// How many bytes the file had in total. The difference is the digest.
    std::uint64_t file_size{0};
    /// True when the file was not a `.tar.md5` at all - no appended digest.
    bool has_appended_digest{true};

    std::string describe() const;
};

/// Splits a `.tar.md5` into its archive and its digest and checks them.
///
/// The digest is the last sixteen bytes of the file. A file whose length is not
/// archive-plus-sixteen is still checked against the last sixteen bytes; whether
/// that is right is `has_appended_digest`, which is false when the archive part
/// does not end with the two zero blocks a tar must.
TarMd5Check verify_tar_md5(const std::vector<std::uint8_t>& data);

}  // namespace huaxin::protocols::samsung
