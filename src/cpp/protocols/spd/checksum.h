#pragma once

// =============================================================================
//  Spreadtrum / Unisoc checksums.
//
//  There is no single checksum in this protocol family. Four different ones are
//  in use, on different things, and two of them are easily confused because both
//  are called "CRC-16":
//
//    | Function        | Algorithm                        | Used for                    |
//    | --------------- | -------------------------------- | --------------------------- |
//    | crc16_ccitt     | poly 0x1021, MSB-first, init 0   | classic BootROM BSL frame   |
//    | crc16_arc       | poly 0xA001, reflected, init 0   | PAC header and payload, NV  |
//    | sprd_sum        | ones-complement 16-bit word sum  | RDA8910/UIS8910 BSL frame   |
//    | crc32_ieee      | poly 0xEDB88320, reflected       | NV and NOR images           |
//
//  PROVENANCE - the algorithms were taken from four independent sources that
//  agree on every check value:
//
//    * ajsb85/sprdflash-rs (MIT), crates/sprdflash-core/src/checksum.rs - names
//      the polynomials, the widths and the check values, and documents which one
//      belongs to which protocol.
//    * iscle/sprdclient (GPL-3.0), main.c - sprd_brom_crc and sprd_fdl_crc as
//      they appear in a working client.
//    * affggh/unpac_py, unpac.py - the PAC's CRC-16-ARC.
//    * Mani-Sadhasivam/unisoc-dloader, include/BinPack.h - the vendor's own
//      header, which is where the PAC magic and the V1/V2 split come from.
//
//  The check values below are asserted in the tests, so a mistake in any of
//  these is caught without a device.
// =============================================================================

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#include "protocols/qualcomm/sahara.h"  // ProtocolError

namespace huaxin::protocols::spd {

/// Which checksum a BSL session is using.
///
/// The two are not interchangeable and the choice is the chip family's, not the
/// host's: the classic BootROM (SC65xx, SC77xx, SC98xx and most others) uses
/// CRC-16-CCITT, while the RDA8910/UIS8910 family uses the Spreadtrum sum. A
/// host that assumes one gets no reply at all, which is why this tool detects
/// the check from the device's first frame rather than assuming.
enum class ChecksumKind {
    Unknown,
    /// CRC-16/CCITT, poly 0x1021, MSB-first, init 0. The classic BootROM.
    Crc16Ccitt,
    /// The Spreadtrum ones-complement word sum. RDA8910 and UIS8910 agents.
    SprdSum,
};

const char* to_string(ChecksumKind kind) noexcept;

// --- CRC-16 ----------------------------------------------------------------
/// CRC-16 with polynomial 0x1021, MSB-first, initial value 0.
///
/// The sources call this "CRC-16", and it is what a classic Spreadtrum BootROM
/// checks a BSL frame with. The initial value matters: with 0 this is the variant
/// usually catalogued as CRC-16/XMODEM (check value 0x31C3 for "123456789"),
/// while the one usually called CRC-16/CCITT-FALSE initialises to 0xFFFF and
/// gives 0x29B1. The protocol uses init 0, which is asserted below.
std::uint16_t crc16_ccitt(const std::uint8_t* data, std::size_t size) noexcept;
std::uint16_t crc16_ccitt(const std::vector<std::uint8_t>& data) noexcept;

/// CRC-16/ARC: polynomial 0xA001 (the reflection of 0x8005), reflected,
/// initial value 0, no final xor.
///
/// This is what protects a PAC's header and payload, and what an NV image
/// carries in its first two bytes. Check value: crc16_arc("123456789") == 0xBB3D.
std::uint16_t crc16_arc(const std::uint8_t* data, std::size_t size) noexcept;
std::uint16_t crc16_arc(const std::vector<std::uint8_t>& data) noexcept;
/// Resumes a CRC-16/ARC over a further chunk, so a large PAC payload can be
/// checked without holding a second copy of it.
std::uint16_t crc16_arc_continue(std::uint16_t crc, const std::uint8_t* data,
                                 std::size_t size) noexcept;

// --- the Spreadtrum sum ------------------------------------------------------
/// The Spreadtrum ones-complement checksum.
///
/// Sums the buffer as 16-bit big-endian words (a trailing odd byte is added on
/// its own), folds the carry twice and complements. The RDA8910 and UIS8910 boot
/// ROMs and agents put this where the classic ones put a CRC-16-CCITT.
///
/// The two reference implementations spell this differently - one sums
/// little-endian words and swaps the result, the other sums big-endian words and
/// does not - and they are the same function. Swapping *and* summing big-endian
/// words, which is the obvious way to misread the pair, double-swaps and gives a
/// different answer on every input. The tests pin this against the independent
/// little-endian spelling.
std::uint16_t sprd_sum(const std::uint8_t* data, std::size_t size) noexcept;
std::uint16_t sprd_sum(const std::vector<std::uint8_t>& data) noexcept;

// --- CRC-32 ------------------------------------------------------------------
/// CRC-32 as IEEE 802.3 defines it: reflected polynomial 0xEDB88320, initial
/// value 0xFFFFFFFF, final xor 0xFFFFFFFF.
///
/// Not used by the BSL frames - those are 16-bit. It is the checksum an NV or
/// NOR image carries, and the one a firmware package's checksum file lists.
/// Check value: crc32_ieee("123456789") == 0xCBF43926.
std::uint32_t crc32_ieee(const std::uint8_t* data, std::size_t size) noexcept;
std::uint32_t crc32_ieee(const std::vector<std::uint8_t>& data) noexcept;
/// Resumes a CRC-32 over a further chunk.
std::uint32_t crc32_ieee_continue(std::uint32_t crc, const std::uint8_t* data,
                                  std::size_t size) noexcept;
/// CRC-32 as the eight uppercase hex digits a checksum file lists.
std::string crc32_hex(const std::vector<std::uint8_t>& data);

// --- the plain word sum ------------------------------------------------------
/// The 32-bit sum an NV region's START_DATA carries.
std::uint32_t sum32(const std::uint8_t* data, std::size_t size) noexcept;
std::uint32_t sum32(const std::vector<std::uint8_t>& data) noexcept;

// --- dispatch -----------------------------------------------------------------
/// Computes whichever checksum a session is using.
std::uint16_t compute(ChecksumKind kind, const std::uint8_t* data, std::size_t size);

}  // namespace huaxin::protocols::spd
