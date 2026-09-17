#include "protocols/samsung/tar.h"

#include <algorithm>
#include <fstream>
#include <array>
#include <cctype>
#include <cstdio>
#include <cstring>

namespace huaxin::protocols::samsung {

using protocols::qualcomm::ProtocolError;

namespace {

// --- MD5, RFC 1321 -----------------------------------------------------------
constexpr std::array<std::uint32_t, 64> kMd5K = {
    0xd76aa478, 0xe8c7b756, 0x242070db, 0xc1bdceee, 0xf57c0faf, 0x4787c62a, 0xa8304613,
    0xfd469501, 0x698098d8, 0x8b44f7af, 0xffff5bb1, 0x895cd7be, 0x6b901122, 0xfd987193,
    0xa679438e, 0x49b40821, 0xf61e2562, 0xc040b340, 0x265e5a51, 0xe9b6c7aa, 0xd62f105d,
    0x02441453, 0xd8a1e681, 0xe7d3fbc8, 0x21e1cde6, 0xc33707d6, 0xf4d50d87, 0x455a14ed,
    0xa9e3e905, 0xfcefa3f8, 0x676f02d9, 0x8d2a4c8a, 0xfffa3942, 0x8771f681, 0x6d9d6122,
    0xfde5380c, 0xa4beea44, 0x4bdecfa9, 0xf6bb4b60, 0xbebfbc70, 0x289b7ec6, 0xeaa127fa,
    0xd4ef3085, 0x04881d05, 0xd9d4d039, 0xe6db99e5, 0x1fa27cf8, 0xc4ac5665, 0xf4292244,
    0x432aff97, 0xab9423a7, 0xfc93a039, 0x655b59c3, 0x8f0ccc92, 0xffeff47d, 0x85845dd1,
    0x6fa87e4f, 0xfe2ce6e0, 0xa3014314, 0x4e0811a1, 0xf7537e82, 0xbd3af235, 0x2ad7d2bb,
    0xeb86d391};

constexpr std::array<int, 64> kMd5Shift = {7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7,
                                           12, 17, 22, 5, 9,  14, 20, 5, 9,  14, 20, 5,  9,
                                           14, 20, 5, 9,  14, 20, 4, 11, 16, 23, 4,  11, 16,
                                           23, 4, 11, 16, 23, 4, 11, 16, 23, 6,  10, 15, 21,
                                           6, 10, 15, 21, 6, 10, 15, 21, 6,  10, 15, 21};

std::uint32_t rotl(std::uint32_t value, int bits) {
    return (value << bits) | (value >> (32 - bits));
}

void md5_block(const std::uint8_t* block, std::uint32_t state[4]) {
    std::uint32_t words[16];
    for (int index = 0; index < 16; ++index) {
        words[index] = static_cast<std::uint32_t>(block[index * 4])
                       | (static_cast<std::uint32_t>(block[index * 4 + 1]) << 8)
                       | (static_cast<std::uint32_t>(block[index * 4 + 2]) << 16)
                       | (static_cast<std::uint32_t>(block[index * 4 + 3]) << 24);
    }

    std::uint32_t a = state[0];
    std::uint32_t b = state[1];
    std::uint32_t c = state[2];
    std::uint32_t d = state[3];

    for (int index = 0; index < 64; ++index) {
        std::uint32_t f = 0;
        int g = 0;
        if (index < 16) {
            f = (b & c) | (~b & d);
            g = index;
        } else if (index < 32) {
            f = (d & b) | (~d & c);
            g = (5 * index + 1) % 16;
        } else if (index < 48) {
            f = b ^ c ^ d;
            g = (3 * index + 5) % 16;
        } else {
            f = c ^ (b | ~d);
            g = (7 * index) % 16;
        }
        f = f + a + kMd5K[static_cast<std::size_t>(index)] + words[g];
        a = d;
        d = c;
        c = b;
        b = b + rotl(f, kMd5Shift[static_cast<std::size_t>(index)]);
    }

    state[0] += a;
    state[1] += b;
    state[2] += c;
    state[3] += d;
}

/// Reads a tar numeric field: octal ASCII, optionally space- or NUL-terminated.
///
/// A GNU base-256 field (the top bit of the first byte set) is handled too: that
/// is how a tar stores a size above the octal field's limit, and treating one as
/// octal gives a wildly wrong number rather than an error.
bool parse_tar_number(const char* field, std::size_t size, std::uint64_t& out) {
    if (size == 0) {
        return false;
    }
    const unsigned char first = static_cast<unsigned char>(field[0]);
    if ((first & 0x80) != 0) {
        // GNU base-256, big-endian.
        std::uint64_t value = first & 0x7F;
        for (std::size_t index = 1; index < size; ++index) {
            value = (value << 8) | static_cast<unsigned char>(field[index]);
        }
        out = value;
        return true;
    }
    std::uint64_t value = 0;
    bool saw_digit = false;
    for (std::size_t index = 0; index < size; ++index) {
        const char character = field[index];
        if (character == '\0' || character == ' ') {
            if (saw_digit) {
                break;
            }
            continue;
        }
        if (character < '0' || character > '7') {
            return false;
        }
        value = value * 8 + static_cast<std::uint64_t>(character - '0');
        saw_digit = true;
    }
    out = value;
    return true;
}

/// A NUL-terminated field, trimmed.
std::string tar_string(const char* field, std::size_t size) {
    std::size_t length = 0;
    while (length < size && field[length] != '\0') {
        ++length;
    }
    return std::string(field, length);
}

std::uint64_t read_u64(const std::uint8_t* data) {
    std::uint64_t value = 0;
    for (int index = 7; index >= 0; --index) {
        value = (value << 8) | data[index];
    }
    return value;
}

}  // namespace

// --- MD5 ---------------------------------------------------------------------
Md5Digest md5(const std::uint8_t* data, std::size_t size) {
    std::uint32_t state[4] = {0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476};

    const std::size_t full_blocks = size / 64;
    for (std::size_t index = 0; index < full_blocks; ++index) {
        md5_block(data + index * 64, state);
    }

    // The tail, padded with 0x80, zeros, and the bit length little-endian.
    std::uint8_t tail[128] = {0};
    const std::size_t remainder = size - full_blocks * 64;
    std::memcpy(tail, data + full_blocks * 64, remainder);
    tail[remainder] = 0x80;
    const std::size_t tail_size = remainder < 56 ? 64 : 128;
    const std::uint64_t bits = static_cast<std::uint64_t>(size) * 8;
    for (int index = 0; index < 8; ++index) {
        tail[tail_size - 8 + static_cast<std::size_t>(index)] =
            static_cast<std::uint8_t>((bits >> (8 * index)) & 0xFF);
    }
    for (std::size_t index = 0; index < tail_size; index += 64) {
        md5_block(tail + index, state);
    }

    Md5Digest digest{};
    for (int index = 0; index < 4; ++index) {
        for (int byte = 0; byte < 4; ++byte) {
            digest[static_cast<std::size_t>(index * 4 + byte)] =
                static_cast<std::uint8_t>((state[index] >> (8 * byte)) & 0xFF);
        }
    }
    return digest;
}

Md5Digest md5(const std::vector<std::uint8_t>& data) {
    return md5(data.data(), data.size());
}

std::string md5_hex(const Md5Digest& digest) {
    static const char* digits = "0123456789abcdef";
    std::string out;
    out.reserve(32);
    for (const std::uint8_t byte : digest) {
        out.push_back(digits[(byte >> 4) & 0xF]);
        out.push_back(digits[byte & 0xF]);
    }
    return out;
}

// --- TAR ---------------------------------------------------------------------
std::string TarEntry::base_name() const {
    const std::size_t slash = name.find_last_of('/');
    return slash == std::string::npos ? name : name.substr(slash + 1);
}

const TarEntry* TarArchive::find(const std::string& name) const {
    // Exact first, then base name, then case-insensitively: an operator naming
    // "boot.img" means the member, whatever directory the package put it in and
    // whatever case it used.
    for (const TarEntry& entry : entries) {
        if (entry.is_file() && entry.name == name) {
            return &entry;
        }
    }
    auto lower = [](const std::string& text) {
        std::string out;
        out.reserve(text.size());
        for (const char character : text) {
            out.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(character))));
        }
        return out;
    };
    const std::string wanted = lower(name);
    for (const TarEntry& entry : entries) {
        if (entry.is_file() && lower(entry.base_name()) == wanted) {
            return &entry;
        }
    }
    for (const TarEntry& entry : entries) {
        if (entry.is_file() && lower(entry.name) == wanted) {
            return &entry;
        }
    }
    return nullptr;
}

std::vector<TarEntry> TarArchive::files() const {
    std::vector<TarEntry> out;
    for (const TarEntry& entry : entries) {
        if (entry.is_file()) {
            out.push_back(entry);
        }
    }
    return out;
}

std::uint64_t TarArchive::total_file_bytes() const {
    std::uint64_t total = 0;
    for (const TarEntry& entry : entries) {
        if (entry.is_file()) {
            total += entry.size;
        }
    }
    return total;
}

TarArchive parse_tar(const std::vector<std::uint8_t>& data) {
    TarArchive archive;
    std::size_t offset = 0;
    // A pending GNU long name, from a '././@LongLink' member, to apply to the
    // next entry.
    std::string pending_name;

    while (offset + 512 <= data.size()) {
        const std::uint8_t* header = data.data() + offset;

        // Two zero blocks end an archive.
        const bool all_zero =
            std::all_of(header, header + 512, [](std::uint8_t byte) { return byte == 0; });
        if (all_zero) {
            archive.terminated = true;
            break;
        }

        // The checksum is the sum of the header's bytes with the checksum field
        // read as spaces. It is worth checking: it is the only thing that says
        // this is a tar header rather than arbitrary bytes that happened to
        // align.
        std::uint64_t stored_checksum = 0;
        if (!parse_tar_number(reinterpret_cast<const char*>(header + 148), 8, stored_checksum)) {
            throw ProtocolError("this is not a tar archive: the header checksum field at offset "
                                + std::to_string(offset) + " is not a number");
        }
        std::uint32_t computed = 0;
        for (std::size_t index = 0; index < 512; ++index) {
            computed += (index >= 148 && index < 156) ? 0x20 : header[index];
        }
        if (computed != stored_checksum) {
            char message[192];
            std::snprintf(message, sizeof(message),
                          "the tar header at offset %llu fails its checksum (stored 0x%llx, "
                          "computed 0x%x): this file is damaged or is not a tar archive",
                          static_cast<unsigned long long>(offset),
                          static_cast<unsigned long long>(stored_checksum), computed);
            throw ProtocolError(message);
        }

        TarEntry entry;
        const std::string name = tar_string(reinterpret_cast<const char*>(header), 100);
        const std::string prefix = tar_string(reinterpret_cast<const char*>(header + 345), 155);
        entry.name = prefix.empty() ? name : prefix + "/" + name;
        entry.type_flag = static_cast<char>(header[156]);
        entry.mode = 0;
        std::uint64_t mode = 0;
        if (parse_tar_number(reinterpret_cast<const char*>(header + 100), 8, mode)) {
            entry.mode = static_cast<std::uint32_t>(mode);
        }

        std::uint64_t size = 0;
        if (!parse_tar_number(reinterpret_cast<const char*>(header + 124), 12, size)) {
            throw ProtocolError("the tar member '" + entry.name + "' has an unreadable size field");
        }
        entry.size = size;
        entry.data_offset = offset + 512;

        // The data must be there, or the archive was cut short.
        if (entry.data_offset + entry.size > data.size()) {
            throw ProtocolError(
                "the tar member '" + entry.name + "' claims " + std::to_string(entry.size)
                + " bytes at offset " + std::to_string(entry.data_offset)
                + ", past the end of a " + std::to_string(data.size())
                + " byte file. The package is truncated.");
        }

        // The GNU long-name extension: a member whose name is the real name of
        // the one after it.
        if (name == "././@LongLink") {
            pending_name.assign(reinterpret_cast<const char*>(data.data() + entry.data_offset),
                                static_cast<std::size_t>(entry.size));
            while (!pending_name.empty()
                   && (pending_name.back() == '\0' || pending_name.back() == '\n')) {
                pending_name.pop_back();
            }
            const std::uint64_t padded = (entry.size + 511) / 512 * 512;
            if (entry.data_offset + padded > data.size()) {
                throw ProtocolError("the tar long-name member runs past the end of the file");
            }
            offset = static_cast<std::size_t>(entry.data_offset + padded);
            continue;
        }
        if (!pending_name.empty()) {
            entry.name = pending_name;
            if (!prefix.empty()) {
                entry.name = prefix + "/" + pending_name;
            }
            pending_name.clear();
        }

        archive.entries.push_back(entry);

        const std::uint64_t padded = (entry.size + 511) / 512 * 512;
        offset = static_cast<std::size_t>(entry.data_offset + padded);
    }

    if (archive.entries.empty() && !archive.terminated) {
        throw ProtocolError(
            "no tar member was found. This file is not a tar archive, or it is empty; an Odin "
            "package is a tar file, usually with .tar or .tar.md5 appended.");
    }
    return archive;
}

std::vector<std::uint8_t> extract_entry(const std::vector<std::uint8_t>& data,
                                        const TarEntry& entry) {
    if (entry.data_offset + entry.size > data.size()) {
        throw ProtocolError("the tar member '" + entry.name + "' points outside the archive");
    }
    const std::uint8_t* begin = data.data() + entry.data_offset;
    return std::vector<std::uint8_t>(begin, begin + entry.size);
}

// --- .tar.md5 ----------------------------------------------------------------
std::string TarMd5Check::describe() const {
    char buffer[224];
    std::snprintf(buffer, sizeof(buffer),
                  "archive %llu bytes, digest %s, computed %s - %s",
                  static_cast<unsigned long long>(archive_size),
                  md5_hex(stored).c_str(), md5_hex(computed).c_str(),
                  matched ? "matches" : "DOES NOT MATCH");
    return buffer;
}

TarMd5Check verify_tar_md5(const std::vector<uint8_t>& data) {
    TarMd5Check result;
    result.file_size = data.size();

    // The digest is the last sixteen bytes. Everything before it is the archive.
    // Not thirty-two hex characters: a package built that way is a different
    // convention, and treating the ASCII as a digest would fail every check.
    constexpr std::size_t kDigestSize = 16;
    if (data.size() <= kDigestSize) {
        throw ProtocolError("a .tar.md5 has to be at least " + std::to_string(kDigestSize + 1)
                            + " bytes; this file is " + std::to_string(data.size()));
    }
    result.archive_size = data.size() - kDigestSize;
    std::copy(data.end() - static_cast<std::ptrdiff_t>(kDigestSize), data.end(),
              result.stored.begin());
    result.computed = md5(data.data(), static_cast<std::size_t>(result.archive_size));
    result.matched = result.stored == result.computed;

    // Whether the appended-digest reading is the right one. A tar ends with two
    // zero blocks, and if the archive part does not, then either the file has no
    // appended digest or it is damaged. Saying which is more useful than either
    // assuming or refusing.
    const std::size_t tail = static_cast<std::size_t>(result.archive_size);
    if (tail >= 1024) {
        const std::uint8_t* last = data.data() + tail - 1024;
        result.has_appended_digest =
            std::all_of(last, last + 1024, [](std::uint8_t byte) { return byte == 0; });
    } else {
        result.has_appended_digest = false;
    }
    return result;
}



/// Lists a tar archive by streaming its headers.
///
/// A firmware package is two to six gigabytes and a listing needs the member
/// names, not the member data. `parse_tar` takes the whole buffer, which for a
/// package that size means reading gigabytes of mostly-image data to answer a
/// question about a few kilobytes of headers - so this seeks past each member's
/// data instead, and never holds more than one 512-byte header at a time.
///
/// It stops at the two zero blocks that end an archive, or at the first byte of
/// the appended `.md5` digest when the archive carries one - the digest is raw
/// binary, not a tar header, and treating it as one would fail its checksum and
/// report a good package as corrupt.
TarArchive list_tar_file(const std::string& path) {
    std::ifstream file(path, std::ios::binary | std::ios::ate);
    if (!file) {
        throw ProtocolError("cannot open " + path);
    }
    const std::uint64_t total = static_cast<std::uint64_t>(file.tellg());

    TarArchive archive;
    std::uint64_t offset = 0;
    std::string pending_name;
    std::vector<std::uint8_t> header(512);

    while (offset + 512 <= total) {
        file.seekg(static_cast<std::streamoff>(offset), std::ios::beg);
        if (!file.read(reinterpret_cast<char*>(header.data()), 512)) {
            break;
        }
        const std::uint8_t* raw = header.data();

        const bool all_zero =
            std::all_of(raw, raw + 512, [](std::uint8_t byte) { return byte == 0; });
        if (all_zero) {
            archive.terminated = true;
            break;
        }

        std::uint64_t stored_checksum = 0;
        if (!parse_tar_number(reinterpret_cast<const char*>(raw + 148), 8, stored_checksum)) {
            // Not a header. On a .tar.md5 that is the appended digest, which is
            // the normal way for this loop to end.
            break;
        }
        std::uint32_t computed = 0;
        for (std::size_t index = 0; index < 512; ++index) {
            computed += (index >= 148 && index < 156) ? 0x20 : raw[index];
        }
        if (computed != stored_checksum) {
            break;  // the digest, or damage; either way there is nothing more to list
        }

        TarEntry entry;
        const std::string name = tar_string(reinterpret_cast<const char*>(raw), 100);
        const std::string prefix = tar_string(reinterpret_cast<const char*>(raw + 345), 155);
        entry.name = prefix.empty() ? name : prefix + "/" + name;
        entry.type_flag = static_cast<char>(raw[156]);
        entry.data_offset = offset + 512;

        std::uint64_t size = 0;
        if (!parse_tar_number(reinterpret_cast<const char*>(raw + 124), 12, size)) {
            throw ProtocolError("the tar member '" + entry.name + "' has an unreadable size field");
        }
        entry.size = size;

        std::uint64_t mode = 0;
        if (parse_tar_number(reinterpret_cast<const char*>(raw + 100), 8, mode)) {
            entry.mode = static_cast<std::uint32_t>(mode);
        }

        const std::uint64_t padded = (size + 511) / 512 * 512;
        if (entry.data_offset + padded > total) {
            throw ProtocolError("the tar member '" + entry.name + "' runs past the end of "
                                + path + ". The package is truncated.");
        }

        if (name == "././@LongLink") {
            pending_name.assign(static_cast<std::size_t>(size), '\0');
            file.seekg(static_cast<std::streamoff>(entry.data_offset), std::ios::beg);
            file.read(pending_name.data(), static_cast<std::streamsize>(size));
            while (!pending_name.empty()
                   && (pending_name.back() == '\0' || pending_name.back() == '\n')) {
                pending_name.pop_back();
            }
            offset = entry.data_offset + padded;
            continue;
        }
        if (!pending_name.empty()) {
            entry.name = prefix.empty() ? pending_name : prefix + "/" + pending_name;
            pending_name.clear();
        }

        archive.entries.push_back(entry);
        offset = entry.data_offset + padded;
    }

    if (archive.entries.empty() && !archive.terminated) {
        throw ProtocolError(
            "no tar member was found in " + path
            + ". This file is not a tar archive, or it is empty.");
    }
    return archive;
}

}  // namespace huaxin::protocols::samsung
