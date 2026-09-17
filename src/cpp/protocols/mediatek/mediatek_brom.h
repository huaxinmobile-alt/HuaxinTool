#pragma once

// =============================================================================
//  MediaTek BROM session: transport + handshake + download agent in one object.
//
//  Ordering, which the bootrom enforces and this facade makes explicit:
//
//      1. the device is in BROM or preloader mode (its USB ID says which)
//      2. the host performs the complemented-echo handshake
//      3. chip identity and target configuration can then be read
//      4. SEND_DA uploads the agent, JUMP_DA starts it
//      5. from there the conversation is the *DA's* protocol, not the bootrom's
//
//  Steps 2-4 happen once per connection. A failed or interrupted upload leaves
//  the bootrom in an undefined state; power-cycle the device before retrying.
// =============================================================================

#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

#include "protocols/mediatek/brom.h"
#include "protocols/mediatek/da.h"
#include "protocols/mediatek/mediatek_flash.h"
#include "protocols/mediatek/scatter.h"
#include "usb/mtk_transport.h"

namespace huaxin::protocols::mediatek {

class MediaTekBrom {
public:
    struct Callbacks {
        std::function<void(const std::string& level, const std::string& message)> log;
        std::function<void(int percent, const std::string& message)> progress;
        std::function<bool()> cancelled;
    };

    explicit MediaTekBrom(Callbacks callbacks = {});
    ~MediaTekBrom();

    MediaTekBrom(const MediaTekBrom&) = delete;
    MediaTekBrom& operator=(const MediaTekBrom&) = delete;
    MediaTekBrom(MediaTekBrom&&) = delete;
    MediaTekBrom& operator=(MediaTekBrom&&) = delete;

    /// MediaTek USB IDs currently on the bus, as "0e8d:0003" strings.
    static std::vector<std::string> devices_present();

    void connect();
    void disconnect() noexcept;
    bool connected() const noexcept { return m_transport.is_open(); }
    std::string describe() const { return m_transport.describe(); }

    /// Which boot stage is attached, from its USB ID.
    usb::MtkMode mode() const noexcept { return m_transport.mode(); }

    /// Runs the handshake, once. Idempotent: calling it again does nothing.
    void handshake(bool send_lead_byte = false);
    bool handshaked() const noexcept { return m_handshaked; }

    /// Reads the hardware code, version block and BROM version.
    BromChipInfo read_chip_info();

    /// Reads the bootrom's security configuration.
    TargetConfig read_target_config();

    /// Loads an agent file from disk and uploads it, optionally starting it.
    ///
    /// `signature_length` is the size of the trailing signature inside the file,
    /// which the bootrom is told about separately. Defaults to 0 for agents that
    /// are not signed, which is the case for the ones this project expects.
    void load_download_agent(const std::string& path,
                             std::uint32_t load_address,
                             std::uint32_t signature_length = 0,
                             bool start = true);

    /// The identity from the last read, for the UI.
    const BromChipInfo& chip_info() const noexcept { return m_chip; }

    // -- download agent ----------------------------------------------------
    // Everything below needs a running agent: handshake, then an upload that
    // starts it. The methods say so rather than sending a DA command into a
    // bootrom that only knows how to receive an image.

    /// True once an agent has been uploaded and started.
    bool agent_running() const noexcept { return m_agent_started; }

    /// Reads the agent's version, which stage it took over from, the storage
    /// geometry and the negotiated packet sizes.
    ///
    /// Nothing here is fatal on its own: an older agent that does not implement
    /// GET_DA_VERSION or one of the geometry queries still flashes, so each
    /// answer is reported and a missing one is a gap in the report rather than
    /// a failure.
    FlashInfo read_flash_info();

    /// The info from the last read_flash_info(), for the UI.
    const FlashInfo& flash_info() const noexcept { return m_flash; }

    /// Installs the rich progress callback used by the flash operations.
    void set_flash_progress(FlashProgressCallback callback) {
        m_flash_progress = std::move(callback);
    }

    // -- storage operations ------------------------------------------------

    /// Writes one image to one region.
    ///
    /// `address` is in bytes, which is what a scatter file carries; the agent's
    /// own unit conversion, if any, is its business. Returns the bytes written,
    /// including any sector padding.
    std::uint64_t write_region(const std::vector<std::uint8_t>& image, std::uint64_t address,
                               DaStorage storage = DaStorage::Emmc,
                               std::uint32_t partition = static_cast<std::uint32_t>(EmmcPartition::User));

    /// Reads a region, for a backup or a verification read-back.
    std::vector<std::uint8_t> read_region(std::uint64_t address, std::uint64_t length,
                                          DaStorage storage = DaStorage::Emmc,
                                          std::uint32_t partition = static_cast<std::uint32_t>(EmmcPartition::User));

    /// Erases a region.
    void erase_region(std::uint64_t address, std::uint64_t length,
                      DaStorage storage = DaStorage::Emmc,
                      std::uint32_t partition = static_cast<std::uint32_t>(EmmcPartition::User));

    /// Writes an image file to the region a scatter entry names.
    std::uint64_t flash_partition(const ScatterPartition& entry, const std::string& image_path);

    /// Replays every downloadable entry of a scatter file.
    ///
    /// Image paths are resolved against `image_dir`, which is normally the
    /// scatter file's own directory - that is how these packages are laid out.
    ///
    /// On failure the run stops by default. That is the deliberate choice:
    /// carrying on after a failed write leaves a device that is part new and
    /// part old, which is harder to recover than one that failed early and said
    /// where. `continue_on_error` exists for the case where the operator wants
    /// the rest regardless, and the result says exactly what was and was not
    /// written either way.
    FlashResult flash_scatter(const std::string& scatter_path, const std::string& image_dir,
                              bool continue_on_error = false,
                              const std::vector<std::string>& only = {});

    /// Reads a partition named by a scatter file out to a file.
    std::uint64_t read_back(const std::string& scatter_path, const std::string& partition_name,
                            const std::string& destination);

    /// Erases every partition named by a scatter file that has no image - the
    /// "format" half of a firmware upgrade. Returns the bytes erased.
    std::uint64_t format_scatter(const std::string& scatter_path,
                                 const std::vector<std::string>& only = {});

    /// Tells the agent to shut the device down. The link is not usable after.
    void shutdown_device(unsigned int mode = 0);

private:
    BromSession::Callbacks session_callbacks() const;
    void ensure_connected();
    void ensure_agent();

    /// The live agent conversation, created on first use after JUMP_DA.
    DaSession& agent();

    usb::MtkTransport m_transport;
    Callbacks m_callbacks;
    BromChipInfo m_chip;
    bool m_handshaked{false};
    bool m_agent_started{false};
    std::unique_ptr<DaSession> m_agent;
    FlashInfo m_flash;
    FlashProgressCallback m_flash_progress;
};

/// Default SRAM address a download agent is loaded to on current chipsets.
///
/// TODO: verify against a primary source per chipset. The bootrom accepts an
/// address supplied by the host, and different chip generations use different
/// SRAM layouts, so this is a caller-visible default rather than a hidden
/// constant - the UI asks for it.
inline constexpr std::uint32_t kDefaultDaLoadAddress = 0x00200000;

}  // namespace huaxin::protocols::mediatek
