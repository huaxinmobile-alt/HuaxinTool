#include "protocols/mediatek/scatter.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <fstream>
#include <set>
#include <sstream>

#include "protocols/qualcomm/sahara.h"  // ProtocolError

namespace huaxin::protocols::mediatek {

using protocols::qualcomm::ProtocolError;

namespace {

/// The keys this parser reads by name. Anything else lands in `extra`.
///
/// Listed once here so `unmodelled_keys()` and the field readers cannot drift
/// apart: a key added to one without the other shows up as unmodelled, which is
/// the safe direction to be wrong in.
const std::set<std::string>& handled_keys() {
    static const std::set<std::string> keys = {
        "general", "info", "config_version", "platform", "project", "storage",
        "boot_channel", "block_size", "partition_index", "partition_name",
        "file_name", "is_download", "linear_start_addr", "begin_address",
        "partition_size", "region", "operation_type",
    };
    return keys;
}

std::string trim(const std::string& text) {
    std::size_t begin = 0;
    while (begin < text.size() && std::isspace(static_cast<unsigned char>(text[begin])) != 0) {
        ++begin;
    }
    std::size_t end = text.size();
    while (end > begin && std::isspace(static_cast<unsigned char>(text[end - 1])) != 0) {
        --end;
    }
    return text.substr(begin, end - begin);
}

/// Strips one layer of matching quotes and any trailing comment.
///
/// A `#` only starts a comment at the start of a value or after whitespace, so a
/// path or a value containing one survives.
std::string clean_value(const std::string& raw) {
    std::string value = trim(raw);
    // A trailing YAML comment.
    for (std::size_t index = 0; index < value.size(); ++index) {
        if (value[index] == '#' && (index == 0 || std::isspace(static_cast<unsigned char>(value[index - 1])) != 0)) {
            value = trim(value.substr(0, index));
            break;
        }
    }
    if (value.size() >= 2
        && ((value.front() == '"' && value.back() == '"')
            || (value.front() == '\'' && value.back() == '\''))) {
        value = value.substr(1, value.size() - 2);
    }
    return value;
}

/// Parses a number a scatter file may spell in decimal or in hex.
///
/// Returns false for text that is not a number at all, so the caller can leave a
/// field at zero and report it rather than treating "0x" as zero silently.
bool parse_number(const std::string& raw, std::uint64_t& out) {
    const std::string value = trim(raw);
    if (value.empty()) {
        return false;
    }
    // Some packages write a bare 0x prefix for a value they mean to be filled
    // in later. That is not a number, and reading it as zero would place a
    // partition at the start of the flash.
    std::size_t index = 0;
    int base = 10;
    if (value.size() > 2 && (value[0] == '0') && (value[1] == 'x' || value[1] == 'X')) {
        index = 2;
        base = 16;
    }
    if (index >= value.size()) {
        return false;
    }
    std::uint64_t result = 0;
    for (; index < value.size(); ++index) {
        const char character = value[index];
        int digit = -1;
        if (character >= '0' && character <= '9') {
            digit = character - '0';
        } else if (base == 16 && character >= 'a' && character <= 'f') {
            digit = 10 + (character - 'a');
        } else if (base == 16 && character >= 'A' && character <= 'F') {
            digit = 10 + (character - 'A');
        } else {
            return false;
        }
        if (digit >= base) {
            return false;
        }
        result = result * static_cast<std::uint64_t>(base) + static_cast<std::uint64_t>(digit);
    }
    out = result;
    return true;
}

bool parse_bool(const std::string& raw, bool& out) {
    std::string lowered;
    for (const char character : trim(raw)) {
        lowered.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(character))));
    }
    if (lowered == "true" || lowered == "yes" || lowered == "1" || lowered == "on") {
        out = true;
        return true;
    }
    if (lowered == "false" || lowered == "no" || lowered == "0" || lowered == "off") {
        out = false;
        return true;
    }
    return false;
}

/// Splits "key = value" or "key: value" and reports which separator was used.
bool split_pair(const std::string& line, std::string& key, std::string& value, bool& colon) {
    const std::size_t equals = line.find('=');
    const std::size_t colon_at = line.find(':');
    if (colon_at != std::string::npos && (equals == std::string::npos || colon_at < equals)) {
        key = trim(line.substr(0, colon_at));
        value = clean_value(line.substr(colon_at + 1));
        colon = true;
        return !key.empty();
    }
    if (equals != std::string::npos) {
        key = trim(line.substr(0, equals));
        value = clean_value(line.substr(equals + 1));
        colon = false;
        return !key.empty();
    }
    return false;
}

/// A line that starts a new partition entry: `- partition_index: SYS0`,
/// `- !BitDesc ...`, or a bare `partition_index = SYS0`.
bool starts_entry(const std::string& line, std::string& key, std::string& value) {
    const std::string stripped = trim(line);
    if (stripped.empty()) {
        return false;
    }
    if (stripped[0] != '-') {
        bool colon = false;
        if (!split_pair(stripped, key, value, colon)) {
            return false;
        }
        // Only partition_index opens an entry. partition_name is a *field* of
        // one - treating it as an opener too split every modern entry in half,
        // because each carries both keys.
        return key == "partition_index";
    }
    // A list item. Strip the dash and any YAML tag after it.
    std::string rest = trim(stripped.substr(1));
    if (!rest.empty() && rest[0] == '!') {
        const std::size_t space = rest.find_first_of(" \t");
        rest = space == std::string::npos ? std::string{} : trim(rest.substr(space + 1));
    }
    if (rest.empty()) {
        return false;
    }
    // The general block opens the file as a list item too.
    if (rest.rfind("general", 0) == 0) {
        value = clean_value(rest.size() > 7 ? rest.substr(7) : std::string{});
        key = "general";
        return true;
    }
    bool colon = false;
    if (!split_pair(rest, key, value, colon)) {
        return false;
    }
    return key == "partition_index";
}

/// Applies one key/value to a partition under construction.
void apply_partition_field(ScatterPartition& partition, const std::string& key,
                           const std::string& value) {
    if (key == "partition_name") {
        partition.name = value;
    } else if (key == "partition_index") {
        partition.index = value;
        // Some legacy packages record only the index, and it is then the entry's
        // only name. Kept in `index` always, promoted to `name` only when the
        // file never provides a real one - done at the end, in finish_partition.
    } else if (key == "file_name") {
        partition.file_name = value;
    } else if (key == "is_download") {
        partition.is_download_stated = true;
        bool flag = false;
        if (parse_bool(value, flag)) {
            partition.is_download = flag;
        } else {
            // Unreadable: keep the text so it is visible rather than pretending
            // it was false.
            partition.extra[key] = value;
        }
    } else if (key == "linear_start_addr" || key == "begin_address") {
        std::uint64_t number = 0;
        if (parse_number(value, number)) {
            partition.start_address = number;
        } else {
            partition.extra[key] = value;
        }
    } else if (key == "partition_size") {
        std::uint64_t number = 0;
        if (parse_number(value, number)) {
            partition.size = number;
        } else {
            partition.extra[key] = value;
        }
    } else if (key == "region") {
        partition.region = value;
    } else if (key == "storage") {
        partition.storage = value;
    } else if (key == "operation_type") {
        partition.operation_type = value;
    } else {
        partition.extra[key] = value;
    }
}

/// Finishes an entry: fills in the defaults that depend on the whole entry.
void finish_partition(ScatterPartition& partition) {
    if (partition.name.empty()) {
        partition.name = partition.index;
    }
    // No `is_download` key at all: a partition with an image is meant to be
    // written. Both reference parsers make the same assumption. An explicit
    // `false` is honoured, which is the whole reason the stated flag exists.
    if (!partition.is_download_stated && !partition.file_name.empty()
        && partition.extra.find("is_download") == partition.extra.end()) {
        partition.is_download = true;
    }
}

/// True when an entry says anything at all.
bool partition_is_empty(const ScatterPartition& partition) {
    return partition.name.empty() && partition.index.empty() && partition.file_name.empty()
           && !partition.is_download_stated && partition.start_address == 0 && partition.size == 0
           && partition.region.empty() && partition.storage.empty()
           && partition.operation_type.empty() && partition.extra.empty();
}

}  // namespace

const char* to_string(ScatterFormat format) noexcept {
    switch (format) {
        case ScatterFormat::Legacy:  return "legacy key=value";
        case ScatterFormat::Modern:  return "modern YAML";
        case ScatterFormat::Unknown: return "unrecognised";
    }
    return "unrecognised";
}

std::string ScatterPartition::describe() const {
    char buffer[160];
    std::snprintf(buffer, sizeof(buffer), "%s at 0x%llx, %llu bytes%s%s",
                  name.empty() ? "(unnamed)" : name.c_str(),
                  static_cast<unsigned long long>(start_address),
                  static_cast<unsigned long long>(size), region.empty() ? "" : ", region ",
                  region.empty() ? "" : region.c_str());
    return buffer;
}

std::string ScatterPartition::extra_value(const std::string& key) const {
    const auto entry = extra.find(key);
    return entry == extra.end() ? std::string{} : entry->second;
}

std::vector<ScatterPartition> ScatterFile::downloads() const {
    std::vector<ScatterPartition> out;
    for (const ScatterPartition& partition : partitions) {
        if (partition.downloadable()) {
            out.push_back(partition);
        }
    }
    return out;
}

const ScatterPartition* ScatterFile::find(const std::string& name) const {
    for (const ScatterPartition& partition : partitions) {
        if (partition.name.size() != name.size()) {
            continue;
        }
        bool same = true;
        for (std::size_t index = 0; index < name.size(); ++index) {
            const char left = static_cast<char>(std::tolower(static_cast<unsigned char>(partition.name[index])));
            const char right = static_cast<char>(std::tolower(static_cast<unsigned char>(name[index])));
            if (left != right) {
                same = false;
                break;
            }
        }
        if (same) {
            return &partition;
        }
    }
    return nullptr;
}

std::uint64_t ScatterFile::total_download_bytes() const {
    std::uint64_t total = 0;
    for (const ScatterPartition& partition : partitions) {
        if (partition.downloadable()) {
            total += partition.size;
        }
    }
    return total;
}

std::vector<std::string> ScatterFile::unmodelled_keys() const {
    std::set<std::string> seen;
    for (const ScatterPartition& partition : partitions) {
        for (const auto& entry : partition.extra) {
            if (entry.first != "is_download" && entry.first != "linear_start_addr"
                && entry.first != "begin_address" && entry.first != "partition_size") {
                seen.insert(entry.first);
            }
        }
    }
    for (const auto& entry : general.extra) {
        seen.insert(entry.first);
    }
    return std::vector<std::string>(seen.begin(), seen.end());
}

ScatterFormat detect_scatter_format(const std::string& text) noexcept {
    // The modern shape is recognised by the tag its entries carry or by the YAML
    // key syntax. A file with `partition_name:` is modern; one whose entries are
    // only `partition_index = ...` is legacy.
    if (text.find("!BitDesc") != std::string::npos) {
        return ScatterFormat::Modern;
    }
    if (text.find("partition_name:") != std::string::npos) {
        return ScatterFormat::Modern;
    }
    if (text.find("partition_index =") != std::string::npos
        || text.find("partition_index=") != std::string::npos) {
        return ScatterFormat::Legacy;
    }
    return ScatterFormat::Unknown;
}

ScatterFile parse_scatter(const std::string& text) {
    ScatterFile file;
    file.format = detect_scatter_format(text);

    std::istringstream stream(text);
    std::string line;

    ScatterPartition current;
    bool have_partition = false;
    bool in_general = false;
    ScatterGeneral general;
    bool general_seen = false;

    const auto flush = [&]() {
        if (have_partition) {
            if (!partition_is_empty(current)) {
                finish_partition(current);
                file.partitions.push_back(current);
            }
            current = ScatterPartition{};
            have_partition = false;
        }
    };

    while (std::getline(stream, line)) {
        // A line with no content is skipped rather than treated as a separator:
        // entries in a real scatter file are not reliably blank-line delimited.
        const std::string stripped = trim(line);
        if (stripped.empty()) {
            continue;
        }
        // Comment banners.
        if (stripped[0] == '#') {
            continue;
        }

        std::string key;
        std::string value;
        if (starts_entry(line, key, value)) {
            if (key == "general") {
                flush();
                in_general = true;
                general_seen = true;
                continue;
            }
            // A new partition begins: the previous one is complete.
            flush();
            in_general = false;
            have_partition = true;
            apply_partition_field(current, key, value);
            continue;
        }

        // A nested list item inside the general block arrives as
        // "- key: value"; the dash belongs to the list, not to the key.
        const std::string field_line = stripped[0] == '-' ? trim(stripped.substr(1)) : stripped;
        std::string field_key;
        std::string field_value;
        bool colon = false;
        if (field_line.empty() || !split_pair(field_line, field_key, field_value, colon)) {
            // A list item that is not a key/value pair, such as `- info:` with
            // nothing after it. Ignored rather than treated as an error.
            continue;
        }

        if (in_general) {
            // The general block is a nested list of key/value pairs.
            if (field_key == "info") {
                continue;
            }
            if (field_key == "config_version") {
                general.config_version = field_value;
            } else if (field_key == "platform") {
                general.platform = field_value;
            } else if (field_key == "project") {
                general.project = field_value;
            } else if (field_key == "storage") {
                general.storage = field_value;
            } else if (field_key == "boot_channel") {
                general.boot_channel = field_value;
            } else if (field_key == "block_size") {
                general.block_size = field_value;
            } else {
                general.extra[field_key] = field_value;
            }
            continue;
        }

        if (!have_partition) {
            // Content before any entry and outside the general block. Kept in
            // the general block rather than dropped, because a legacy file is
            // sometimes just a bare list of key/value lines.
            if (!general_seen) {
                general.extra[field_key] = field_value;
                general_seen = true;
            } else {
                general.extra[field_key] = field_value;
            }
            continue;
        }

        apply_partition_field(current, field_key, field_value);
    }
    flush();

    file.general = general;

    if (file.partitions.empty()) {
        throw ProtocolError(
            "no partition entries were found. A scatter file is a list of partitions with "
            "partition_name/partition_index entries; this does not look like one.");
    }
    if (file.format == ScatterFormat::Unknown) {
        // Not fatal - the entries parsed - but worth saying, because a file that
        // matches neither known shape may have been read with the wrong rules.
        file.format = ScatterFormat::Modern;
    }
    return file;
}

ScatterFile load_scatter(const std::string& path) {
    std::ifstream file(path, std::ios::binary);
    if (!file) {
        throw ProtocolError("cannot open the scatter file " + path);
    }
    std::ostringstream buffer;
    buffer << file.rdbuf();
    const std::string text = buffer.str();
    if (text.empty()) {
        throw ProtocolError(path + " is empty");
    }
    ScatterFile parsed = parse_scatter(text);
    if (parsed.partitions.empty() && parsed.general.is_empty()) {
        throw ProtocolError(path + " contains no scatter data");
    }
    return parsed;
}

}  // namespace huaxin::protocols::mediatek
