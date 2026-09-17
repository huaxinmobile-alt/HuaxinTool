// =============================================================================
//  Odin protocol - the parts that came from rodin rather than Heimdall.
//
//  Split out of odin.cpp so the origin of each block stays visible: everything
//  here is the newer generation of the protocol (the ODIN/LOKE handshake, the
//  version negotiation, the 0x69 device-info command, the 500-byte PIT block
//  protocol and the flash sequence), and everything in odin.cpp is Heimdall's.
// =============================================================================

#include <algorithm>
#include <cstdio>
#include <cstring>

#include "protocols/samsung/odin.h"
#include "core/flash_timeouts.h"
#include "protocols/samsung/tar.h"

namespace huaxin::protocols::samsung {

using protocols::qualcomm::ProtocolError;

namespace {

std::string hex32(std::uint32_t value) {
    char buffer[16];
    std::snprintf(buffer, sizeof(buffer), "0x%08x", value);
    return buffer;
}

}  // namespace

// --- packet builders ---------------------------------------------------------
std::vector<std::uint8_t> build_pit_packet(OdinPitRequest request, std::uint32_t argument) {
    std::vector<std::uint8_t> packet = build_control_packet(OdinControl::PitFile);
    write_le32(packet, 4, static_cast<std::uint32_t>(request));
    if (argument != 0) {
        write_le32(packet, 8, argument);
    }
    return packet;
}

std::vector<std::uint8_t> build_file_packet(OdinFileRequest request, std::uint32_t argument) {
    std::vector<std::uint8_t> packet = build_control_packet(OdinControl::FileTransfer);
    write_le32(packet, 4, static_cast<std::uint32_t>(request));
    if (argument != 0) {
        write_le32(packet, 8, argument);
    }
    return packet;
}

std::vector<std::uint8_t> build_end_packet(OdinEndRequest request) {
    std::vector<std::uint8_t> packet = build_control_packet(OdinControl::EndSession);
    write_le32(packet, 4, static_cast<std::uint32_t>(request));
    return packet;
}

// --- PIT sanity and rebuilding -----------------------------------------------
std::string PitData::sanity_problem() const {
    if (entries.empty()) {
        return "it holds no partitions";
    }
    for (const PitEntry& entry : entries) {
        if (entry.partition_name.empty()) {
            return "an entry has no partition name, so nothing can be matched to it";
        }
    }
    // Two entries claiming the same partition id is the failure that matters:
    // a write aimed at one would land on whichever the device resolved it to.
    for (std::size_t left = 0; left < entries.size(); ++left) {
        for (std::size_t right = left + 1; right < entries.size(); ++right) {
            if (entries[left].identifier == entries[right].identifier) {
                return "two entries claim partition id " + std::to_string(entries[left].identifier)
                       + " (" + entries[left].partition_name + " and "
                       + entries[right].partition_name + ")";
            }
        }
    }
    // An entry with no block count describes a partition of zero length, which
    // is legal for a purely informational entry but is worth naming when the
    // table is about to be written to a device.
    std::size_t empty_count = 0;
    for (const PitEntry& entry : entries) {
        if (entry.block_count == 0) {
            ++empty_count;
        }
    }
    if (empty_count == entries.size()) {
        return "every entry has a zero block count, so no partition would have any space";
    }
    return {};
}

bool PitData::looks_sane() const {
    return sanity_problem().empty();
}

std::vector<std::uint8_t> build_pit(const PitData& pit) {
    if (pit.entries.empty()) {
        throw ProtocolError("refusing to build a partition table with no partitions");
    }
    const std::size_t needed = pit.expected_size();
    // A PIT is written in whole 4096-byte flash blocks, so the image is padded
    // up rather than left at 28 + 132n.
    const std::size_t total = (needed + kPitBlockSize - 1) / kPitBlockSize * kPitBlockSize;
    std::vector<std::uint8_t> out(total, 0);

    // The field order here is the parser's, which is Heimdall's: the entry
    // count sits at offset 4, immediately after the magic, and the two reserved
    // words follow it. Writing them in the order the struct declares them
    // instead of the order they are read puts the count where the parser looks
    // for a reserved word, and the table comes back with no partitions in it -
    // a repartition that silently empties the table. The round-trip test is what
    // caught that.
    write_le32(out, 0, PitData::kFileIdentifier);
    write_le32(out, 4, static_cast<std::uint32_t>(pit.entries.size()));
    write_le32(out, 8, pit.reserved1);
    write_le32(out, 12, pit.reserved2);

    std::size_t offset = PitData::kHeaderDataSize;
    for (const PitEntry& entry : pit.entries) {
        write_le32(out, offset + 0, entry.binary_type);
        write_le32(out, offset + 4, entry.device_type);
        write_le32(out, offset + 8, entry.identifier);
        write_le32(out, offset + 12, entry.attributes);
        write_le32(out, offset + 16, entry.update_attributes);
        write_le32(out, offset + 20, entry.block_size_or_offset);
        write_le32(out, offset + 24, entry.block_count);
        write_le32(out, offset + 28, entry.file_offset);
        write_le32(out, offset + 32, entry.file_size);
        // Three name fields of 32 bytes each, at 36, 68 and 100.
        const std::string names[3] = {entry.partition_name, entry.flash_filename,
                                      entry.fota_filename};
        for (std::size_t index = 0; index < 3; ++index) {
            const std::size_t name_offset = offset + 36 + index * 32;
            const std::size_t length = std::min<std::size_t>(names[index].size(), 31);
            std::memcpy(out.data() + name_offset, names[index].data(), length);
        }
        offset += PitData::kEntryDataSize;
    }
    return out;
}

// --- session information -----------------------------------------------------
std::string SessionInfo::describe() const {
    char buffer[256];
    if (version_known) {
        std::snprintf(buffer, sizeof(buffer),
                      "protocol version %u, %u byte file parts%s (raw word %s)",
                      version, packet_size, lz4_supported ? ", LZ4 accepted" : "",
                      hex32(raw).c_str());
    } else {
        std::snprintf(buffer, sizeof(buffer),
                      "%u byte file parts, no protocol version reported (raw word %s)",
                      packet_size, hex32(raw).c_str());
    }
    return buffer;
}

std::string DeviceInfo::describe() const {
    char buffer[192];
    std::snprintf(buffer, sizeof(buffer), "device info: magic %s, %u entries",
                  hex32(magic).c_str(), count);
    std::string text = buffer;
    if (!recognised) {
        text += " (the magic is not the one this build expects)";
    }
    return text;
}

// --- the handshake -----------------------------------------------------------
void OdinSession::handshake() {
    check_cancelled();
    if (m_handshake_done) {
        return;
    }

    // The bulk form first: four bytes out, four back.
    bool bulk_worked = false;
    try {
        m_transport.write_all(reinterpret_cast<const std::uint8_t*>(kHandshakeRequest),
                              kHandshakeLength, kHandshakeTimeout);
        std::uint8_t reply[kHandshakeLength] = {0};
        m_transport.read_exact(reply, kHandshakeLength, kHandshakeTimeout);
        if (std::memcmp(reply, kHandshakeResponse, kHandshakeLength) == 0) {
            bulk_worked = true;
            log("ok", "handshake: ODIN <-> LOKE");
        } else {
            log("warn", "the device answered the handshake with '"
                            + std::string(reinterpret_cast<const char*>(reply), kHandshakeLength)
                            + "' instead of 'LOKE'; trying the control-transfer form");
        }
    } catch (const ProtocolError& error) {
        log("warn", std::string("the bulk handshake did not complete (") + error.what()
                        + "); trying the control-transfer form");
    }

    // The control-transfer form, which is what odin4 uses and what a bootloader
    // that has stalled its bulk endpoint needs.
    if (!bulk_worked) {
        try {
            m_transport.control_transfer(kHandshakeOutRequestType, kHandshakeRequestCode,
                                         kHandshakeValue, 0, kHandshakeTimeout);
            // The out transfer carries "ODIN" as its data, which the transport
            // interface cannot express, so the bulk write is repeated here. A
            // bootloader that accepts the control form accepts both.
            m_transport.write_all(reinterpret_cast<const std::uint8_t*>(kHandshakeRequest),
                                  kHandshakeLength, kHandshakeTimeout);
            std::uint8_t reply[kHandshakeLength] = {0};
            m_transport.read_exact(reply, kHandshakeLength, kHandshakeTimeout);
            if (std::memcmp(reply, kHandshakeResponse, kHandshakeLength) == 0) {
                bulk_worked = true;
                log("ok", "handshake over the control endpoint: ODIN <-> LOKE");
            }
        } catch (const ProtocolError& error) {
            log("warn", std::string("the control-transfer handshake did not complete either: ")
                            + error.what());
        }
    }

    if (!bulk_worked) {
        throw ProtocolError(
            "the device did not answer the ODIN handshake with LOKE. It is either not in "
            "download mode, or its bulk endpoint is stalled - unplugging and reconnecting "
            "the cable in download mode is the usual fix.");
    }
    m_handshake_done = true;
}

// --- begin session -----------------------------------------------------------
SessionInfo OdinSession::begin_session() {
    check_cancelled();
    if (!m_handshake_done) {
        throw ProtocolError(
            "the ODIN/LOKE handshake has to succeed before a session can be opened; the "
            "bootloader answers nothing at all until it has.");
    }

    log("info", "opening an Odin session");

    // The argument is the highest protocol version this host understands. A
    // classic bootloader ignores the word entirely; a newer one answers with its
    // own version, which is what the whole negotiation hangs on.
    constexpr std::uint32_t kMaxSupportedVersion = 0x7FFFFFFFu;
    write_packet(build_session_packet(OdinSessionRequest::BeginSession, kMaxSupportedVersion));

    // Only the response *type* indicates success here. The second word is not a
    // status: on a classic bootloader it is the device's preferred packet size,
    // on a newer one its high half is the protocol version. Treating non-zero as
    // a refusal would reject every device that offers either.
    const OdinResponse response = read_response(OdinControl::Session);
    m_session_open = true;

    m_session = SessionInfo{};
    m_session.raw = response.result;

    // The newer bootloaders put the version in the high half. Whether that is
    // what the bits mean is not knowable from the word alone, so the reading is
    // accepted only when it looks like one: a classic bootloader's packet size
    // is a round number with a zero high half, and a version of 0 is not a
    // version.
    const std::uint16_t high = static_cast<std::uint16_t>((response.result >> 16) & 0xFFFF);
    const std::uint16_t low = static_cast<std::uint16_t>(response.result & 0xFFFF);
    if (high != 0 && high < 0x1000) {
        m_session.version = static_cast<std::uint16_t>(high & 0x7FFF);
        m_session.version_known = true;
        m_session.lz4_supported = (high & 0x8000) != 0;
    } else if (low != 0 && (response.result & 0xFFFF0000u) == 0) {
        // A bare packet size, which is the classic reply.
        m_session.version_known = false;
    }

    if (m_session.version_known && m_session.version >= 2) {
        // Version 2 and above want a megabyte per part and an explicit
        // SendFilePartSize before the first transfer.
        m_session.packet_size = kModernFilePartSize;
        log("ok", m_session.describe());
        set_file_part_size(m_session.packet_size);
    } else if (m_session.raw >= 512 && m_session.raw <= 16u * 1024u * 1024u
               && (response.result & 0xFFFF0000u) == 0) {
        // The classic reading: a plausible packet size and nothing above it.
        m_session.packet_size = m_session.raw;
        log("ok", m_session.describe());
    } else {
        m_session.packet_size = kDefaultFilePartSize;
        log("ok", m_session.describe());
    }
    return m_session;
}

// --- device information ------------------------------------------------------
DeviceInfo OdinSession::read_device_info() {
    check_cancelled();
    if (!m_session_open) {
        throw ProtocolError("the session has to be open before the device can be queried");
    }
    log("info", "requesting device information");
    write_packet(build_control_packet(OdinControl::DeviceInfo));
    const OdinResponse response = read_response(OdinControl::DeviceInfo);
    if (response.result == 0) {
        throw ProtocolError(
            "the device did not return device information. Older bootloaders do not implement "
            "this command at all, which is not a fault.");
    }
    // A sane bound: the bootloader sends a header and two arrays of that many
    // words. Anything larger is a corrupt length rather than a real reply.
    if (response.result > 64u * 1024u) {
        throw ProtocolError("the device claims a " + std::to_string(response.result)
                            + " byte device-info reply, far larger than any real one");
    }

    std::vector<std::uint8_t> raw(response.result);
    read_exact_raw(raw.data(), raw.size());
    if (raw.size() < 8) {
        throw ProtocolError("the device-info reply is " + std::to_string(raw.size())
                            + " bytes, too short to hold its header");
    }

    DeviceInfo info;
    info.magic = read_le32(raw, 0);
    info.count = read_le32(raw, 4);
    info.recognised = info.magic == kDeviceInfoMagic;

    // magic, count, then count index words and count offset words.
    const std::size_t available = (raw.size() - 8) / 8;
    const std::size_t usable = std::min<std::size_t>(info.count, available);
    if (usable < info.count) {
        log("warn", "the device claims " + std::to_string(info.count)
                        + " device-info entries but the reply only holds "
                        + std::to_string(available));
    }
    for (std::size_t index = 0; index < usable; ++index) {
        DeviceInfoEntry entry;
        entry.index = read_le32(raw, 8 + index * 4);
        entry.offset = read_le32(raw, 8 + info.count * 4 + index * 4);
        info.entries.push_back(entry);
    }
    log(info.recognised ? "ok" : "warn", info.describe());
    return info;
}

// --- PIT ---------------------------------------------------------------------
PitData OdinSession::read_pit() {
    check_cancelled();
    if (!m_session_open) {
        throw ProtocolError("the session has to be open before the PIT can be read");
    }

    log("info", "requesting the partition table");
    // Request the dump first, then read it block by block. Heimdall issues one
    // bare PitFile packet and reads the whole table; the newer bootloaders
    // expect the explicit request, and they answer the classic form too, so the
    // explicit one is what is sent.
    write_packet(build_pit_packet(OdinPitRequest::Dump));

    const OdinResponse response = read_response(OdinControl::PitFile);
    // Here the result word is not a status: it is the length of the PIT that
    // follows. A zero length is the device saying it will not hand one over.
    if (response.result == 0) {
        throw ProtocolError("the device did not return a partition table");
    }
    if (response.result > 1024u * 1024u) {
        throw ProtocolError("the device claims a " + std::to_string(response.result)
                            + " byte partition table, which is far larger than any real PIT");
    }

    const std::size_t pit_size = response.result;
    const std::size_t blocks =
        (pit_size + kPitDumpBlockSize - 1) / kPitDumpBlockSize;

    std::vector<std::uint8_t> raw(pit_size, 0);
    for (std::size_t index = 0; index < blocks; ++index) {
        check_cancelled();
        // Each block is asked for individually with its own packet. Doing one
        // long read here is the mistake: the device sends 500 bytes and waits.
        write_packet(build_pit_packet(OdinPitRequest::Part, static_cast<std::uint32_t>(index)));
        std::uint8_t block[kPitDumpBlockSize] = {0};
        read_exact_raw(block, kPitDumpBlockSize);
        const std::size_t offset = index * kPitDumpBlockSize;
        const std::size_t count = std::min(kPitDumpBlockSize, pit_size - offset);
        std::memcpy(raw.data() + offset, block, count);
    }

    write_packet(build_pit_packet(OdinPitRequest::EndTransfer));
    // The device does not always answer this one. A missing reply is not an
    // error, so it is attempted and its failure ignored.
    try {
        read_response(OdinControl::PitFile);
    } catch (const ProtocolError&) {
        log("debug", "no end-of-PIT-dump response, which is normal");
    }

    log("ok", "received " + std::to_string(raw.size()) + " bytes of partition table");
    PitData pit = parse_pit(raw);
    log("ok", "partition table holds " + std::to_string(pit.entries.size()) + " entries");
    return pit;
}

void OdinSession::flash_pit(const std::vector<std::uint8_t>& pit_data) {
    check_cancelled();
    if (!m_session_open) {
        throw ProtocolError("the session has to be open before the PIT can be written");
    }
    if (pit_data.empty()) {
        throw ProtocolError("refusing to write an empty partition table");
    }
    // The table itself is checked before it is sent, not after. A repartition
    // with a table that does not parse is the one operation here that can leave
    // a device unbootable on its own, and the check costs nothing.
    const PitData pit = parse_pit(pit_data);
    if (!pit.looks_sane()) {
        throw ProtocolError("refusing to write this partition table: " + pit.sanity_problem());
    }

    log("warn", "writing " + std::to_string(pit.entries.size())
                    + " partitions to the device's partition table");

    write_packet(build_pit_packet(OdinPitRequest::Flash));
    read_response(OdinControl::PitFile);

    // The table is sent in 4096-byte blocks, each followed by an 8-byte reply,
    // and the whole transfer is closed with the file size.
    std::size_t offset = 0;
    while (offset < pit_data.size()) {
        check_cancelled();
        const std::size_t count = std::min(kPitBlockSize, pit_data.size() - offset);
        write_packet(build_pit_packet(OdinPitRequest::Part, static_cast<std::uint32_t>(count)));
        read_response(OdinControl::PitFile);

        std::vector<std::uint8_t> block(kPitBlockSize, 0);
        std::memcpy(block.data(), pit_data.data() + offset, count);
        m_transport.write_all(block.data(), block.size(),
                              core::transfer_timeout_ms(kFlashTimeout));
        read_response(OdinControl::PitFile);
        offset += count;
    }

    write_packet(build_pit_packet(OdinPitRequest::EndTransfer,
                                  static_cast<std::uint32_t>(pit_data.size())));
    try {
        read_response(OdinControl::PitFile);
    } catch (const ProtocolError&) {
    }
    log("ok", "partition table written");
}

// --- flashing ----------------------------------------------------------------
void OdinSession::write_sequence(const FlashTarget& target, const std::uint8_t* data,
                                 std::size_t size, std::uint64_t file_size, bool is_last_sequence) {
    // Request this sequence, with the aligned length. The device expects a
    // whole number of file parts, so the length is rounded up.
    const std::uint32_t packet_size = m_session.packet_size;
    const std::uint64_t aligned = ((size + packet_size - 1) / packet_size) * packet_size;

    write_packet(build_file_packet(OdinFileRequest::Part, static_cast<std::uint32_t>(aligned)));
    read_response(OdinControl::FileTransfer);

    // The data itself, one file part at a time, each followed by a reply.
    std::vector<std::uint8_t> part(packet_size, 0);
    std::size_t sent = 0;
    while (sent < size) {
        check_cancelled();
        const std::size_t count = std::min<std::size_t>(packet_size, size - sent);
        std::fill(part.begin(), part.end(), 0);
        std::memcpy(part.data(), data + sent, count);
        m_transport.write_all(part.data(), part.size(),
                              core::transfer_timeout_ms(kFlashTimeout));
        // The reply to a data part is a SendFilePart response, which
        // decode_response maps onto the file-transfer type deliberately.
        read_response(OdinControl::FileTransfer);
        sent += count;
        if (m_callbacks.progress) {
            m_callbacks.progress(static_cast<int>((sent * 100) / (size == 0 ? 1 : size)),
                                 "sending " + target.file_name + ": " + std::to_string(sent)
                                     + " of " + std::to_string(size) + " bytes");
        }
    }

    // The end-of-sequence packet. The two layouts are not the same size and the
    // device reads them by the transfer type, which is why this cannot be one
    // generic packet.
    std::vector<std::uint8_t> end = build_file_packet(OdinFileRequest::End);
    if (target.binary_type == 1) {
        // Comms processor: 0x01 marks a modem transfer.
        write_le32(end, 8, 1);
        write_le32(end, 12, static_cast<std::uint32_t>(size));
        write_le32(end, 16, target.binary_type);
        write_le32(end, 20, target.device_type);
        write_le32(end, 24, is_last_sequence ? 1 : 0);
    } else {
        // Application processor.
        write_le32(end, 8, 0);
        write_le32(end, 12, static_cast<std::uint32_t>(size));
        write_le32(end, 16, target.binary_type);
        write_le32(end, 20, target.device_type);
        write_le32(end, 24, target.partition_id);
        write_le32(end, 28, is_last_sequence ? 1 : 0);
        // The last two words are the EFS-clear and bootloader-update flags. This
        // build sets neither: both change the device beyond flashing the
        // partition, and neither was asked for.
        write_le32(end, 32, 0);
        write_le32(end, 36, 0);
    }
    (void)file_size;
    write_packet(end);
    read_response(OdinControl::FileTransfer);
}

FlashOutcome OdinSession::flash_partition(const FlashTarget& target,
                                          const std::vector<std::uint8_t>& image,
                                          std::uint64_t total_bytes) {
    check_cancelled();
    if (!m_session_open) {
        throw ProtocolError("the session has to be open before anything can be flashed");
    }
    if (image.empty()) {
        throw ProtocolError("refusing to flash an empty image for " + target.file_name);
    }

    FlashOutcome outcome;
    outcome.file_name = target.file_name;
    outcome.partition_name = target.partition_name;
    outcome.bytes = image.size();

    log("info", "flashing " + target.file_name + " (" + std::to_string(image.size())
                    + " bytes) to " + target.partition_name);

    // Announce the total once, before the first transfer of the run.
    send_total_bytes(total_bytes);

    write_packet(build_file_packet(OdinFileRequest::Flash));
    read_response(OdinControl::FileTransfer);

    const std::uint64_t sequence_size = kDefaultSequenceSize;
    const std::size_t sequences =
        static_cast<std::size_t>((image.size() + sequence_size - 1) / sequence_size);

    std::uint64_t offset = 0;
    for (std::size_t index = 0; index < sequences; ++index) {
        const std::size_t remaining = image.size() - static_cast<std::size_t>(offset);
        const std::size_t count = static_cast<std::size_t>(
            std::min<std::uint64_t>(sequence_size, remaining));
        const bool is_last = offset + count >= image.size();
        write_sequence(target, image.data() + offset, count, image.size(), is_last);
        offset += count;
        if (m_callbacks.progress) {
            m_callbacks.progress(static_cast<int>((offset * 100) / image.size()),
                                 target.partition_name + ": " + std::to_string(offset) + " of "
                                     + std::to_string(image.size()) + " bytes");
        }
    }

    outcome.succeeded = true;
    log("ok", target.file_name + " written to " + target.partition_name);
    return outcome;
}

std::vector<FlashOutcome> OdinSession::flash_archive(const std::vector<std::uint8_t>& archive_data,
                                                     const std::vector<FlashTarget>& targets,
                                                     bool continue_on_error) {
    if (targets.empty()) {
        throw ProtocolError("nothing to flash: no target was given");
    }
    // Every member is located before the first byte is written. A package
    // missing one of its files must fail before the device is modified, because
    // the partitions written before the discovery would be new while the rest
    // are old.
    const TarArchive archive = parse_tar(archive_data);
    std::vector<std::string> missing;
    std::uint64_t total = 0;
    for (const FlashTarget& target : targets) {
        const TarEntry* entry = archive.find(target.file_name);
        if (entry == nullptr) {
            missing.push_back(target.file_name);
            continue;
        }
        total += entry->size;
    }
    if (!missing.empty()) {
        std::string list;
        for (std::size_t index = 0; index < missing.size() && index < 5; ++index) {
            list += (index == 0 ? "" : ", ") + missing[index];
        }
        throw ProtocolError("refusing to start: " + std::to_string(missing.size())
                            + " file(s) named by the target list are not in the archive ("
                            + list + "). Nothing was written.");
    }

    log("info", "flashing " + std::to_string(targets.size()) + " partitions, "
                    + std::to_string(total) + " bytes in total");

    std::vector<FlashOutcome> outcomes;
    for (const FlashTarget& target : targets) {
        if (m_callbacks.cancelled && m_callbacks.cancelled()) {
            FlashOutcome skipped;
            skipped.file_name = target.file_name;
            skipped.partition_name = target.partition_name;
            skipped.error = "cancelled before this partition was reached";
            outcomes.push_back(skipped);
            break;
        }
        try {
            const TarEntry* entry = archive.find(target.file_name);
            const std::vector<std::uint8_t> image = extract_entry(archive_data, *entry);
            outcomes.push_back(flash_partition(target, image, total));
        } catch (const ProtocolError& error) {
            FlashOutcome failed;
            failed.file_name = target.file_name;
            failed.partition_name = target.partition_name;
            failed.error = error.what();
            log("error", target.file_name + ": " + failed.error);
            outcomes.push_back(failed);
            if (!continue_on_error) {
                log("error",
                    "stopping after " + target.file_name
                        + ". The partitions written before this point are on the device and "
                          "the ones after it are not; flash the whole package again once the "
                          "cause is fixed rather than continuing from here.");
                break;
            }
        }
    }
    return outcomes;
}

void OdinSession::erase_partition(std::uint32_t partition_id, std::uint32_t device_type,
                                  std::uint64_t total_bytes) {
    check_cancelled();
    if (!m_session_open) {
        throw ProtocolError("the session has to be open before anything can be erased");
    }
    if (total_bytes == 0) {
        throw ProtocolError("refusing to erase a zero-length region");
    }

    // Odin has no erase command. The tools erase by sending a region of zeroes
    // through the ordinary file-transfer path, which is what this does - and
    // saying so matters, because it is slower than a real erase and wears the
    // flash the same way a write does.
    log("warn", "erasing " + std::to_string(total_bytes)
                    + " bytes by writing zeroes: this protocol has no erase command, so the "
                      "region is overwritten rather than trimmed");

    FlashTarget target;
    target.file_name = "(erase)";
    target.partition_name = "partition " + std::to_string(partition_id);
    target.partition_id = partition_id;
    target.device_type = device_type;
    target.binary_type = 0;

    send_total_bytes(total_bytes);
    write_packet(build_file_packet(OdinFileRequest::Flash));
    read_response(OdinControl::FileTransfer);

    const std::uint64_t sequence_size = kDefaultSequenceSize;
    std::vector<std::uint8_t> zeroes(static_cast<std::size_t>(sequence_size), 0);
    std::uint64_t offset = 0;
    while (offset < total_bytes) {
        check_cancelled();
        const std::size_t count = static_cast<std::size_t>(
            std::min<std::uint64_t>(sequence_size, total_bytes - offset));
        const bool is_last = offset + count >= total_bytes;
        write_sequence(target, zeroes.data(), count, total_bytes, is_last);
        offset += count;
        if (m_callbacks.progress) {
            m_callbacks.progress(static_cast<int>((offset * 100) / total_bytes),
                                 "erasing: " + std::to_string(offset) + " of "
                                     + std::to_string(total_bytes) + " bytes");
        }
    }
    log("ok", "erase complete");
}

void OdinSession::reboot_to_download() {
    check_cancelled();
    write_packet(build_end_packet(OdinEndRequest::RebootToDownload));
    try {
        read_response(OdinControl::EndSession);
    } catch (const ProtocolError&) {
        log("debug", "no response to the reboot request, which is normal");
    }
    m_session_open = false;
    log("info", "the device has been asked to reboot into download mode");
}

}  // namespace huaxin::protocols::samsung
