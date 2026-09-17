#pragma once

// =============================================================================
//  Qualcomm Sahara protocol (the handshake that precedes Firehose).
//
//  PROVENANCE - every constant and field layout in this file was transcribed
//  from Qualcomm's upstream Linux EDL tool, linux-msm/qdl (BSD-3-Clause), file
//  src/sahara.c, rather than written from memory. Nothing here is guessed; the
//  one value that could not be verified from a primary source is marked TODO.
//
//  Wire format: all integers are little-endian. Every packet is an 8-byte
//  header (command, length) followed by the command's payload, and `length`
//  counts the header too - it is the total packet size.
//
//  The handshake is driven by the device, not the host:
//      device -> HELLO_REQ          (it announces itself when it enters EDL)
//      host   -> HELLO_RESP         (host accepts and picks a mode)
//      device -> READ_DATA*         (repeatable: "send me image N, offset, len")
//      host   -> <raw image bytes>  (no header - a bare payload)
//      host   -> END_OF_IMAGE       (closes that image transfer)
//      device -> DONE_REQ / RESET_RESP
//      host   -> DONE_RESP
// =============================================================================

#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

namespace huaxin::protocols::qualcomm {

// --- command codes -----------------------------------------------------------
// Minimum protocol version in which each command appears is noted as in qdl.
enum class SaharaCommand : std::uint32_t {
    Hello = 0x1,              // v1.0
    HelloResponse = 0x2,      // v1.0
    ReadData = 0x3,           // v1.0
    EndOfImage = 0x4,         // v1.0
    Done = 0x5,               // v1.0
    DoneResponse = 0x6,       // v1.0
    Reset = 0x7,              // v1.0
    ResetResponse = 0x8,      // v1.0
    MemoryDebug = 0x9,        // v2.0
    MemoryRead = 0xa,         // v2.0
    CommandReady = 0xb,       // v2.1
    SwitchMode = 0xc,         // v2.1
    Execute = 0xd,            // v2.1
    ExecuteResponse = 0xe,    // v2.1
    ExecuteData = 0xf,        // v2.1
    MemoryDebug64 = 0x10,     // v2.5
    MemoryRead64 = 0x11,      // v2.5
    ReadData64 = 0x12,        // v2.8
    ResetState = 0x13,        // v2.9
    WriteData = 0x14,         // v3.0
};

/// Total packet sizes (header included), from qdl's *_LENGTH constants.
inline constexpr std::uint32_t kHelloLength = 0x30;
inline constexpr std::uint32_t kReadDataLength = 0x14;
inline constexpr std::uint32_t kReadData64Length = 0x20;
inline constexpr std::uint32_t kEndOfImageLength = 0x10;
inline constexpr std::uint32_t kDoneLength = 0x8;
inline constexpr std::uint32_t kDoneResponseLength = 0xc;
inline constexpr std::uint32_t kResetLength = 0x8;
/// Host -> device EXECUTE and EXECUTE_DATA: header + one client command word.
inline constexpr std::uint32_t kExecuteLength = 0xc;
inline constexpr std::uint32_t kSwitchModeLength = 0xc;
/// Device -> host EXECUTE_RESP: header + client command word + data length word.
///
/// Deliberately not kExecuteLength. qdl has a single SAHARA_EXECUTE_LENGTH of
/// 0xc, but that is the size of the packets the *host* sends; its reader
/// requires at least 4 words (16 bytes) from the device and reads data_length
/// at offset 12, which is only possible in a 16-byte packet. Using 12 here
/// would drop the data length and make every EXECUTE read look empty.
inline constexpr std::uint32_t kExecuteResponseLength = 0x10;
inline constexpr std::uint32_t kPacketHeaderLength = 0x8;

/// Protocol version the host claims in HELLO_RESP.
inline constexpr std::uint32_t kSaharaVersion = 2;
inline constexpr std::uint32_t kSaharaSuccess = 0;

/// `mode` values.
enum class SaharaMode : std::uint32_t {
    ImageTransferPending = 0x0,
    ImageTransferComplete = 0x1,
    MemoryDebug = 0x2,
    Command = 0x3,
};

/// Client commands carried by EXECUTE, i.e. how device identity is read.
enum class SaharaExecCommand : std::uint32_t {
    SerialNumber = 0x01,   // u32
    MsmHardwareId = 0x02,  // u64; pre-v3 only
    OemPublicKeyHash = 0x03,
    ReadChipIdV3 = 0x0a,   // 44+ bytes; v3 and later
};

/// Timeout qdl uses for command-mode exchanges.
inline constexpr unsigned int kSaharaCommandTimeoutMs = 1000;
/// Waiting for the device to announce itself can legitimately take longer.
inline constexpr unsigned int kSaharaHelloTimeoutMs = 5000;

// --- bytes -------------------------------------------------------------------
std::uint16_t read_le16(const std::uint8_t* data) noexcept;
std::uint32_t read_le32(const std::uint8_t* data) noexcept;
std::uint64_t read_le64(const std::uint8_t* data) noexcept;
void write_le32(std::uint8_t* out, std::uint32_t value) noexcept;
void write_le64(std::uint8_t* out, std::uint64_t value) noexcept;

std::string hex(const std::uint8_t* data, std::size_t size, bool spaced = false);

// --- packet payloads ---------------------------------------------------------
// Field names follow qdl. Note that the two HELLO payloads are both 0x28 bytes
// of fields: version / compatible / <max_len|status> / mode, then 6 reserved
// words. Older documentation calls `compatible` "min_version" and `max_len`
// "max_version" - same layout, different names.
struct SaharaHelloRequest {
    std::uint32_t version{0};
    std::uint32_t compatible{0};
    std::uint32_t max_length{0};  // largest packet the device will accept
    std::uint32_t mode{0};
    std::uint32_t reserved[6]{};
};

struct SaharaHelloResponse {
    std::uint32_t version{kSaharaVersion};
    std::uint32_t compatible{1};
    std::uint32_t status{kSaharaSuccess};
    std::uint32_t mode{0};
    std::uint32_t reserved[6]{};
};

struct SaharaReadDataRequest {
    std::uint32_t image{0};
    std::uint32_t offset{0};
    std::uint32_t length{0};
};

struct SaharaReadData64Request {
    std::uint64_t image{0};
    std::uint64_t offset{0};
    std::uint64_t length{0};
};

struct SaharaEndOfImage {
    std::uint32_t image{0};
    std::uint32_t status{kSaharaSuccess};
};

struct SaharaExecuteRequest {
    std::uint32_t client_command{0};
};

struct SaharaExecuteResponse {
    std::uint32_t client_command{0};
    std::uint32_t data_length{0};
};

// --- encoding ----------------------------------------------------------------
std::vector<std::uint8_t> encode_hello_response(const SaharaHelloResponse& response);
std::vector<std::uint8_t> encode_end_of_image(const SaharaEndOfImage& end_of_image);
std::vector<std::uint8_t> encode_execute(const SaharaExecuteRequest& request);

/// The follow-up that actually releases the payload. Same body as EXECUTE but a
/// different command code (0x0f vs 0x0d) - sending EXECUTE twice leaves the
/// device waiting and the payload read times out.
std::vector<std::uint8_t> encode_execute_data(const SaharaExecuteRequest& request);
std::vector<std::uint8_t> encode_reset();
std::vector<std::uint8_t> encode_done_response(std::uint32_t status = kSaharaSuccess);
std::vector<std::uint8_t> encode_switch_mode(SaharaMode mode);

// --- decoding ----------------------------------------------------------------
// All decoders take the complete packet (header included) and validate both the
// command code and the declared length before reading any field. `error` is
// filled in on failure and the return value is false.
bool decode_hello_request(const std::uint8_t* data, std::size_t size,
                          SaharaHelloRequest& out, std::string& error);
bool decode_hello_response(const std::uint8_t* data, std::size_t size,
                           SaharaHelloResponse& out, std::string& error);
bool decode_read_data_request(const std::uint8_t* data, std::size_t size,
                              SaharaReadDataRequest& out, std::string& error);
bool decode_read_data64_request(const std::uint8_t* data, std::size_t size,
                                SaharaReadData64Request& out, std::string& error);
bool decode_execute_response(const std::uint8_t* data, std::size_t size,
                             SaharaExecuteResponse& out, std::string& error);

/// Reads just the 8-byte header out of a complete packet.
bool decode_header(const std::uint8_t* data, std::size_t size,
                   std::uint32_t& command, std::uint32_t& length, std::string& error);

// --- transport ---------------------------------------------------------------
/// The byte pipe Sahara runs over. Kept abstract so the handshake can be driven
/// - and tested - without a device attached.
class IByteTransport {
public:
    virtual ~IByteTransport() = default;

    /// Sends exactly `size` bytes or throws.
    virtual void write_all(const std::uint8_t* data, std::size_t size, unsigned int timeout_ms) = 0;

    /// Receives exactly `size` bytes or throws. Bulk transfers may return short
    /// packets, so an implementation must accumulate.
    virtual void read_exact(std::uint8_t* buffer, std::size_t size, unsigned int timeout_ms) = 0;

    /// Sends a class control transfer, for the one protocol that needs one:
    /// Unisoc's Research Download mode configures its endpoints with a bare
    /// control transfer before it will accept a bulk frame at all.
    ///
    /// The default throws, so a transport that has no control pipe says so
    /// rather than silently doing nothing and leaving the link dead.
    virtual void control_transfer(std::uint8_t request_type, std::uint8_t request,
                                  std::uint16_t value, std::uint16_t index,
                                  unsigned int timeout_ms);

    /// Receives up to `size` bytes and returns how many arrived.
    ///
    /// Firehose responses carry no length prefix, so the host has to read
    /// whatever is available and look for the end of the document. A timeout
    /// with nothing received throws.
    virtual std::size_t read_some(std::uint8_t* buffer, std::size_t size, unsigned int timeout_ms) = 0;

    /// Human-readable description of the link, for logs.
    virtual std::string describe() const = 0;
};

// --- device identity ---------------------------------------------------------
struct SaharaDeviceInfo {
    std::uint32_t protocol_version{0};
    std::uint32_t device_mode{0};

    bool have_serial{false};
    std::uint32_t serial{0};

    bool have_hardware_id{false};
    std::uint64_t hardware_id{0};
    std::uint32_t msm_id{0};
    std::uint32_t oem_id{0};
    std::uint32_t model_id{0};

    bool have_pk_hash{false};
    std::string pk_hash;  // hex, no 0x prefix

    /// A chip family name where the MSM id is one we recognise. Deliberately
    /// empty when unknown rather than a guess.
    std::string chip_name;
};

// --- session -----------------------------------------------------------------
/// Drives the Sahara handshake over a transport.
///
/// Not thread-safe by design: HardwareBridge serialises access, and the whole
/// point of the single worker thread is that only one conversation with a
/// device happens at a time.
class SaharaSession {
public:
    struct Callbacks {
        std::function<void(const std::string& level, const std::string& message)> log;
        std::function<void(int percent, const std::string& message)> progress;
        /// Returns true when the operator asked to stop; checked between packets.
        std::function<bool()> cancelled;
    };

    /// image id -> raw bytes. Most chipsets take the Firehose programmer as
    /// image 13; the device names the ids it wants in READ_DATA, so the map is
    /// keyed by what the device asks for rather than by a fixed index.
    using ImageTable = std::map<std::uint32_t, std::vector<std::uint8_t>>;

    SaharaSession(IByteTransport& transport, Callbacks callbacks);

    /// Runs the handshake and uploads whatever the device requests.
    /// Throws ProtocolError on a malformed packet, a timeout, or cancellation.
    SaharaDeviceInfo run(const ImageTable& images);

    /// Reads serial / MSM id / OEM PK hash over Sahara command mode.
    ///
    /// Command mode is a separate conversation from the image transfer: the host
    /// announces it in HELLO_RESP, the device then answers EXECUTE instead of
    /// asking for image data, and the host has to explicitly leave. That is why
    /// this is a separate step rather than part of run().
    ///
    /// `return_to_image_transfer` picks how it leaves:
    ///   true  - SWITCH_MODE back to image transfer. The device re-sends HELLO,
    ///           which a following run() picks up. Use this before uploading.
    ///   false - SAHARA_RESET. The device goes back to its initial EDL state,
    ///           so the next operation starts from a clean HELLO. Use this when
    ///           nothing is going to be uploaded.
    SaharaDeviceInfo query_device_identity(SaharaDeviceInfo info, bool return_to_image_transfer);

    const SaharaDeviceInfo& device_info() const noexcept { return m_info; }

private:
    void log(const std::string& level, const std::string& message) const;
    void check_cancelled() const;

    IByteTransport& m_transport;
    Callbacks m_callbacks;
    SaharaDeviceInfo m_info;
};

/// Raised for any protocol-level failure.
class ProtocolError : public std::runtime_error {
public:
    explicit ProtocolError(const std::string& message) : std::runtime_error(message) {}

    /// A coarse classification hint for the unified error layer.
    ///
    /// The protocol layer must not depend on core/flash_error.h: that points the
    /// dependency arrow the wrong way, and it would drag libusb into the
    /// protocol test binaries, which deliberately link without it. So a derived
    /// exception that knows something more specific about itself says so with a
    /// short tag, and core::classify() reads the tag instead of testing for
    /// every derived type in the tree.
    virtual const char* error_kind() const noexcept { return "protocol"; }
};

/// The default: a transport with no control pipe says so rather than silently
/// doing nothing, which would leave a Unisoc link dead with no error at all.
inline void IByteTransport::control_transfer(std::uint8_t, std::uint8_t, std::uint16_t,
                                             std::uint16_t, unsigned int) {
    throw ProtocolError(
        "this transport has no control pipe, and the protocol needs one: Unisoc Research "
        "Download mode configures its USB endpoints with a control transfer before it will "
        "accept anything on the bulk endpoints.");
}

}  // namespace huaxin::protocols::qualcomm
