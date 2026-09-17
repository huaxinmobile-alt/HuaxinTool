#pragma once

// =============================================================================
//  MediaTek scatter file parser.
//
//  A scatter file ("MTxxxx_Android_scatter.txt") is the package's map: it lists
//  the partitions, where each one starts, how big it is and which image file
//  belongs in it. SP Flash Tool reads it, and so does this tool.
//
//  PROVENANCE - the format is not published as a specification. The two shapes
//  below were taken from two independent open-source parsers rather than from
//  memory:
//
//    * ersascape/mtk-fastboot-scatter (GPL-3.0), tools/parse.py - loads the file
//      as YAML, treats element 0 as the general block, and reads `is_download`,
//      `partition_name` and `file_name` from the rest. That confirms the modern
//      shape is a YAML sequence and fixes three field names.
//    * Rafyal/AutoFlash-Tool (MIT), src/backend/scatter_parser.py - detects the
//      format by looking for "- !BitDesc" / "partition_name:", parses the modern
//      form as `key: value` stanzas and the legacy form as `key = value` blocks
//      split on partition_index, and reads `partition_index`, `partition_name`,
//      `linear_start_addr` (legacy also `begin_address`), `partition_size`,
//      `file_name`, `is_download`, `region` and `storage`.
//
//  WHAT THAT MEANS FOR THIS PARSER. Those field names are the ones treated as
//  meaningful. Everything else a package carries - `type`, `physical_start_addr`,
//  `boundary_check`, `is_reserved`, `operation_type`, `reserve`, and any vendor
//  addition - is preserved verbatim in ScatterPartition::extra and shown as-is.
//  Nothing is assigned a meaning that has not been confirmed, and nothing the
//  file says is dropped. `operation_type` is exposed by name because it is part
//  of the task's field list, but its *values* are passed through as text: the
//  value set is not verified here.
//
//  Neither shape is validated as YAML by a library. Both are read by a small
//  line-oriented scanner, so a package that is almost-but-not-quite YAML still
//  parses and the tool does not grow a YAML dependency for one file format.
// =============================================================================

#include <cstddef>
#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace huaxin::protocols::mediatek {

/// Which of the two shapes a file is in.
enum class ScatterFormat {
    Unknown,
    /// `key = value` blocks, split on partition_index. The older layout.
    Legacy,
    /// YAML `key: value` stanzas, tagged or untagged. The current layout.
    Modern,
};

const char* to_string(ScatterFormat format) noexcept;

/// The general block at the top of the file.
///
/// Only the keys that are both present and meaningful are filled; the rest of
/// the block is preserved in `extra` the same way a partition's is.
struct ScatterGeneral {
    std::string config_version;
    std::string platform;
    std::string project;
    std::string storage;
    std::string boot_channel;
    std::string block_size;
    std::map<std::string, std::string> extra;

    /// True when the general block stated anything at all.
    bool is_empty() const noexcept {
        return config_version.empty() && platform.empty() && project.empty()
               && storage.empty() && boot_channel.empty() && block_size.empty() && extra.empty();
    }
};

/// One partition entry.
struct ScatterPartition {
    /// `partition_name`, falling back to `partition_index` when the file has no
    /// separate name - which is how the legacy shape often records a bootloader.
    std::string name;
    /// `partition_index`, e.g. SYS0. Kept because it is the file's own ordering
    /// key and the only identity a nameless entry has.
    std::string index;
    /// `file_name`. May be empty: a partition can be listed with no image.
    std::string file_name;
    /// `is_download`. When the key is absent it defaults to "a file_name was
    /// given", which is what both reference parsers do.
    bool is_download{false};
    /// True when the file stated is_download at all.
    ///
    /// Without this, `is_download: false` is indistinguishable from the key
    /// being missing, and the default would flip an entry the package explicitly
    /// marked as "do not write" - which is the one direction that must not be
    /// got wrong.
    bool is_download_stated{false};
    /// `linear_start_addr`, or `begin_address` in the legacy shape. Bytes.
    std::uint64_t start_address{0};
    /// `partition_size`. Bytes.
    std::uint64_t size{0};
    /// `region`, e.g. EMMC_USER or EMMC_BOOT_1. Empty when the file is silent,
    /// which means the user area.
    std::string region;
    /// `storage`, e.g. HW_STORAGE_EMMC. Empty when the file is silent.
    std::string storage;
    /// `operation_type`, passed through as text. Its values are not interpreted:
    /// the set is not verified, and acting on a misread one would mean writing
    /// to the wrong place.
    std::string operation_type;

    /// Every other key in the entry, in file order, exactly as written. This is
    /// what keeps `type`, `physical_start_addr`, `boundary_check`, `reserve` and
    /// any vendor extension visible without this parser claiming to know what
    /// they mean.
    std::map<std::string, std::string> extra;

    /// True when the entry names an image that should be written.
    bool downloadable() const noexcept { return is_download && !file_name.empty(); }

    /// Where this partition sits, as a human-readable string, for the log and
    /// the UI.
    std::string describe() const;

    /// The value of an extra key, or an empty string.
    std::string extra_value(const std::string& key) const;
};

/// A parsed scatter file.
struct ScatterFile {
    ScatterFormat format{ScatterFormat::Unknown};
    ScatterGeneral general;
    std::vector<ScatterPartition> partitions;

    /// The partitions that name a file to write, in file order.
    std::vector<ScatterPartition> downloads() const;

    /// Looks up a partition by name. Case-insensitive, because packages are
    /// inconsistent about it and a partition name is not case-sensitive to the
    /// device either.
    const ScatterPartition* find(const std::string& name) const;

    /// Total bytes the downloadable entries claim.
    std::uint64_t total_download_bytes() const;

    /// Everything the parser did not recognise, keyed by the entry it came from,
    /// so the UI can say "this package has fields this build does not model"
    /// rather than silently ignoring them.
    std::vector<std::string> unmodelled_keys() const;
};

/// Parses scatter text.
///
/// Throws ProtocolError when no partition is found, which is the signature of
/// the wrong file being selected rather than of an empty package.
ScatterFile parse_scatter(const std::string& text);

/// Reads a scatter file from disk and parses it. Throws ProtocolError when the
/// file cannot be read.
ScatterFile load_scatter(const std::string& path);

/// Detects which shape a file is in without parsing it.
ScatterFormat detect_scatter_format(const std::string& text) noexcept;

}  // namespace huaxin::protocols::mediatek
