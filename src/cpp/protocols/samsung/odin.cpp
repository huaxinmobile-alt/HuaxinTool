#include "protocols/samsung/odin.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <utility>

namespace huaxin::protocols::samsung {

using protocols::qualcomm::ProtocolError;

namespace {

/// The whole table is little-endian, like Qualcomm's protocols and unlike
/// MediaTek's. Heimdall's unpackers do the same thing on the little-endian hosts
/// everyone actually uses.
std::uint16_t read_le16(const std::uint8_t* data) {
    return static_cast<std::uint16_t>(data[0] | (data[1] << 8));
}

/// Reads a fixed-size, NUL-padded name field. The field is not guaranteed to be
/// terminated, so the length is clamped rather than trusted.
std::string read_name(const std::uint8_t* data, std::size_t length) {
    std::size_t actual = 0;
    while (actual < length && data[actual] != 0) {
        ++actual;
    }
    return std::string(reinterpret_cast<const char*>(data), actual);
}

std::string hex32(std::uint32_t value) {
    char buffer[16];
    std::snprintf(buffer, sizeof(buffer), "0x%08x", value);
    return buffer;
}

}  // namespace

// --- PIT ---------------------------------------------------------------------
std::uint64_t PitEntry::size_bytes() const noexcept {
    // Heimdall does not compute a size; it flashes by block_count. Reporting one
    // is useful for the UI, but only when the geometry is actually present.
    if (block_size_or_offset == 0 || block_count == 0) {
        return 0;
    }
    return static_cast<std::uint64_t>(block_size_or_offset) * block_count;
}

std::string PitEntry::device_type_name() const {
    switch (device_type) {
        case 0: return "OneNAND";
        case 1: return "file/FAT";
        case 2: return "MMC";
        case 3: return "all";
        default: return "unknown";
    }
}

std::size_t PitData::expected_size() const noexcept {
    return kHeaderDataSize + entries.size() * kEntryDataSize;
}

const PitEntry* PitData::find(const std::string& partition_name) const {
    for (const PitEntry& entry : entries) {
        if (entry.partition_name == partition_name) {
            return &entry;
        }
    }
    return nullptr;
}

PitData parse_pit(const std::uint8_t* data, std::size_t size) {
    if (data == nullptr || size < PitData::kHeaderDataSize) {
        throw ProtocolError("the PIT is shorter than its 28-byte header ("
                            + std::to_string(size) + " bytes)");
    }

    const std::uint32_t magic = read_le32(data);
    if (magic != PitData::kFileIdentifier) {
        char buffer[64];
        std::snprintf(buffer, sizeof(buffer),
                      "this is not a PIT: expected magic 0x%08x, found 0x%08x",
                      PitData::kFileIdentifier, magic);
        throw ProtocolError(buffer);
    }

    const std::uint32_t entry_count = read_le32(data + 4);

    // The count comes off the wire, so it is checked against what the buffer can
    // actually hold before anything is allocated or indexed. A device that
    // reports 0xFFFFFFFF entries must not be able to make us allocate 500 GB.
    const std::size_t available = size - PitData::kHeaderDataSize;
    const std::size_t maximum_entries = available / PitData::kEntryDataSize;
    if (entry_count > maximum_entries) {
        throw ProtocolError("the PIT claims " + std::to_string(entry_count)
                            + " entries but only " + std::to_string(available)
                            + " bytes of table follow, enough for "
                            + std::to_string(maximum_entries));
    }

    PitData pit;
    pit.reserved1 = read_le32(data + 8);
    pit.reserved2 = read_le32(data + 12);
    pit.entries.reserve(entry_count);

    for (std::uint32_t index = 0; index < entry_count; ++index) {
        const std::uint8_t* entry = data + PitData::kHeaderDataSize + index * PitData::kEntryDataSize;
        PitEntry parsed;
        parsed.binary_type = read_le32(entry + 0);
        parsed.device_type = read_le32(entry + 4);
        parsed.identifier = read_le32(entry + 8);
        parsed.attributes = read_le32(entry + 12);
        parsed.update_attributes = read_le32(entry + 16);
        parsed.block_size_or_offset = read_le32(entry + 20);
        parsed.block_count = read_le32(entry + 24);
        parsed.file_offset = read_le32(entry + 28);
        parsed.file_size = read_le32(entry + 32);
        // The three name fields run to the end of the 132-byte entry.
        parsed.partition_name = read_name(entry + 36, 32);
        parsed.flash_filename = read_name(entry + 68, 32);
        parsed.fota_filename = read_name(entry + 100, 32);
        pit.entries.push_back(std::move(parsed));
    }

    return pit;
}

PitData parse_pit(const std::vector<std::uint8_t>& data) {
    return parse_pit(data.data(), data.size());
}

// --- packet construction -----------------------------------------------------
const char* to_string(OdinControl control) noexcept {
    switch (control) {
        case OdinControl::SendFilePart: return "SEND_FILE_PART";
        case OdinControl::Session:      return "SESSION";
        case OdinControl::PitFile:      return "PIT_FILE";
        case OdinControl::FileTransfer: return "FILE_TRANSFER";
        case OdinControl::EndSession:   return "END_SESSION";
    }
    return "UNKNOWN";
}

std::vector<std::uint8_t> build_control_packet(OdinControl control, std::size_t size) {
    if (size < 8) {
        throw ProtocolError("an Odin control packet needs at least 8 bytes, asked for "
                            + std::to_string(size));
    }
    std::vector<std::uint8_t> packet(size, 0);
    write_le32(packet, 0, static_cast<std::uint32_t>(control));
    return packet;
}

std::vector<std::uint8_t> build_session_packet(OdinSessionRequest request) {
    std::vector<std::uint8_t> packet = build_control_packet(OdinControl::Session);
    write_le32(packet, 4, static_cast<std::uint32_t>(request));
    return packet;
}

std::vector<std::uint8_t> build_session_packet(OdinSessionRequest request, std::uint32_t argument) {
    std::vector<std::uint8_t> packet = build_session_packet(request);
    write_le32(packet, 8, argument);
    return packet;
}

OdinResponse decode_response(const std::uint8_t* data, std::size_t size, OdinControl expected) {
    if (data == nullptr || size < kResponsePacketSize) {
        throw ProtocolError("an Odin response is 8 bytes, got " + std::to_string(size));
    }
    OdinResponse response;
    const std::uint32_t type = read_le32(data);
    response.result = read_le32(data + 4);

    // The device may answer with a file-part response where a control response
    // was expected: that is how it signals "ready for the next chunk". Treating
    // it as a mismatch would abort a transfer that is going fine.
    if (type == static_cast<std::uint32_t>(OdinControl::SendFilePart)
        && expected == OdinControl::FileTransfer) {
        response.type = OdinControl::SendFilePart;
        return response;
    }
    if (type != static_cast<std::uint32_t>(expected)) {
        throw ProtocolError(std::string("expected a ") + to_string(expected) + " response, got "
                            + hex32(type));
    }
    response.type = expected;
    return response;
}

// --- session -----------------------------------------------------------------
OdinSession::OdinSession(IByteTransport& transport, Callbacks callbacks)
    : m_transport(transport), m_callbacks(std::move(callbacks)) {}

void OdinSession::log(const std::string& level, const std::string& message) const {
    if (m_callbacks.log) {
        m_callbacks.log(level, message);
    }
}

void OdinSession::check_cancelled() const {
    if (m_callbacks.cancelled && m_callbacks.cancelled()) {
        throw ProtocolError("cancelled by the operator");
    }
}

void OdinSession::write_packet(const std::vector<std::uint8_t>& packet) {
    m_transport.write_all(packet.data(), packet.size(), 5000);
}

void OdinSession::read_exact_raw(std::uint8_t* buffer, std::size_t size) {
    m_transport.read_exact(buffer, size, 30000);
}

OdinResponse OdinSession::read_response(OdinControl expected) {
    std::uint8_t buffer[kResponsePacketSize] = {};
    read_exact_raw(buffer, sizeof(buffer));
    return decode_response(buffer, sizeof(buffer), expected);
}

void OdinSession::send_device_type(std::uint32_t device_type) {
    check_cancelled();
    write_packet(build_session_packet(OdinSessionRequest::DeviceType, device_type));
    read_response(OdinControl::Session);
}

void OdinSession::send_total_bytes(std::uint64_t total) {
    check_cancelled();
    // The protocol carries this as a 32-bit word, so anything larger cannot be
    // expressed - refusing beats silently truncating a multi-gigabyte total.
    if (total > 0xFFFFFFFFull) {
        throw ProtocolError("the total transfer size (" + std::to_string(total)
                            + " bytes) does not fit in the 32-bit field the protocol uses");
    }
    log("debug", "announcing " + std::to_string(total) + " bytes in total");
    write_packet(build_session_packet(OdinSessionRequest::TotalBytes, static_cast<std::uint32_t>(total)));
    read_response(OdinControl::Session);
}

void OdinSession::set_file_part_size(std::uint32_t size) {
    check_cancelled();
    log("debug", "setting the file part size to " + std::to_string(size));
    write_packet(build_session_packet(OdinSessionRequest::FilePartSize, size));
    read_response(OdinControl::Session);
}

void OdinSession::end_session() {
    if (!m_session_open) {
        return;
    }
    write_packet(build_control_packet(OdinControl::EndSession));
    // The device does not always answer to this; a missing response is not an
    // error, so the read is attempted and its failure ignored.
    try {
        read_response(OdinControl::EndSession);
    } catch (const ProtocolError&) {
        log("debug", "no end-session response, which is normal");
    }
    m_session_open = false;
    log("info", "session closed");
}

}  // namespace huaxin::protocols::samsung
