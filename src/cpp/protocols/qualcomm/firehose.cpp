#include "protocols/qualcomm/firehose.h"

#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstdlib>
#include <string>
#include <utility>

namespace huaxin::protocols::qualcomm {

namespace {

constexpr const char* kProlog = "<?xml version=\"1.0\" ?>";
constexpr const char* kOpenData = "<data>";
constexpr const char* kCloseData = "</data>";

std::string number(unsigned long long value) {
    return std::to_string(value);
}

/// Wraps one element in the prolog and <data> root the programmer expects.
std::string document(const std::string& element) {
    return std::string(kProlog) + kOpenData + element + kCloseData;
}

bool is_space(char character) {
    return character == ' ' || character == '\t' || character == '\n' || character == '\r';
}

/// Reads an attribute from a tag body.
///
/// Only double-quoted values are recognised, which is all the programmer emits
/// and all our own builders produce. Returns false when the attribute is absent
/// or its value is unterminated.
bool attribute_value(const std::string& tag, const std::string& name, std::string& out) {
    const std::string needle = name + "=";
    std::size_t position = 0;
    while ((position = tag.find(needle, position)) != std::string::npos) {
        // Must be preceded by whitespace or the start of the tag body, so that
        // searching for "value" does not match the tail of "rawmode_value".
        const bool boundary = position == 0 || is_space(tag[position - 1]);
        position += needle.size();
        if (!boundary) {
            continue;
        }
        if (position >= tag.size()) {
            return false;
        }
        const char quote = tag[position];
        if (quote != '"' && quote != '\'') {
            return false;
        }
        const std::size_t end = tag.find(quote, position + 1);
        if (end == std::string::npos) {
            return false;
        }
        out = tag.substr(position + 1, end - position - 1);
        return true;
    }
    return false;
}

}  // namespace

const char* to_string(StorageType type) noexcept {
    switch (type) {
        case StorageType::Unknown: return "unknown";
        case StorageType::Emmc:    return "eMMC";
        case StorageType::Nand:    return "NAND";
        case StorageType::Ufs:     return "UFS";
        case StorageType::Nvme:    return "NVMe";
        case StorageType::SpiNor:  return "SPI-NOR";
    }
    return "unknown";
}

const char* to_string(FirehoseStatus status) noexcept {
    switch (status) {
        case FirehoseStatus::Unknown: return "unknown";
        case FirehoseStatus::Ack:     return "ACK";
        case FirehoseStatus::Nak:     return "NAK";
        case FirehoseStatus::Log:     return "LOG";
    }
    return "unknown";
}

// --- escaping ----------------------------------------------------------------
std::string xml_escape(const std::string& text) {
    std::string out;
    out.reserve(text.size() + 8);
    for (char character : text) {
        switch (character) {
            case '&':  out += "&amp;"; break;
            case '<':  out += "&lt;"; break;
            case '>':  out += "&gt;"; break;
            case '"':  out += "&quot;"; break;
            case '\'': out += "&apos;"; break;
            default:   out.push_back(character); break;
        }
    }
    return out;
}

std::string xml_unescape(const std::string& text) {
    std::string out;
    out.reserve(text.size());
    for (std::size_t index = 0; index < text.size();) {
        if (text[index] != '&') {
            out.push_back(text[index++]);
            continue;
        }
        const std::size_t semicolon = text.find(';', index);
        if (semicolon == std::string::npos || semicolon - index > 12) {
            out.push_back(text[index++]);  // a bare '&' is kept as-is
            continue;
        }
        const std::string entity = text.substr(index + 1, semicolon - index - 1);
        if (entity == "amp") {
            out.push_back('&');
        } else if (entity == "lt") {
            out.push_back('<');
        } else if (entity == "gt") {
            out.push_back('>');
        } else if (entity == "quot") {
            out.push_back('"');
        } else if (entity == "apos") {
            out.push_back('\'');
        } else if (!entity.empty() && entity[0] == '#') {
            const bool decimal = entity.size() > 1 && entity[1] != 'x' && entity[1] != 'X';
            const std::string digits = decimal ? entity.substr(1) : entity.substr(2);
            if (!digits.empty()) {
                const unsigned long code = std::strtoul(digits.c_str(), nullptr, decimal ? 10 : 16);
                if (code < 0x80) {
                    out.push_back(static_cast<char>(code));
                } else if (code < 0x800) {
                    out.push_back(static_cast<char>(0xC0 | (code >> 6)));
                    out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
                } else {
                    out.push_back(static_cast<char>(0xE0 | (code >> 12)));
                    out.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
                    out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
                }
            }
        } else {
            // Not an entity we know: keep it verbatim rather than dropping data.
            out.append(text, index, semicolon - index + 1);
        }
        index = semicolon + 1;
    }
    return out;
}

// --- builders ----------------------------------------------------------------
std::string build_configure_xml(const ConfigureRequest& request) {
    std::string element = "<configure";
    if (!request.memory_name.empty()) {
        element += " MemoryName=\"" + xml_escape(request.memory_name) + "\"";
    }
    element += " MaxPayloadSizeToTargetInBytes=\"" + number(request.max_payload_size_to_target) + "\"";
    element += " Verbose=\"" + number(request.verbose ? 1 : 0) + "\"";
    element += " ZlpAwareHost=\"" + number(request.zero_length_packet_aware ? 1 : 0) + "\"";
    element += " SkipStorageInit=\"" + number(request.skip_storage_init ? 1 : 0) + "\"";
    element += "/>";
    return document(element);
}

std::string build_read_xml(const ReadRequest& request) {
    std::string element = "<read";
    element += " SECTOR_SIZE_IN_BYTES=\"" + number(request.sector_size_in_bytes) + "\"";
    element += " num_partition_sectors=\"" + number(request.num_partition_sectors) + "\"";
    element += " physical_partition_number=\"" + number(request.physical_partition_number) + "\"";
    element += " start_sector=\"" + number(request.start_sector) + "\"";
    if (!request.filename.empty()) {
        element += " filename=\"" + xml_escape(request.filename) + "\"";
    }
    element += "/>";
    return document(element);
}

std::string build_program_xml(const ProgramRequest& request) {
    std::string element = "<program";
    element += " SECTOR_SIZE_IN_BYTES=\"" + number(request.sector_size_in_bytes) + "\"";
    element += " num_partition_sectors=\"" + number(request.num_partition_sectors) + "\"";
    element += " physical_partition_number=\"" + number(request.physical_partition_number) + "\"";
    element += " start_sector=\"" + number(request.start_sector) + "\"";
    if (!request.filename.empty()) {
        element += " filename=\"" + xml_escape(request.filename) + "\"";
    }
    element += "/>";
    return document(element);
}

std::string build_erase_xml(const EraseRequest& request) {
    std::string element = "<erase";
    element += " SECTOR_SIZE_IN_BYTES=\"" + number(request.sector_size_in_bytes) + "\"";
    element += " num_partition_sectors=\"" + number(request.num_partition_sectors) + "\"";
    element += " physical_partition_number=\"" + number(request.physical_partition_number) + "\"";
    element += " start_sector=\"" + number(request.start_sector) + "\"";
    element += "/>";
    return document(element);
}

std::string build_power_xml(const PowerRequest& request) {
    std::string element = "<power";
    element += " value=\"" + xml_escape(request.value) + "\"";
    if (request.delay_seconds > 0) {
        element += " DelayInSeconds=\"" + number(request.delay_seconds) + "\"";
    }
    element += "/>";
    return document(element);
}

std::string build_get_storage_info_xml() {
    return document("<getstorageinfo physical_partition_number=\"0\"/>");
}

std::string build_ping_xml() {
    return document("<ping/>");
}

// --- document framing --------------------------------------------------------
std::vector<std::string> extract_documents(std::string& pending) {
    std::vector<std::string> documents;
    for (;;) {
        const std::size_t start = pending.find("<?xml");
        if (start == std::string::npos) {
            // Keep a short tail in case a prolog is split across reads, but do
            // not let non-XML noise accumulate forever.
            if (pending.size() > 5) {
                pending.erase(0, pending.size() - 5);
            }
            break;
        }
        if (start > 0) {
            pending.erase(0, start);
        }
        const std::size_t end = pending.find("</data>");
        if (end == std::string::npos) {
            break;  // incomplete document, wait for more bytes
        }
        const std::size_t length = end + 7;  // strlen("</data>")
        documents.push_back(pending.substr(0, length));
        pending.erase(0, length);
    }
    return documents;
}

// --- response parsing --------------------------------------------------------
FirehoseResponse parse_firehose_response(const std::string& xml) {
    FirehoseResponse response;
    response.raw = xml;

    if (xml.find("<?xml") == std::string::npos && xml.find("<data") == std::string::npos
        && xml.find("<response") == std::string::npos && xml.find("<log") == std::string::npos) {
        throw ProtocolError("response is not XML: "
                            + xml.substr(0, 80));
    }

    bool saw_response_element = false;
    std::size_t position = 0;
    while ((position = xml.find('<', position)) != std::string::npos) {
        const std::size_t close = xml.find('>', position);
        if (close == std::string::npos) {
            break;
        }
        std::string tag = xml.substr(position + 1, close - position - 1);
        position = close + 1;

        // Skip the prolog, comments, closing tags and any nested <data>.
        if (tag.empty() || tag[0] == '?' || tag[0] == '!' || tag[0] == '/') {
            continue;
        }
        const std::size_t name_end = tag.find_first_of(" \t\n\r/");
        const std::string name = tag.substr(0, name_end);
        if (name_end == std::string::npos) {
            continue;
        }
        tag = tag.substr(name_end);  // the attribute section only

        if (name == "log") {
            std::string value;
            if (attribute_value(tag, "value", value)) {
                response.logs.push_back(xml_unescape(value));
            }
        } else if (name == "response") {
            saw_response_element = true;
            std::string value;
            if (attribute_value(tag, "value", value)) {
                const std::string unescaped = xml_unescape(value);
                if (unescaped == "ACK") {
                    response.status = FirehoseStatus::Ack;
                } else if (unescaped == "NAK") {
                    response.status = FirehoseStatus::Nak;
                } else if (unescaped == "LOG") {
                    response.status = FirehoseStatus::Log;
                }
            }
            std::string rawmode;
            if (attribute_value(tag, "rawmode", rawmode)) {
                response.raw_mode = (xml_unescape(rawmode) == "true");
            }
        }
        // Every other element is ignored on purpose: the programmer adds
        // elements over time and refusing to parse an unknown one would break
        // against firmware we have not seen.
    }

    if (!saw_response_element) {
        // A document with only <log> elements is legal and means "still working".
        response.status = response.logs.empty() ? FirehoseStatus::Unknown : FirehoseStatus::Log;
    }
    return response;
}

// --- storage geometry --------------------------------------------------------
bool json_scalar(const std::string& object, const std::string& key, std::string& out) {
    // Narrow on purpose: find "key", require the next non-space character to be
    // ':', then read a number or a quoted string. It is not a JSON parser and
    // does not pretend to be - it cannot handle nesting, arrays, or escapes
    // beyond the simple ones. That is sufficient for the one blob the programmer
    // sends, and small enough to be sure of.
    const std::string needle = "\"" + key + "\"";
    std::size_t position = 0;
    while ((position = object.find(needle, position)) != std::string::npos) {
        std::size_t cursor = position + needle.size();
        while (cursor < object.size() && is_space(object[cursor])) {
            ++cursor;
        }
        if (cursor >= object.size() || object[cursor] != ':') {
            position = cursor;
            continue;  // the key appears as a value, not as a member
        }
        ++cursor;
        while (cursor < object.size() && is_space(object[cursor])) {
            ++cursor;
        }
        if (cursor >= object.size()) {
            return false;
        }
        if (object[cursor] == '"') {
            const std::size_t end = object.find('"', cursor + 1);
            if (end == std::string::npos) {
                return false;
            }
            out = object.substr(cursor + 1, end - cursor - 1);
            return true;
        }
        const std::size_t start = cursor;
        while (cursor < object.size()
               && (std::isdigit(static_cast<unsigned char>(object[cursor])) || object[cursor] == '.'
                   || object[cursor] == '-' || object[cursor] == '+')) {
            ++cursor;
        }
        if (cursor == start) {
            return false;
        }
        out = object.substr(start, cursor - start);
        return true;
    }
    return false;
}

bool extract_storage_info(const FirehoseResponse& response, StorageInfo& out) {
    for (const std::string& line : response.logs) {
        const std::size_t brace = line.find('{');
        if (brace == std::string::npos || line.find("total_blocks") == std::string::npos) {
            continue;
        }
        const std::string json = line.substr(brace);

        // Restrict to the storage_info object, so a "block_size" appearing
        // elsewhere in the blob cannot be mistaken for the geometry.
        std::string scope = json;
        const std::size_t info = json.find("storage_info");
        if (info != std::string::npos) {
            const std::size_t open = json.find('{', info);
            if (open != std::string::npos) {
                scope = json.substr(open);
            }
        }

        std::string blocks;
        std::string block_size;
        if (!json_scalar(scope, "total_blocks", blocks) || !json_scalar(scope, "block_size", block_size)) {
            continue;
        }

        out.raw_json = json;
        out.total_blocks = std::strtoull(blocks.c_str(), nullptr, 10);
        out.block_size = std::strtoull(block_size.c_str(), nullptr, 10);

        // Some programmers state the storage type and some do not, so a miss
        // here is not a failure.
        std::string type;
        if (json_scalar(json, "storage_type", type) || json_scalar(json, "memory_type", type)) {
            out.storage_type = type;
        }
        return out.total_blocks != 0 && out.block_size != 0;
    }
    return false;
}

// --- rawprogram / patch ------------------------------------------------------
namespace {

/// Collects every element with the given name, as raw tag text including its
/// attributes.
std::vector<std::string> elements_named(const std::string& xml, const std::string& name) {
    std::vector<std::string> found;
    const std::string opener = "<" + name;
    std::size_t position = 0;
    while ((position = xml.find(opener, position)) != std::string::npos) {
        const std::size_t next = position + opener.size();
        // "<program" must not match "<programmer": the name has to end here.
        if (next < xml.size() && !is_space(xml[next]) && xml[next] != '/' && xml[next] != '>') {
            position = next;
            continue;
        }
        const std::size_t close = xml.find('>', position);
        if (close == std::string::npos) {
            break;
        }
        found.push_back(xml.substr(position, close - position + 1));
        position = close + 1;
    }
    return found;
}

/// Reads an attribute, returning "" when it is absent.
std::string attr(const std::string& element, const std::string& name) {
    std::string value;
    return attribute_value(element, name, value) ? value : std::string{};
}

unsigned int attr_unsigned(const std::string& element, const std::string& name,
                           unsigned int fallback) {
    const std::string value = attr(element, name);
    if (value.empty()) {
        return fallback;
    }
    return static_cast<unsigned int>(std::strtoul(value.c_str(), nullptr, 0));
}

std::uint64_t attr_u64(const std::string& element, const std::string& name,
                       std::uint64_t fallback) {
    const std::string value = attr(element, name);
    if (value.empty()) {
        return fallback;
    }
    return std::strtoull(value.c_str(), nullptr, 0);
}

}  // namespace

std::vector<RawProgramEntry> parse_rawprogram_xml(const std::string& xml) {
    std::vector<RawProgramEntry> entries;
    for (const std::string& element : elements_named(xml, "program")) {
        RawProgramEntry entry;
        entry.program.sector_size_in_bytes = attr_unsigned(element, "SECTOR_SIZE_IN_BYTES", 512);
        entry.program.physical_partition_number = attr_unsigned(element, "physical_partition_number", 0);
        entry.program.start_sector = attr_u64(element, "start_sector", 0);
        entry.program.num_partition_sectors = attr_u64(element, "num_partition_sectors", 0);
        entry.program.filename = xml_unescape(attr(element, "filename"));
        entry.label = xml_unescape(attr(element, "label"));
        entries.push_back(std::move(entry));
    }

    if (entries.empty()) {
        throw ProtocolError(
            "no <program> element was found. A rawprogram file is a list of the partitions to "
            "write; this does not look like one.");
    }
    return entries;
}

std::vector<PatchEntry> parse_patch_xml(const std::string& xml) {
    std::vector<PatchEntry> entries;
    for (const std::string& element : elements_named(xml, "patch")) {
        PatchEntry entry;
        entry.sector_size_in_bytes = attr_unsigned(element, "SECTOR_SIZE_IN_BYTES", 512);
        entry.physical_partition_number = attr_unsigned(element, "physical_partition_number", 0);
        entry.start_sector = attr_u64(element, "start_sector", 0);
        entry.byte_offset = attr_unsigned(element, "byte_offset", 0);
        entry.size_in_bytes = attr_unsigned(element, "size_in_bytes", 0);
        entry.filename = xml_unescape(attr(element, "filename"));
        entry.value = xml_unescape(attr(element, "value"));
        entries.push_back(std::move(entry));
    }
    if (entries.empty()) {
        throw ProtocolError("no <patch> element was found in the document");
    }
    return entries;
}

std::string build_patch_xml(const PatchEntry& patch) {
    std::string element = "<patch";
    element += " SECTOR_SIZE_IN_BYTES=\"" + number(patch.sector_size_in_bytes) + "\"";
    element += " byte_offset=\"" + number(patch.byte_offset) + "\"";
    if (!patch.filename.empty()) {
        element += " filename=\"" + xml_escape(patch.filename) + "\"";
    }
    element += " physical_partition_number=\"" + number(patch.physical_partition_number) + "\"";
    element += " size_in_bytes=\"" + number(patch.size_in_bytes) + "\"";
    element += " start_sector=\"" + number(patch.start_sector) + "\"";
    element += " value=\"" + xml_escape(patch.value) + "\"";
    element += "/>";
    return document(element);
}

}  // namespace huaxin::protocols::qualcomm
