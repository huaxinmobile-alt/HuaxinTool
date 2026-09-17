#include "protocols/mediatek/mediatek_brom.h"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <utility>
#include <vector>

namespace huaxin::protocols::mediatek {

using protocols::qualcomm::ProtocolError;

namespace {

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
                            + std::to_string(limit) + " byte limit for a download agent");
    }
    file.seekg(0, std::ios::beg);
    std::vector<std::uint8_t> data(static_cast<std::size_t>(size));
    if (!file.read(reinterpret_cast<char*>(data.data()), size)) {
        throw ProtocolError("could not read all of " + path);
    }
    return data;
}

/// Reads an image that is about to be flashed. A separate limit from the agent
/// above because a partition image is legitimately far larger.
std::vector<std::uint8_t> read_image(const std::string& path) {
    std::ifstream file(path, std::ios::binary | std::ios::ate);
    if (!file) {
        throw ProtocolError("cannot open the image " + path);
    }
    const std::streamoff size = file.tellg();
    if (size <= 0) {
        throw ProtocolError(path + " is empty");
    }
    // Above this the image is held in memory by the transfer path, so reading it
    // is refused up front rather than half way through a flash.
    constexpr std::streamoff kMaxImage = 512ll * 1024ll * 1024ll;
    if (size > kMaxImage) {
        throw ProtocolError(path + " is " + std::to_string(size / (1024 * 1024))
                            + " MiB, above the " + std::to_string(kMaxImage / (1024 * 1024))
                            + " MiB this build holds in memory");
    }
    file.seekg(0, std::ios::beg);
    std::vector<std::uint8_t> data(static_cast<std::size_t>(size));
    if (!file.read(reinterpret_cast<char*>(data.data()), size)) {
        throw ProtocolError("could not read all of " + path);
    }
    return data;
}

std::string parent_directory(const std::string& path) {
    const std::size_t slash = path.find_last_of("/\\");
    return slash == std::string::npos ? std::string{"."} : path.substr(0, slash);
}

std::string join_path(const std::string& directory, const std::string& name) {
    if (name.empty()) {
        return name;
    }
    const bool absolute = name.size() > 1 && (name[0] == '/' || name[0] == '\\'
                                             || (std::isalpha(static_cast<unsigned char>(name[0])) != 0
                                                 && name[1] == ':'));
    if (absolute || directory.empty() || directory == ".") {
        return name;
    }
    return directory + "/" + name;
}

bool same_name(const std::string& left, const std::string& right) {
    if (left.size() != right.size()) {
        return false;
    }
    for (std::size_t index = 0; index < left.size(); ++index) {
        const char a = static_cast<char>(std::tolower(static_cast<unsigned char>(left[index])));
        const char b = static_cast<char>(std::tolower(static_cast<unsigned char>(right[index])));
        if (a != b) {
            return false;
        }
    }
    return true;
}

}  // namespace

MediaTekBrom::MediaTekBrom(Callbacks callbacks) : m_callbacks(std::move(callbacks)) {}

MediaTekBrom::~MediaTekBrom() {
    disconnect();
}

std::vector<std::string> MediaTekBrom::devices_present() {
    return usb::MtkTransport::devices_present();
}

void MediaTekBrom::disconnect() noexcept {
    // The agent lives on the same link, so it goes with it. Dropping it here
    // rather than leaving a dangling conversation is what stops a later call
    // from writing into a closed handle.
    m_agent.reset();
    m_agent_started = false;
    m_transport.close();
    m_handshaked = false;
}

void MediaTekBrom::ensure_connected() {
    if (m_transport.is_open()) {
        return;
    }
    connect();
}

void MediaTekBrom::connect() {
    if (m_transport.is_open()) {
        return;
    }
    m_transport.open();
    if (m_callbacks.log) {
        m_callbacks.log("ok", "opened " + m_transport.describe());
    }
}

BromSession::Callbacks MediaTekBrom::session_callbacks() const {
    return BromSession::Callbacks{m_callbacks.log, m_callbacks.progress, m_callbacks.cancelled};
}

void MediaTekBrom::handshake(bool send_lead_byte) {
    if (m_handshaked) {
        return;
    }
    ensure_connected();
    BromSession session(m_transport, session_callbacks());
    session.handshake(send_lead_byte);
    m_handshaked = true;
}

BromChipInfo MediaTekBrom::read_chip_info() {
    handshake();
    BromSession session(m_transport, session_callbacks());
    m_chip = session.read_chip_info();
    if (m_callbacks.log) {
        m_callbacks.log("debug",
                        "remaining chip identification (marketing name) is not implemented: the "
                        "hardware code is reported as a number and left at that");
    }
    return m_chip;
}

TargetConfig MediaTekBrom::read_target_config() {
    handshake();
    BromSession session(m_transport, session_callbacks());
    return session.read_target_config();
}

void MediaTekBrom::load_download_agent(const std::string& path, std::uint32_t load_address,
                                       std::uint32_t signature_length, bool start) {
    // 8 MiB is far above any real agent and low enough to catch the wrong file
    // being picked - a preloader or a firmware image rather than a DA.
    const std::vector<std::uint8_t> file = read_file(path, 8u * 1024u * 1024u);
    if (m_callbacks.log) {
        m_callbacks.log("info", "download agent " + path + " is " + std::to_string(file.size())
                                    + " bytes, loading at 0x"
                                    + [&] {
                                          char buffer[16];
                                          std::snprintf(buffer, sizeof(buffer), "%08x",
                                                        load_address);
                                          return std::string(buffer);
                                      }());
    }

    handshake();
    DownloadAgent agent;
    agent.payload = build_da_payload(file);
    agent.code_length = file.size() - std::min<std::size_t>(signature_length, file.size());
    agent.signature_length = signature_length;
    agent.load_address = load_address;

    BromSession session(m_transport, session_callbacks());
    session.upload_download_agent(agent, start);
    if (start) {
        m_agent_started = true;
        // Anything the agent says from here on is a different protocol on the
        // same wire, so a stale conversation would be worse than none.
        m_agent.reset();
    }
}

DaSession& MediaTekBrom::agent() {
    if (!m_agent) {
        DaSession::Callbacks callbacks;
        callbacks.log = m_callbacks.log;
        callbacks.cancelled = m_callbacks.cancelled;
        // The percent-only callback the other protocols use is not what the
        // flash operations report through; they use set_flash_progress. Bridging
        // it anyway means a caller that only set the old one still sees
        // something move rather than nothing at all.
        callbacks.progress = [this](int percent, const std::string& message) {
            if (m_flash_progress && percent >= 0) {
                FlashProgress event;
                event.phase = "working";
                event.percent = percent;
                m_flash_progress(event);
            } else if (m_callbacks.progress) {
                m_callbacks.progress(percent, message);
            }
        };
        m_agent = std::make_unique<DaSession>(m_transport, std::move(callbacks));
    }
    return *m_agent;
}

void MediaTekBrom::ensure_agent() {
    if (!m_agent_started) {
        throw ProtocolError(
            "no download agent is running: the bootrom can only load one and jump to it. "
            "Upload the agent first (handshake, then the DA upload), and only then can "
            "flashing commands be sent.");
    }
}

FlashInfo MediaTekBrom::read_flash_info() {
    ensure_agent();
    DaSession& session = agent();

    m_flash.da_version.clear();
    session.get_da_version(m_flash.da_version);

    m_flash.connection_agent = session.get_connection_agent();
    if (!m_flash.connection_agent.empty() && m_callbacks.log) {
        m_callbacks.log("ok", "the agent took over from the " + m_flash.connection_agent);
    }

    const bool emmc_ok = [&] {
        try {
            m_flash.emmc = session.get_emmc_info();
            return m_flash.emmc.type != 0;
        } catch (const ProtocolError& error) {
            if (m_callbacks.log) {
                m_callbacks.log("warn", std::string("the agent did not report eMMC geometry: ")
                                            + error.what());
            }
            return false;
        }
    }();
    if (emmc_ok) {
        m_flash.have_storage = true;
        m_flash.storage = DaStorage::Emmc;
        m_flash.total_size = m_flash.emmc.user_size;
        m_flash.block_size = m_flash.emmc.block_size;
    }

    if (!emmc_ok) {
        // Not an eMMC target, or an agent that answers eMMC with a zero type.
        // The other three are asked in turn rather than assumed.
        try {
            m_flash.nand = session.get_nand_info();
            if (m_flash.nand.type != 0) {
                m_flash.have_storage = true;
                m_flash.storage = DaStorage::Nand;
                m_flash.total_size = m_flash.nand.total_size;
                m_flash.block_size = m_flash.nand.block_size;
            }
        } catch (const ProtocolError&) {
            // A miss here is expected on an eMMC device and is not worth a line
            // of its own; the summary below reports what was found.
        }
    }
    if (!m_flash.have_storage) {
        try {
            m_flash.nor = session.get_nor_info();
            if (m_flash.nor.type != 0) {
                m_flash.have_storage = true;
                m_flash.storage = DaStorage::Nor;
                m_flash.total_size = m_flash.nor.available_size;
            }
        } catch (const ProtocolError&) {
        }
    }

    try {
        m_flash.ram = session.get_ram_info();
    } catch (const ProtocolError&) {
    }

    try {
        session.get_packet_length();
    } catch (const ProtocolError&) {
    }

    // A refusal is a normal answer here: on a non-secure device many agents do
    // not implement the query at all.
    std::uint32_t status = 0;
    session.device_control(DaControlCode::SlaEnabledStatus, {}, status);
    m_flash.sla_enabled = status == 0;

    if (m_callbacks.log) {
        m_callbacks.log(m_flash.have_storage ? "ok" : "warn", m_flash.describe());
    }
    return m_flash;
}

std::uint64_t MediaTekBrom::write_region(const std::vector<std::uint8_t>& image,
                                         std::uint64_t address, DaStorage storage,
                                         std::uint32_t partition) {
    ensure_agent();
    ProgressPump pump(m_flash_progress, "writing", std::string{}, image.size(), 0, 0);
    const std::uint64_t written = agent().write_region(storage, partition, address, image);
    pump.update(written, true);
    return written;
}

std::vector<std::uint8_t> MediaTekBrom::read_region(std::uint64_t address, std::uint64_t length,
                                                    DaStorage storage, std::uint32_t partition) {
    ensure_agent();
    ProgressPump pump(m_flash_progress, "reading", std::string{}, length, 0, 0);
    std::vector<std::uint8_t> data = agent().read_region(storage, partition, address, length);
    pump.update(data.size(), true);
    return data;
}

void MediaTekBrom::erase_region(std::uint64_t address, std::uint64_t length, DaStorage storage,
                                std::uint32_t partition) {
    ensure_agent();
    ProgressPump pump(m_flash_progress, "erasing", std::string{}, length, 0, 0);
    agent().format_region(storage, partition, address, length);
    pump.update(length, true);
}

std::uint64_t MediaTekBrom::flash_partition(const ScatterPartition& entry,
                                            const std::string& image_path) {
    const std::vector<std::uint8_t> image = read_image(image_path);
    return write_region(image, entry.start_address, DaStorage::Emmc,
                        static_cast<std::uint32_t>(EmmcPartition::User));
}

FlashResult MediaTekBrom::flash_scatter(const std::string& scatter_path,
                                        const std::string& image_dir, bool continue_on_error,
                                        const std::vector<std::string>& only) {
    ensure_agent();

    const ScatterFile scatter = load_scatter(scatter_path);
    const std::string directory = image_dir.empty() ? parent_directory(scatter_path) : image_dir;

    std::vector<ScatterPartition> wanted;
    for (const ScatterPartition& entry : scatter.downloads()) {
        if (!only.empty()
            && std::none_of(only.begin(), only.end(), [&](const std::string& name) {
                   return same_name(name, entry.name) || same_name(name, entry.file_name);
               })) {
            continue;
        }
        wanted.push_back(entry);
    }
    if (wanted.empty()) {
        throw ProtocolError("nothing to write: " + scatter_path
                            + " lists no downloadable partition that matches the selection");
    }

    // Every image is checked before the first byte is written. A package with a
    // missing file must fail before the device is modified, not half way
    // through - the partitions written before the discovery would be new while
    // the rest are old, which is the worst state to hand back.
    std::vector<std::string> missing;
    for (const ScatterPartition& entry : wanted) {
        const std::string path = join_path(directory, entry.file_name);
        std::ifstream probe(path, std::ios::binary);
        if (!probe) {
            missing.push_back(entry.file_name);
        }
    }
    if (!missing.empty()) {
        std::string list;
        for (std::size_t index = 0; index < missing.size() && index < 5; ++index) {
            list += (index == 0 ? "" : ", ") + missing[index];
        }
        throw ProtocolError("refusing to start: " + std::to_string(missing.size())
                            + " image file(s) named by " + scatter_path + " are not in "
                            + directory + " (" + list + "). Nothing was written.");
    }

    if (m_callbacks.log) {
        m_callbacks.log("info", "flashing " + std::to_string(wanted.size()) + " partition(s) from "
                                    + scatter_path + " ("
                                    + to_string(scatter.format) + ", "
                                    + format_bytes(scatter.total_download_bytes()) + " claimed)");
    }

    FlashResult result;
    for (std::size_t index = 0; index < wanted.size(); ++index) {
        const ScatterPartition& entry = wanted[index];
        const std::string path = join_path(directory, entry.file_name);

        PartitionOutcome outcome;
        outcome.name = entry.name;
        outcome.file_name = entry.file_name;

        if (m_callbacks.cancelled && m_callbacks.cancelled()) {
            outcome.skipped = true;
            outcome.error = "cancelled before this partition was reached";
            result.partitions.push_back(outcome);
            if (m_callbacks.log) {
                m_callbacks.log("warn", "cancelled at " + entry.name);
            }
            break;
        }

        try {
            const std::vector<std::uint8_t> image = read_image(path);
            outcome.bytes = image.size();

            if (m_callbacks.log) {
                m_callbacks.log("info", "[" + std::to_string(index + 1) + "/"
                                            + std::to_string(wanted.size()) + "] "
                                            + entry.name + ": " + format_bytes(image.size())
                                            + " to 0x"
                                            + [&] {
                                                  char buffer[24];
                                                  std::snprintf(buffer, sizeof(buffer), "%llx",
                                                                static_cast<unsigned long long>(
                                                                    entry.start_address));
                                                  return std::string(buffer);
                                              }());
            }

            ProgressPump pump(m_flash_progress, "writing", entry.name, image.size(), index + 1,
                              wanted.size());
            const std::uint64_t written =
                agent().write_region(DaStorage::Emmc,
                                     static_cast<std::uint32_t>(EmmcPartition::User),
                                     entry.start_address, image);
            pump.update(written, true);
            outcome.bytes = written;
            outcome.succeeded = true;
            if (m_callbacks.log) {
                m_callbacks.log("ok", entry.name + ": wrote " + format_bytes(written));
            }
        } catch (const ProtocolError& error) {
            outcome.error = error.what();
            if (m_callbacks.log) {
                m_callbacks.log("error", entry.name + ": " + outcome.error);
            }
        }

        result.partitions.push_back(outcome);

        if (!outcome.succeeded && !continue_on_error) {
            if (m_callbacks.log) {
                m_callbacks.log("error",
                                "stopping after " + entry.name + ". The partitions written "
                                "before this point are on the device and the ones after it are "
                                "not; write the whole package again once the cause is fixed "
                                "rather than continuing from here.");
            }
            break;
        }
    }

    result.completed = result.failed_count() == 0 && result.written_count() == wanted.size();
    if (m_callbacks.log) {
        m_callbacks.log(result.completed ? "ok" : "warn", result.summary());
    }
    return result;
}

std::uint64_t MediaTekBrom::read_back(const std::string& scatter_path,
                                      const std::string& partition_name,
                                      const std::string& destination) {
    ensure_agent();
    const ScatterFile scatter = load_scatter(scatter_path);
    const ScatterPartition* entry = scatter.find(partition_name);
    if (entry == nullptr) {
        throw ProtocolError(partition_name + " is not in " + scatter_path
                            + "; the partitions it lists are the ones that can be read back");
    }
    if (entry->size == 0) {
        throw ProtocolError(entry->name + " has no size in " + scatter_path
                            + ", so there is no way to know how much to read");
    }

    ProgressPump pump(m_flash_progress, "reading", entry->name, entry->size, 0, 0);
    const std::vector<std::uint8_t> data =
        agent().read_region(DaStorage::Emmc, static_cast<std::uint32_t>(EmmcPartition::User),
                            entry->start_address, entry->size);
    pump.update(data.size(), true);

    std::ofstream out(destination, std::ios::binary | std::ios::trunc);
    if (!out) {
        throw ProtocolError("cannot write " + destination);
    }
    out.write(reinterpret_cast<const char*>(data.data()),
              static_cast<std::streamsize>(data.size()));
    if (!out) {
        throw ProtocolError("could not write all of " + destination);
    }
    if (m_callbacks.log) {
        m_callbacks.log("ok", "wrote " + format_bytes(data.size()) + " to " + destination);
    }
    return data.size();
}

std::uint64_t MediaTekBrom::format_scatter(const std::string& scatter_path,
                                           const std::vector<std::string>& only) {
    ensure_agent();
    const ScatterFile scatter = load_scatter(scatter_path);

    // A partition with no image is a region the package means to clear: there is
    // nothing to write there, and leaving the old contents would keep stale data
    // on a device being rebuilt.
    std::vector<ScatterPartition> targets;
    for (const ScatterPartition& entry : scatter.partitions) {
        if (entry.downloadable() || entry.size == 0) {
            continue;
        }
        if (!only.empty()
            && std::none_of(only.begin(), only.end(), [&](const std::string& name) {
                   return same_name(name, entry.name);
               })) {
            continue;
        }
        targets.push_back(entry);
    }
    if (targets.empty()) {
        throw ProtocolError("nothing to format: " + scatter_path
                            + " lists no partition without an image");
    }

    std::uint64_t erased_total = 0;
    for (std::size_t index = 0; index < targets.size(); ++index) {
        const ScatterPartition& entry = targets[index];
        if (m_callbacks.cancelled && m_callbacks.cancelled()) {
            throw ProtocolError("cancelled before " + entry.name + " was erased");
        }
        if (m_callbacks.log) {
            m_callbacks.log("info", "[" + std::to_string(index + 1) + "/"
                                        + std::to_string(targets.size()) + "] erasing "
                                        + entry.name + " (" + format_bytes(entry.size) + ")");
        }
        ProgressPump pump(m_flash_progress, "erasing", entry.name, entry.size, index + 1,
                          targets.size());
        agent().format_region(DaStorage::Emmc, static_cast<std::uint32_t>(EmmcPartition::User),
                              entry.start_address, entry.size);
        pump.update(entry.size, true);
        erased_total += entry.size;
    }
    if (m_callbacks.log) {
        m_callbacks.log("ok", "erased " + std::to_string(targets.size()) + " partition(s), "
                                  + format_bytes(erased_total));
    }
    return erased_total;
}

void MediaTekBrom::shutdown_device(unsigned int mode) {
    ensure_agent();
    agent().shutdown(mode);
    // The device re-enumerates, so the agent conversation is over whatever it
    // answers.
    m_agent.reset();
    m_agent_started = false;
}

}  // namespace huaxin::protocols::mediatek
