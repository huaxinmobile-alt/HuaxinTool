#pragma once

// =============================================================================
//  Qualcomm Firehose protocol.
//
//  Firehose runs *after* Sahara has uploaded a programmer. It is a
//  request/response protocol carried over the same bulk endpoint: the host sends
//  a short XML document, the device answers with XML, and for bulk operations
//  switches to a raw byte stream in between.
//
//  PROVENANCE - the command shapes, attribute names and the response format were
//  transcribed from Qualcomm's upstream Linux EDL tool linux-msm/qdl
//  (BSD-3-Clause), files src/firehose.c and src/qdl.h. The `<configure>` and
//  `<read>` attribute sets and the `<power value="reset"/>` form are taken
//  verbatim from there.
//
//  Request:   <?xml version="1.0" ?><data><configure .../></data>
//  Response:  <?xml version="1.0" ?><data>
//               <log value="..."/>
//               <response value="ACK"/>                  or value="NAK"
//             </data>
//  A response may also carry rawmode="true", meaning the device is about to
//  stream raw bytes (the payload of a read) and the host must stop parsing XML
//  until it has consumed them.
// =============================================================================

#include <cstdint>
#include <string>
#include <vector>

#include "protocols/qualcomm/sahara.h"  // ProtocolError

namespace huaxin::protocols::qualcomm {

/// Owns unread USB bytes across XML responses and raw read payloads.
class FirehoseReader {
public:
    std::string response(IByteTransport& transport, unsigned int timeout_ms);
    void read_exact(IByteTransport& transport, std::uint8_t* data,
                    std::size_t size, unsigned int timeout_ms);
    void clear() { m_pending.clear(); }
private:
    std::string m_pending;
};

// --- storage ----------------------------------------------------------------
// The storage *types* are from qdl's `enum qdl_storage_type`. The exact strings
// the programmer expects in <configure MemoryName="..."/> could NOT be verified
// from a primary source here, so the wire name is supplied by the caller and is
// deliberately not defaulted to a guess.
enum class StorageType {
    Unknown,
    Emmc,
    Nand,
    Ufs,
    Nvme,
    SpiNor,
};

/// Human label for the UI. The wire name is a separate, caller-supplied string.
const char* to_string(StorageType type) noexcept;

// --- commands ----------------------------------------------------------------
struct ConfigureRequest {
    std::size_t max_payload_size_to_target{1024 * 1024};
    bool verbose{false};
    bool zero_length_packet_aware{true};  // ZlpAwareHost
    bool skip_storage_init{false};

    /// <configure MemoryName="...">. Empty omits the attribute.
    ///
    /// TODO: Implement the exact MemoryName spelling from a verified source -
    /// qdl's encode_storage_type() is in a file not vendored here, so the strings
    /// ("eMMC", "UFS", ...) are supplied by the caller rather than guessed.
    std::string memory_name;
};

struct ReadRequest {
    unsigned int sector_size_in_bytes{512};
    unsigned int physical_partition_number{0};
    std::uint64_t start_sector{0};
    std::uint64_t num_partition_sectors{0};
    std::string filename;
};

struct ProgramRequest {
    unsigned int sector_size_in_bytes{512};
    unsigned int physical_partition_number{0};
    std::uint64_t start_sector{0};
    std::uint64_t num_partition_sectors{0};
    std::string filename;
};

struct EraseRequest {
    unsigned int sector_size_in_bytes{512};
    unsigned int physical_partition_number{0};
    std::uint64_t start_sector{0};
    std::uint64_t num_partition_sectors{0};
};

struct PowerRequest {
    /// "reset" or "off" are the values qdl sends.
    std::string value{"reset"};
    unsigned int delay_seconds{10};
};

// --- builders ----------------------------------------------------------------
// Pure functions: no transport, no device. Each returns a complete document,
// prolog included, ready to write to the bulk-out endpoint.
std::string build_configure_xml(const ConfigureRequest& request);
std::string build_read_xml(const ReadRequest& request);
std::string build_program_xml(const ProgramRequest& request);
std::string build_erase_xml(const EraseRequest& request);
std::string build_power_xml(const PowerRequest& request);
std::string build_get_storage_info_xml();
std::string build_ping_xml();

// --- responses ---------------------------------------------------------------
enum class FirehoseStatus {
    Unknown,  // no <response> element at all
    Ack,
    Nak,
    Log,  // <response value="LOG"/> - informational, not a verdict
};

const char* to_string(FirehoseStatus status) noexcept;

struct FirehoseResponse {
    FirehoseStatus status{FirehoseStatus::Unknown};
    /// The device is switching to raw bytes; the host must read the payload
    /// before parsing any more XML.
    bool raw_mode{false};
    /// Every <log value="..."/> in order.
    std::vector<std::string> logs;
    /// The document as received, for the log console.
    std::string raw;
};

/// Parses a response document.
///
/// This is a scanner for the shapes the programmer actually emits, not a
/// general XML parser: it understands `<log value="..."/>` and
/// `<response value="..." rawmode="..."/>` with double-quoted attribute values
/// and the standard entity escapes, and ignores everything else. Malformed
/// input is reported rather than guessed at; unknown elements are skipped.
///
/// Throws ProtocolError only for input that cannot be a response at all.
FirehoseResponse parse_firehose_response(const std::string& xml);

/// Unescapes the five predefined XML entities plus numeric character
/// references. Exposed because the log messages need it too.
std::string xml_unescape(const std::string& text);

/// Escapes a value for use inside a double-quoted attribute.
std::string xml_escape(const std::string& text);

/// Extracts every complete XML document from a stream buffer, returning the
/// documents and leaving any trailing partial one in `pending`. The programmer
/// does not frame its responses, so the host has to find document boundaries.
std::vector<std::string> extract_documents(std::string& pending);

// --- storage geometry --------------------------------------------------------
/// What the programmer reports about the flash it is driving.
struct StorageInfo {
    std::uint64_t total_blocks{0};
    std::uint64_t block_size{0};
    /// "UFS", "eMMC", ... when the device states it; empty when it does not.
    std::string storage_type;
    /// The raw JSON object the device sent, kept so the UI can show fields this
    /// struct does not model rather than losing them.
    std::string raw_json;

    std::uint64_t total_bytes() const noexcept { return total_blocks * block_size; }
};

/// Pulls the geometry out of a <getstorageinfo> reply.
///
/// This is not an XML parse: the programmer encodes the geometry as a **JSON
/// blob inside a <log> value**, e.g.
///     <log value="{&quot;storage_info&quot;:{&quot;total_blocks&quot;:61071360,
///                 &quot;block_size&quot;:512}}"/>
/// so the JSON has to be picked out of the (unescaped) attribute text. The
/// extractor is deliberately narrow - it reads scalar members of `storage_info`
/// and nothing else - rather than a general JSON parser pretending to be
/// complete.
///
/// Returns false when no geometry is present, which happens on firmware that
/// answers with a bare ACK.
bool extract_storage_info(const FirehoseResponse& response, StorageInfo& out);

/// Reads one scalar member of a JSON object: numbers, or strings in quotes.
/// `object` is the JSON text; `key` is the member name without quotes.
bool json_scalar(const std::string& object, const std::string& key, std::string& out);

// --- rawprogram / patch files ------------------------------------------------
/// One <program> element from a rawprogramN.xml.
struct RawProgramEntry {
    ProgramRequest program;
    /// The GPT partition name this maps to, when the file states one. Matches
    /// the entry the GPT reports, not a sector range.
    std::string label;
};

/// Parses a rawprogramN.xml document into its <program> elements.
///
/// These files are a list of <program> elements wrapped in <data>. Attributes
/// are read by name; unknown ones are ignored, because the format carries
/// vendor extras that do not affect where the bytes land.
///
/// Throws ProtocolError when the text contains no <program> element at all,
/// which is the signature of the wrong file being selected.
std::vector<RawProgramEntry> parse_rawprogram_xml(const std::string& xml);

/// One <patch> element from a patchN.xml.
struct PatchEntry {
    std::string filename;
    std::string value;
    unsigned int sector_size_in_bytes{512};
    unsigned int physical_partition_number{0};
    std::uint64_t start_sector{0};
    unsigned int byte_offset{0};
    unsigned int size_in_bytes{0};
};

/// Parses a patchN.xml document into its <patch> elements.
std::vector<PatchEntry> parse_patch_xml(const std::string& xml);

/// Builds a <patch> command, the step that writes values such as the bootloader
/// version back into an image after programming it.
std::string build_patch_xml(const PatchEntry& patch);

}  // namespace huaxin::protocols::qualcomm
