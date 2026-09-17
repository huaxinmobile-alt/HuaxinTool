#pragma once

// =============================================================================
//  Bulk transport for Unisoc / Spreadtrum Research Download mode.
//
//  The USB plumbing is MtkTransport's, pointed at the Unisoc vendor id: the
//  device is found by VID/PID, interface 0 is claimed, and the bulk endpoints
//  are discovered from the descriptor rather than assumed.
//
//  The endpoint addresses in the reference clients are 0x85 in and 0x06 out,
//  and those are what a Research Download device advertises. They are still
//  discovered here rather than hard-coded, because a device that enumerates
//  differently is better served by opening than by refusing.
//
//  One thing this transport has that the others do not: Research Download mode
//  needs a class control transfer before it will accept anything on the bulk
//  endpoints at all. Without it the link is silently dead - nothing errors, the
//  device simply never answers. The BSL session sends it as part of the hello.
// =============================================================================

#include <cstdint>
#include <string>
#include <vector>

#include "usb/mtk_transport.h"

namespace huaxin::usb {

/// The Unisoc identities a device in a download mode advertises. Only the first
/// is confirmed: it is the one every reference client opens, and it is in this
/// project's catalogue. The others are here so that a device which presents a
/// different id is reported as "found but not this one" rather than as absent.
///
/// TODO: confirm the secondary ids against real hardware. A wrong entry here
/// costs nothing - the transport would fail to claim the interface and say so -
/// but a missing one makes a device look like it is not connected.
std::vector<UsbTarget> unisoc_targets();

/// A link to a device in Research Download mode.
class SpdTransport final : public MtkTransport {
public:
    struct Options {
        unsigned int read_timeout_ms{5000};
        int interface_number{0};
    };

    /// Finds a Unisoc device in a download mode and opens it. Throws
    /// ProtocolError with driver guidance on failure.
    void open();
    void open(const Options& options);

    /// Unisoc USB IDs currently present, as "1782:4d00" style strings.
    static std::vector<std::string> devices_present();
};

}  // namespace huaxin::usb
