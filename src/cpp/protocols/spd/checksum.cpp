#include "protocols/spd/checksum.h"

#include <cstdio>

namespace huaxin::protocols::spd {

const char* to_string(ChecksumKind kind) noexcept {
    switch (kind) {
        case ChecksumKind::Crc16Ccitt: return "CRC-16/CCITT";
        case ChecksumKind::SprdSum:    return "Spreadtrum sum";
        case ChecksumKind::Unknown:    return "not yet determined";
    }
    return "not yet determined";
}

std::uint16_t crc16_ccitt(const std::uint8_t* data, std::size_t size) noexcept {
    std::uint16_t crc = 0;
    for (std::size_t index = 0; index < size; ++index) {
        crc ^= static_cast<std::uint16_t>(data[index]) << 8;
        for (int bit = 0; bit < 8; ++bit) {
            if ((crc & 0x8000) != 0) {
                crc = static_cast<std::uint16_t>((crc << 1) ^ 0x1021);
            } else {
                crc = static_cast<std::uint16_t>(crc << 1);
            }
        }
    }
    return crc;
}

std::uint16_t crc16_ccitt(const std::vector<std::uint8_t>& data) noexcept {
    return crc16_ccitt(data.data(), data.size());
}

std::uint16_t crc16_arc_continue(std::uint16_t crc, const std::uint8_t* data,
                                 std::size_t size) noexcept {
    for (std::size_t index = 0; index < size; ++index) {
        crc ^= data[index];
        for (int bit = 0; bit < 8; ++bit) {
            if ((crc & 1) != 0) {
                crc = static_cast<std::uint16_t>((crc >> 1) ^ 0xA001);
            } else {
                crc = static_cast<std::uint16_t>(crc >> 1);
            }
        }
    }
    return crc;
}

std::uint16_t crc16_arc(const std::uint8_t* data, std::size_t size) noexcept {
    return crc16_arc_continue(0, data, size);
}

std::uint16_t crc16_arc(const std::vector<std::uint8_t>& data) noexcept {
    return crc16_arc(data.data(), data.size());
}

std::uint16_t sprd_sum(const std::uint8_t* data, std::size_t size) noexcept {
    std::uint32_t total = 0;
    std::size_t index = 0;
    // 16-bit big-endian words, which is the order the wire uses. Summing
    // little-endian words and swapping the result at the end is the same
    // operation - both spellings appear in the sources this was taken from -
    // and the tests pin the two against each other.
    while (size - index >= 2) {
        total += (static_cast<std::uint32_t>(data[index]) << 8)
                 | static_cast<std::uint32_t>(data[index + 1]);
        index += 2;
    }
    if (index < size) {
        total += data[index];
    }
    total = (total >> 16) + (total & 0xFFFF);
    return static_cast<std::uint16_t>(~(total + (total >> 16)) & 0xFFFF);
}

std::uint16_t sprd_sum(const std::vector<std::uint8_t>& data) noexcept {
    return sprd_sum(data.data(), data.size());
}

std::uint32_t crc32_ieee_continue(std::uint32_t crc, const std::uint8_t* data,
                                  std::size_t size) noexcept {
    for (std::size_t index = 0; index < size; ++index) {
        crc ^= data[index];
        for (int bit = 0; bit < 8; ++bit) {
            if ((crc & 1) != 0) {
                crc = (crc >> 1) ^ 0xEDB88320u;
            } else {
                crc >>= 1;
            }
        }
    }
    return crc;
}

std::uint32_t crc32_ieee(const std::uint8_t* data, std::size_t size) noexcept {
    return crc32_ieee_continue(0xFFFFFFFFu, data, size) ^ 0xFFFFFFFFu;
}

std::uint32_t crc32_ieee(const std::vector<std::uint8_t>& data) noexcept {
    return crc32_ieee(data.data(), data.size());
}

std::string crc32_hex(const std::vector<std::uint8_t>& data) {
    char buffer[16];
    std::snprintf(buffer, sizeof(buffer), "%08X", crc32_ieee(data));
    return buffer;
}

std::uint32_t sum32(const std::uint8_t* data, std::size_t size) noexcept {
    std::uint32_t total = 0;
    for (std::size_t index = 0; index < size; ++index) {
        total += data[index];
    }
    return total;
}

std::uint32_t sum32(const std::vector<std::uint8_t>& data) noexcept {
    return sum32(data.data(), data.size());
}

std::uint16_t compute(ChecksumKind kind, const std::uint8_t* data, std::size_t size) {
    switch (kind) {
        case ChecksumKind::Crc16Ccitt:
            return crc16_ccitt(data, size);
        case ChecksumKind::SprdSum:
            return sprd_sum(data, size);
        case ChecksumKind::Unknown:
            break;
    }
    throw protocols::qualcomm::ProtocolError(
        "the checksum in use has not been determined yet; it is decided by the chip family, "
        "and guessing would make every frame the host sends fail verification");
}

}  // namespace huaxin::protocols::spd
