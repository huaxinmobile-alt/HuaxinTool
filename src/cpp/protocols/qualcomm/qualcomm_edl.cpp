#include "protocols/qualcomm/qualcomm_edl.h"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <fstream>
#include <string>
#include <utility>
#include <vector>

#include "protocols/qualcomm/firehose.h"

namespace huaxin::protocols::qualcomm {

namespace {

/// Sahara image id the Firehose programmer is requested under. The device names
/// the id it wants in READ_DATA; 13 is the value every observed chipset asks for,
/// and the image map is keyed by whatever the device requests, so a device that
/// asks for a different id still gets its data as long as it is in the map.
constexpr std::uint32_t kProgrammerImageId = 13;

/// Reading a response waits for the device to finish thinking. Erasing a large
/// partition takes longer than answering a ping, hence the per-call overrides.
constexpr unsigned int kDefaultResponseTimeoutMs = 10000;

std::vector<std::uint8_t> read_file(const std::string& path, std::size_t limit) {
    std::ifstream file(path, std::ios::binary | std::ios::ate);
    if (!file) {
        throw ProtocolError("cannot open " + path);
    }
    const std::streamoff size = file.tellg();
    if (size <= 0) {
        throw ProtocolError(path + " is empty");
    }
    if (static_cast<std::size_t>(size) > limit) {
        throw ProtocolError(path + " is " + std::to_string(size) + " bytes, larger than the "
                            + std::to_string(limit) + " byte limit");
    }
    file.seekg(0, std::ios::beg);
    std::vector<std::uint8_t> data(static_cast<std::size_t>(size));
    if (!file.read(reinterpret_cast<char*>(data.data()), size)) {
        throw ProtocolError("could not read all of " + path);
    }
    return data;
}

}  // namespace

QualcommEdl::QualcommEdl(Callbacks callbacks) : m_callbacks(std::move(callbacks)) {}

QualcommEdl::~QualcommEdl() {
    disconnect();
}

bool QualcommEdl::device_present() {
    return usb::EdlTransport::device_present();
}

void QualcommEdl::disconnect() noexcept {
    m_transport.close();
    m_programmer_loaded = false;
}

void QualcommEdl::connect() {
    if (m_transport.is_open()) {
        return;
    }
    m_transport.open();
    if (m_callbacks.log) {
        m_callbacks.log("ok", "opened " + m_transport.describe());
    }
}

void QualcommEdl::reopen() {
    // A device that was reset re-enumerates, which invalidates any handle we
    // hold. Reconnecting is cheap and removes an entire class of stale-handle
    // failures, so every fresh Sahara conversation starts here.
    disconnect();
    connect();
}

// --- Sahara ------------------------------------------------------------------
SaharaSession::Callbacks QualcommEdl::sahara_callbacks() const {
    return SaharaSession::Callbacks{m_callbacks.log, m_callbacks.progress, m_callbacks.cancelled};
}

SaharaDeviceInfo QualcommEdl::read_device_info() {
    reopen();
    if (m_callbacks.log) {
        m_callbacks.log("info", "starting a Sahara conversation to read device identity");
    }
    SaharaSession session(m_transport, sahara_callbacks());
    // Nothing will be uploaded, so leave the device in its initial state.
    m_info = session.query_device_identity(SaharaDeviceInfo{}, false);
    m_programmer_loaded = false;
    return m_info;
}

SaharaDeviceInfo QualcommEdl::load_programmer(const std::string& path, bool read_identity) {
    // 16 MiB is far above any real programmer (they run a few hundred KB) and
    // well below anything that would indicate the wrong file was picked.
    const std::vector<std::uint8_t> programmer = read_file(path, 16u * 1024u * 1024u);
    if (m_callbacks.log) {
        m_callbacks.log("info", "programmer " + path + " is " + std::to_string(programmer.size())
                                    + " bytes");
    }

    reopen();
    SaharaSession session(m_transport, sahara_callbacks());

    if (read_identity) {
        // Entering command mode hands the device back to image transfer with a
        // fresh HELLO, which the run() below then picks up.
        m_info = session.query_device_identity(SaharaDeviceInfo{}, true);
    }

    SaharaSession::ImageTable images;
    images.emplace(kProgrammerImageId, programmer);
    m_info = session.run(images);

    m_programmer_loaded = true;
    if (m_callbacks.log) {
        m_callbacks.log("ok", "programmer uploaded; the device is now in Firehose mode");
    }
    return m_info;
}

// --- Firehose ----------------------------------------------------------------
std::string QualcommEdl::read_response_document(unsigned int timeout_ms) {
    std::string pending;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    std::uint8_t buffer[4096];

    for (;;) {
        std::vector<std::string> documents = extract_documents(pending);
        if (!documents.empty()) {
            return documents.front();
        }
        const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(
                              deadline - std::chrono::steady_clock::now())
                              .count();
        if (left <= 0) {
            throw ProtocolError("timed out waiting for a Firehose response"
                                + (pending.empty() ? std::string{}
                                                   : " (partial: " + pending.substr(0, 120) + ")"));
        }
        const std::size_t received = m_transport.read_some(buffer, sizeof(buffer),
                                                           static_cast<unsigned int>(left));
        pending.append(reinterpret_cast<const char*>(buffer), received);
    }
}

FirehoseResponse QualcommEdl::send_firehose(const std::string& xml, unsigned int timeout_ms) {
    if (!m_programmer_loaded) {
        throw ProtocolError(
            "the device is not in Firehose mode yet: a programmer has to be uploaded "
            "through Sahara first (Load Firehose Programmer).");
    }
    if (m_callbacks.log) {
        m_callbacks.log("debug", "FIREHOSE -> " + xml);
    }
    m_transport.write_all(reinterpret_cast<const std::uint8_t*>(xml.data()), xml.size(),
                          kDefaultResponseTimeoutMs);

    const std::string document = read_response_document(timeout_ms);
    m_last_response = document;
    FirehoseResponse response = parse_firehose_response(document);

    if (m_callbacks.log) {
        for (const std::string& line : response.logs) {
            m_callbacks.log("output", line);
        }
        m_callbacks.log(response.status == FirehoseStatus::Nak ? "error" : "debug",
                        std::string("FIREHOSE <- ") + to_string(response.status)
                            + (response.raw_mode ? " (raw mode)" : ""));
    }
    return response;
}

FirehoseResponse QualcommEdl::configure(const ConfigureRequest& request, unsigned int timeout_ms) {
    return send_firehose(build_configure_xml(request), timeout_ms);
}

FirehoseResponse QualcommEdl::power(const PowerRequest& request) {
    // The device stops answering once it resets, so a missing response is normal
    // here; send_firehose still waits only as long as the timeout allows.
    return send_firehose(build_power_xml(request), 5000);
}

FirehoseResponse QualcommEdl::ping() {
    return send_firehose(build_ping_xml(), 2000);
}

// --- retry -------------------------------------------------------------------
FirehoseResponse QualcommEdl::send_with_retry(const std::string& xml, const std::string& what,
                                              unsigned int timeout_ms, unsigned int attempts) {
    FirehoseResponse response;
    for (unsigned int attempt = 1; attempt <= attempts; ++attempt) {
        if (m_callbacks.cancelled && m_callbacks.cancelled()) {
            throw ProtocolError("cancelled by the operator");
        }
        response = send_firehose(xml, timeout_ms);
        if (response.status != FirehoseStatus::Nak) {
            return response;
        }
        // A NAK is usually the programmer saying "not ready yet", so it is worth
        // another go. A protocol error or a disconnect is raised by
        // send_firehose and is deliberately not retried here: repeating a
        // command into a desynchronised stream makes things worse.
        if (attempt < attempts) {
            const std::string message = what + ": the device answered NAK, retrying ("
                                        + std::to_string(attempt) + " of " + std::to_string(attempts)
                                        + ")";
            if (m_callbacks.log) {
                m_callbacks.log("warn", message);
            }
        }
    }
    throw ProtocolError(what + ": the device answered NAK on all " + std::to_string(attempts)
                        + " attempts");
}

// --- storage -----------------------------------------------------------------
StorageInfo QualcommEdl::get_storage_info(unsigned int lun) {
    FirehoseResponse response = send_with_retry(build_get_storage_info_xml(),
                                                "getstorageinfo", kCommandTimeoutMs);
    if (response.status == FirehoseStatus::Nak) {
        throw ProtocolError("the device refused getstorageinfo");
    }

    StorageInfo info;
    if (!extract_storage_info(response, info)) {
        // Not fatal: some programmers answer a bare ACK, in which case the
        // caller falls back to a sector size it assumes. Saying so beats
        // inventing a geometry.
        if (m_callbacks.log) {
            m_callbacks.log("warn",
                            "the programmer did not report storage geometry; the device may not "
                            "support getstorageinfo on this firmware");
        }
        return info;
    }

    if (m_callbacks.log) {
        m_callbacks.log("ok", "storage: " + std::to_string(info.total_blocks) + " blocks of "
                                  + std::to_string(info.block_size) + " bytes ("
                                  + std::to_string(info.total_bytes() / (1024 * 1024)) + " MiB)"
                                  + (info.storage_type.empty() ? "" : ", " + info.storage_type));
    }
    return info;
}

// --- GPT ---------------------------------------------------------------------
GptTable QualcommEdl::read_gpt(unsigned int lun, std::uint64_t sector_size) {
    if (sector_size == 0) {
        const StorageInfo info = get_storage_info(lun);
        // 512 is the near-universal default; the geometry query above is where a
        // device that differs would say so.
        sector_size = info.block_size != 0 ? info.block_size : 512;
    }

    // LBA 1 holds the header; LBA 0 is the protective MBR and is skipped.
    ReadRequest header_request;
    header_request.sector_size_in_bytes = static_cast<unsigned int>(sector_size);
    header_request.physical_partition_number = lun;
    header_request.start_sector = 1;
    header_request.num_partition_sectors = 1;
    const std::vector<std::uint8_t> header_data = read_partition(header_request);
    const GptHeader header = parse_gpt_header(header_data.data(), header_data.size());

    if (m_callbacks.log) {
        m_callbacks.log("info", "GPT revision " + header.revision_string() + ", "
                                    + std::to_string(header.num_part_entries) + " entry slots, "
                                    + "array at LBA " + std::to_string(header.part_entry_lba));
    }

    // The entry array lives where the header says, not at a fixed LBA 2, so its
    // size has to be computed from the header before it can be read.
    const std::uint64_t array_bytes =
        static_cast<std::uint64_t>(header.num_part_entries) * header.part_entry_size;
    const std::uint64_t array_sectors = (array_bytes + sector_size - 1) / sector_size;

    ReadRequest array_request;
    array_request.sector_size_in_bytes = static_cast<unsigned int>(sector_size);
    array_request.physical_partition_number = lun;
    array_request.start_sector = header.part_entry_lba;
    array_request.num_partition_sectors = array_sectors;
    const std::vector<std::uint8_t> array_data = read_partition(array_request);

    GptTable table;
    table.header = header;
    table.entries = parse_gpt_entries(array_data.data(), array_data.size(),
                                      header.num_part_entries, header.part_entry_size);

    if (m_callbacks.log) {
        m_callbacks.log("ok", "partition table: " + std::to_string(table.used_entries().size())
                                  + " partitions in use, "
                                  + std::to_string(table.total_bytes(sector_size) / (1024 * 1024))
                                  + " MiB");
    }
    return table;
}

// --- data transfer -----------------------------------------------------------
void QualcommEdl::program_partition(const ProgramRequest& request,
                                    const std::vector<std::uint8_t>& image,
                                    unsigned int setup_timeout_ms,
                                    unsigned int completion_timeout_ms) {
    if (!m_programmer_loaded) {
        throw ProtocolError("a programmer has to be uploaded before anything can be written");
    }
    if (image.empty()) {
        throw ProtocolError("refusing to program an empty image");
    }

    const std::uint64_t expected =
        static_cast<std::uint64_t>(request.num_partition_sectors) * request.sector_size_in_bytes;
    if (expected != 0 && image.size() > expected) {
        throw ProtocolError("the image is " + std::to_string(image.size())
                            + " bytes but the request only claims room for "
                            + std::to_string(expected) + "; refusing to write past the partition");
    }

    // 1. the command, then the setup acknowledgement.
    send_with_retry(build_program_xml(request), "program", setup_timeout_ms);

    // 2. the data, streamed in payload-sized pieces.
    if (m_callbacks.log) {
        m_callbacks.log("info", "writing " + std::to_string(image.size()) + " bytes");
    }
    const std::size_t chunk = 1024 * 1024;  // the programmer negotiates its own ceiling
    std::uint64_t written = 0;
    while (written < image.size()) {
        if (m_callbacks.cancelled && m_callbacks.cancelled()) {
            throw ProtocolError("cancelled by the operator during a write; the partition is now "
                                "in an unknown state and must be rewritten");
        }
        const std::size_t count =
            static_cast<std::size_t>(std::min<std::uint64_t>(chunk, image.size() - written));
        m_transport.write_all(image.data() + written, count, kProgramCompletionTimeoutMs);
        written += count;
        if (m_progress) {
            m_progress(written, image.size());
        }
    }

    // 3. the verdict. This is where the device has actually finished writing, so
    //    it gets the longest timeout in the project.
    const std::string response = read_response_document(completion_timeout_ms);
    m_last_response = response;
    const FirehoseResponse parsed = parse_firehose_response(response);
    for (const std::string& line : parsed.logs) {
        if (m_callbacks.log) {
            m_callbacks.log("output", line);
        }
    }
    if (parsed.status != FirehoseStatus::Ack) {
        throw ProtocolError("the device did not confirm the write: "
                            + std::string(to_string(parsed.status)));
    }
    if (m_callbacks.log) {
        m_callbacks.log("ok", "wrote " + std::to_string(image.size()) + " bytes");
    }
}

std::vector<std::uint8_t> QualcommEdl::read_partition(const ReadRequest& request,
                                                     unsigned int timeout_ms) {
    if (!m_programmer_loaded) {
        throw ProtocolError("a programmer has to be uploaded before a partition can be read");
    }
    const std::uint64_t total =
        static_cast<std::uint64_t>(request.num_partition_sectors) * request.sector_size_in_bytes;
    if (total == 0) {
        throw ProtocolError("the read request asks for zero bytes");
    }
    // A read that cannot be held in memory is refused rather than attempted: the
    // caller would rather know now than after minutes of transfer.
    constexpr std::uint64_t kMaxReadBytes = 512ull * 1024ull * 1024ull;
    if (total > kMaxReadBytes) {
        throw ProtocolError("the read request is " + std::to_string(total / (1024 * 1024))
                            + " MiB, above the " + std::to_string(kMaxReadBytes / (1024 * 1024))
                            + " MiB limit this build holds in memory");
    }

    send_with_retry(build_read_xml(request), "read", kCommandTimeoutMs);

    std::vector<std::uint8_t> data(static_cast<std::size_t>(total));
    std::uint64_t received = 0;
    while (received < total) {
        if (m_callbacks.cancelled && m_callbacks.cancelled()) {
            throw ProtocolError("cancelled by the operator during a read");
        }
        const std::size_t count =
            static_cast<std::size_t>(std::min<std::uint64_t>(1024 * 1024, total - received));
        m_transport.read_exact(data.data() + received, count, timeout_ms);
        received += count;
        if (m_progress) {
            m_progress(received, total);
        }
    }

    // The device sends a closing response once the data is out.
    const std::string response = read_response_document(timeout_ms);
    m_last_response = response;
    const FirehoseResponse parsed = parse_firehose_response(response);
    if (parsed.status == FirehoseStatus::Nak) {
        throw ProtocolError("the device reported a failure after the read");
    }
    return data;
}

void QualcommEdl::erase_sectors(const EraseRequest& request, unsigned int timeout_ms) {
    send_with_retry(build_erase_xml(request), "erase", timeout_ms);
    if (m_callbacks.log) {
        m_callbacks.log("ok", "erased " + std::to_string(request.num_partition_sectors)
                                  + " sectors from " + std::to_string(request.start_sector));
    }
}

FirehoseResponse QualcommEdl::patch(const PatchEntry& entry, unsigned int timeout_ms) {
    return send_with_retry(build_patch_xml(entry), "patch", timeout_ms);
}

}  // namespace huaxin::protocols::qualcomm
