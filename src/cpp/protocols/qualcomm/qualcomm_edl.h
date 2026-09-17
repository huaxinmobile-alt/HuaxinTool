#pragma once

// =============================================================================
//  Qualcomm EDL session: Sahara handshake + Firehose in one object.
//
//  This is the facade the UI drives. It owns the USB link and enforces the
//  ordering the device requires:
//
//      1. the device is in EDL mode and announces itself with SAHARA_HELLO_REQ
//      2. the host may enter Sahara command mode to read chip identity, then
//         either returns to image transfer (to upload) or resets (to finish)
//      3. the host uploads a Firehose programmer through Sahara's image transfer
//      4. only then does the device accept Firehose XML commands
//
//  Steps 1-3 happen once per device connection. Step 3 is what turns the device
//  from "waiting for an image" into "accepting Firehose commands", so no
//  Firehose call can work before a programmer has been uploaded.
// =============================================================================

#include <cstddef>
#include <functional>
#include <string>

#include <cstdint>
#include <vector>

#include "protocols/qualcomm/firehose.h"
#include "protocols/qualcomm/gpt.h"
#include "protocols/qualcomm/sahara.h"
#include "core/flash_timeouts.h"
#include "usb/edl_transport.h"

namespace huaxin::protocols::qualcomm {

class QualcommEdl {
public:
    struct Callbacks {
        std::function<void(const std::string& level, const std::string& message)> log;
        std::function<void(int percent, const std::string& message)> progress;
        std::function<bool()> cancelled;
    };

    explicit QualcommEdl(Callbacks callbacks = {});
    ~QualcommEdl();

    QualcommEdl(const QualcommEdl&) = delete;
    QualcommEdl& operator=(const QualcommEdl&) = delete;
    QualcommEdl(QualcommEdl&&) = delete;
    QualcommEdl& operator=(QualcommEdl&&) = delete;

    /// True when a 05c6:9008 device is on the bus, openable or not.
    static bool device_present();

    // -- link --------------------------------------------------------------

    /// Opens the USB link to the EDL device. Throws ProtocolError with an
    /// actionable message (including the Windows driver hint) on failure.
    void connect();

    /// Closes the link. Safe to call repeatedly.
    void disconnect() noexcept;

    bool connected() const noexcept { return m_transport.is_open(); }

    std::string describe() const { return m_transport.describe(); }

    // -- Sahara ------------------------------------------------------------

    /// Reads chip identity (serial, MSM id, OEM PK hash) and leaves the device
    /// back in its initial EDL state. No programmer is uploaded.
    ///
    /// Throws ProtocolError if the handshake fails.
    SaharaDeviceInfo read_device_info();

    /// Uploads a Firehose programmer file and leaves the device in Firehose
    /// mode. The chip identity is read first when `read_identity` is true.
    ///
    /// Returns the identity, or an empty struct when `read_identity` is false.
    SaharaDeviceInfo load_programmer(const std::string& path, bool read_identity = true);

    /// True once a programmer is loaded and Firehose commands will be accepted.
    bool programmer_loaded() const noexcept { return m_programmer_loaded; }

    /// Identity from the most recent Sahara conversation.
    const SaharaDeviceInfo& device_info() const noexcept { return m_info; }

    // -- Firehose ----------------------------------------------------------

    /// Sends one XML document and returns the parsed response.
    /// Throws ProtocolError when no programmer is loaded.
    FirehoseResponse send_firehose(const std::string& xml,
                                   unsigned int timeout_ms = core::command_timeout_ms(10000));

    /// <configure>: negotiates payload size and storage before anything else.
    FirehoseResponse configure(const ConfigureRequest& request,
                               unsigned int timeout_ms = core::command_timeout_ms(5000));

    /// <power value="reset"> - restarts the device out of EDL.
    FirehoseResponse power(const PowerRequest& request);


    /// <ping>. The cheapest way to confirm Firehose is alive.
    FirehoseResponse ping();

    /// The last raw response, for the log console.
    const std::string& last_response() const noexcept { return m_last_response; }

    // -- Firehose operations ----------------------------------------------
    // Every one of these retries a NAK up to kCommandAttempts times before
    // giving up. A NAK is usually transient (the programmer is not ready yet);
    // a protocol error or a disconnect is not, and is raised immediately.

    /// <getstorageinfo>: the geometry of a LUN. Throws when the programmer
    /// answers without one.
    StorageInfo get_storage_info(unsigned int lun = 0);

    /// Reads and parses the GPT of a LUN.
    ///
    /// Read in two steps because they are separate flash regions: the header at
    /// LBA 1, then the entry array wherever the header says it lives.
    GptTable read_gpt(unsigned int lun = 0, std::uint64_t sector_size = 0);

    /// <program>: writes `image` to a partition.
    ///
    /// Sends the command, waits for the programmer to acknowledge the setup,
    /// streams the data, then waits for the final verdict - which is the part
    /// that takes as long as the flash does, hence the long timeout.
    void program_partition(const ProgramRequest& request, const std::vector<std::uint8_t>& image,
                           unsigned int setup_timeout_ms = core::command_timeout_ms(kProgramSetupTimeoutMs),
                           unsigned int completion_timeout_ms =
                               core::transfer_timeout_ms(kProgramCompletionTimeoutMs));

    /// <read>: reads `num_sectors` starting at `start_sector` into a buffer.
    std::vector<std::uint8_t> read_partition(const ReadRequest& request,
                                             unsigned int timeout_ms = core::transfer_timeout_ms(kReadTimeoutMs));

    /// <erase>: erases a sector range.
    void erase_sectors(const EraseRequest& request,
                       unsigned int timeout_ms = core::transfer_timeout_ms(kEraseTimeoutMs));

    /// <patch>: writes a value into an already-programmed image.
    FirehoseResponse patch(const PatchEntry& entry,
                           unsigned int timeout_ms = core::command_timeout_ms(kCommandTimeoutMs));

    // -- timeouts and retries ---------------------------------------------
    static constexpr unsigned int kCommandTimeoutMs = 30000;      // one plain command
    static constexpr unsigned int kProgramSetupTimeoutMs = 10000;  // programmer acknowledges the write
    static constexpr unsigned int kProgramCompletionTimeoutMs = 120000;  // the write itself
    static constexpr unsigned int kReadTimeoutMs = 30000;
    static constexpr unsigned int kEraseTimeoutMs = 60000;
    static constexpr unsigned int kCommandAttempts = 3;

    /// Sends one command, retrying a NAK. Returns the final response.
    FirehoseResponse send_with_retry(const std::string& xml, const std::string& what,
                                     unsigned int timeout_ms, unsigned int attempts = kCommandAttempts);

    /// Where read data is streamed to while it arrives, and the callback that
    /// reports progress. Both optional.
    using ProgressCallback = std::function<void(std::uint64_t done, std::uint64_t total)>;
    void set_progress_callback(ProgressCallback callback) { m_progress = std::move(callback); }

private:
    /// Adapts this object's callbacks to the shape SaharaSession expects.
    SaharaSession::Callbacks sahara_callbacks() const;

    /// Closes and reopens the link. The device re-enumerates after a reset, so
    /// assuming an old handle is still valid is a reliable way to get puzzling
    /// failures; a fresh open costs milliseconds.
    void reopen();

    /// Reads one complete XML document, framed by <?xml ... </data>.
    std::string read_response_document(unsigned int timeout_ms);

    usb::EdlTransport m_transport;
    Callbacks m_callbacks;
    SaharaDeviceInfo m_info;
    std::string m_last_response;
    bool m_programmer_loaded{false};
    ProgressCallback m_progress;
};

}  // namespace huaxin::protocols::qualcomm
