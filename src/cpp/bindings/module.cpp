// =============================================================================
//  pybind11 glue for the Huaxin native backend.
//
//  This file contains no hardware logic and no protocol logic on purpose - it
//  only describes the C++ API to Python. Everything it exposes lives in core/,
//  usb/ or protocols/.
//
//  Conventions
//  -----------
//   * std::runtime_error  ->  RuntimeError     (unhandled by callers)
//   * bool returns        ->  expected, actionable failures
//   * std::vector<T>      ->  list[T]          (needs pybind11/stl.h)
//
//  The GIL
//  -------
//  Calls that touch USB release the GIL. Without that, the worker thread would
//  hold it for the whole enumeration and the Qt event loop would stall waiting
//  to run Python slots - the UI would freeze even though the work is on another
//  thread. `py::gil_scoped_release` is a scoped object, so it is destroyed (and
//  the GIL reacquired) when the lambda returns, which is *before* pybind11
//  converts the return value. That ordering is what makes returning a
//  std::vector safe here.
// =============================================================================

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>  // std::vector <-> list, std::string <-> str

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

#include "bindings/core_bindings.h"
#include "bindings/mediatek_bindings.h"
#include "bindings/qualcomm_bindings.h"
#include "bindings/samsung_bindings.h"
#include "bindings/spd_bindings.h"
#include "core/device_catalog.h"
#include "core/device_info.h"
#include "core/hardware_bridge.h"
#include "core/logger.h"
#include "usb/usb_manager.h"

namespace py = pybind11;
using huaxin::core::DeviceInfo;
using huaxin::core::HardwareBridge;
using huaxin::core::TargetKind;
using huaxin::usb::UsbManager;

PYBIND11_MODULE(huaxin_core, m) {
    m.doc() = "Huaxin Tool native backend: USB/serial transport and vendor protocol "
              "implementations (Qualcomm EDL, MediaTek BROM, Unisoc, Samsung Odin, ADB/Fastboot).";
    m.attr("__version__") = HUAXIN_CORE_VERSION;

    py::enum_<TargetKind>(m, "TargetKind", "Which flashing backend a device is reachable through.")
        .value("Unknown", TargetKind::Unknown)
        .value("AdbInterface", TargetKind::AdbInterface)
        .value("FastbootInterface", TargetKind::FastbootInterface)
        .value("QualcommEdl", TargetKind::QualcommEdl)
        .value("MediaTekBootRom", TargetKind::MediaTekBootRom)
        .value("MediaTekPreloader", TargetKind::MediaTekPreloader)
        .value("UnisocResearchDownload", TargetKind::UnisocResearchDownload)
        .value("SamsungDownload", TargetKind::SamsungDownload);

    py::class_<DeviceInfo>(m, "DeviceInfo",
                           "A device as it currently appears on the USB bus. Read-only: this is a "
                           "snapshot of hardware state, so mutating it would change nothing real.")
        .def(py::init<>())
        .def_readonly("vid", &DeviceInfo::vid, "USB vendor ID.")
        .def_readonly("pid", &DeviceInfo::pid, "USB product ID.")
        .def_readonly("bus_number", &DeviceInfo::bus_number)
        .def_readonly("port_number", &DeviceInfo::port_number)
        .def_readonly("device_address", &DeviceInfo::device_address)
        .def_readonly("manufacturer", &DeviceInfo::manufacturer,
                      "USB manufacturer string. Empty when the device could not be opened.")
        .def_readonly("product", &DeviceInfo::product,
                      "USB product string. Empty when the device could not be opened.")
        .def_readonly("serial", &DeviceInfo::serial,
                      "USB serial number. Empty when the device could not be opened.")
        .def_readonly("vendor", &DeviceInfo::vendor, "Vendor name from the catalogue, else from USB.")
        .def_readonly("mode", &DeviceInfo::mode, "Mode name, e.g. 'EDL (Emergency Download)'.")
        .def_readonly("phase", &DeviceInfo::phase, "Phase that implements this target's protocol.")
        .def_readonly("kind", &DeviceInfo::kind)
        .def_readonly("is_root_hub", &DeviceInfo::is_root_hub,
                      "True for a host-controller hub. Only present when enumeration was "
                      "asked to include them.")
        .def_readonly("recognised", &DeviceInfo::recognised, "True when VID:PID matched the catalogue.")
        .def_readonly("verified", &DeviceInfo::verified,
                      "True when that catalogue entry is confirmed against real hardware.")
        .def_readonly("description", &DeviceInfo::description, "Display label.")
        .def_property_readonly("usb_id", &DeviceInfo::usb_id,
                               "VID:PID as lowercase hex, e.g. '05c6:9008'.")
        .def("__repr__", [](const DeviceInfo& self) {
            return "DeviceInfo(" + self.usb_id() + ", '" + self.description + "')";
        });

    py::class_<HardwareBridge>(m, "HardwareBridge",
                               "Owns the native USB context and dispatches to the per-vendor "
                               "protocol implementations.")
        .def(py::init<>(),
             "Creates the bridge. No OS resources are touched until init() is called.")
        .def(
            "init",
            [](HardwareBridge& self) {
                py::gil_scoped_release release;
                return self.init();
            },
            "Creates the libusb context. Idempotent; returns True on success, with the "
            "reason available from last_error().")
        .def(
            "shutdown",
            [](HardwareBridge& self) {
                py::gil_scoped_release release;
                self.shutdown();
            },
            "Releases the libusb context. Safe to call repeatedly.")
        .def("is_initialized", &HardwareBridge::is_initialized,
             "True between a successful init() and the matching shutdown().")
        .def(
            "get_device_list",
            [](HardwareBridge& self, bool read_string_descriptors, bool include_root_hubs) {
                py::gil_scoped_release release;
                return self.get_device_list(read_string_descriptors, include_root_hubs);
            },
            py::arg("read_string_descriptors") = true, py::arg("include_root_hubs") = false,
            "Enumerates every USB device libusb can see.\n\n"
            "Set read_string_descriptors=False to skip manufacturer/product/serial, which "
            "needs each device to be openable (a WinUSB-class driver on Windows) and costs "
            "a control transfer per string per device.\n\n"
            "Raises RuntimeError when init() has not been called.")
        .def("last_error", &HardwareBridge::last_error,
             "Reason the last init() failed; empty when it succeeded.")
        .def("backend_version", &HardwareBridge::backend_version,
             "Version string of the native backend.")
        .def("libusb_version", &HardwareBridge::libusb_version,
             "Version of the libusb that is actually linked in.")
        .def("known_target_count", &HardwareBridge::known_target_count,
             "Number of VID:PID pairings in the catalogue.");

    py::class_<huaxin::core::KnownTarget>(m, "KnownTarget",
                                          "One VID:PID pairing from the catalogue. The same table "
                                          "drives enumeration, the UI and protocol routing.")
        .def_readonly("vid", &huaxin::core::KnownTarget::vid)
        .def_readonly("pid", &huaxin::core::KnownTarget::pid)
        .def_readonly("kind", &huaxin::core::KnownTarget::kind)
        .def_readonly("vendor", &huaxin::core::KnownTarget::vendor)
        .def_readonly("mode", &huaxin::core::KnownTarget::mode)
        .def_readonly("phase", &huaxin::core::KnownTarget::phase)
        .def_readonly("verified", &huaxin::core::KnownTarget::verified,
                      "False when the pairing is reported in the wild but has not been "
                      "confirmed against real hardware by this project.")
        .def("__repr__", [](const huaxin::core::KnownTarget& self) {
            char id[16];
            std::snprintf(id, sizeof(id), "%04x:%04x", static_cast<unsigned>(self.vid),
                          static_cast<unsigned>(self.pid));
            return std::string("KnownTarget(") + id + ", '" + self.vendor + " - " + self.mode
                   + (self.verified ? "'" : "' [unverified]") + ")";
        });

    // The catalogue is queryable on its own so the mapping can be tested without
    // a device plugged in. It also lets the UI preview a target before hardware
    // is attached.
    m.def(
        "lookup_target",
        [](std::uint16_t vid, std::uint16_t pid) -> py::object {
            const huaxin::core::KnownTarget* target = huaxin::core::find_target(vid, pid);
            return target == nullptr ? py::none() : py::cast(*target);
        },
        py::arg("vid"), py::arg("pid"),
        "Returns the KnownTarget for an exact VID:PID, or None when it is not a target "
        "this tool implements.");
    m.def(
        "vendor_for_vid",
        [](std::uint16_t vid) -> py::object {
            const char* vendor = huaxin::core::vendor_for_vid(vid);
            return vendor == nullptr ? py::none() : py::cast(vendor);
        },
        py::arg("vid"), "Vendor name for a VID alone, or None when unknown.");

    // Vendor protocols live in their own binding file; this one stays the
    // module's entry point and the place for core-level types.
    //
    // register_core comes first: it installs the FlashException translator, and
    // the vendor entry points below raise through it. Registering it later would
    // still work - translators are tried newest first - but the order here says
    // which layer the error system belongs to.
    huaxin::bindings::register_core(m);
    huaxin::bindings::register_qualcomm(m);
    huaxin::bindings::register_mediatek(m);
    huaxin::bindings::register_samsung(m);
    huaxin::bindings::register_spd(m);

    // -- file log ----------------------------------------------------------
    // The UI mirrors its own lines here as well, so flash_log.txt is one
    // chronological record of everything the tool did rather than only the parts
    // that happened to be in C++.
    py::class_<huaxin::core::Logger>(m, "Logger", "The process-wide file log.")
        .def_static("instance", &huaxin::core::Logger::instance,
                    py::return_value_policy::reference)
        .def("open", &huaxin::core::Logger::open, py::arg("path"),
             "Opens (or creates) the log file, appending. False when it cannot be written.")
        .def("close", &huaxin::core::Logger::close)
        .def("is_open", &huaxin::core::Logger::is_open)
        .def("path", &huaxin::core::Logger::path)
        // Takes the string level names the rest of the project uses rather than
        // the C++ enum, so Python callers do not have to reach for a type they
        // otherwise never see.
        .def(
            "write",
            [](huaxin::core::Logger& self, const std::string& level, const std::string& message) {
                self.log(huaxin::core::Logger::parse_level(level), message);
            },
            py::arg("level"), py::arg("message"),
            "Writes one timestamped entry. Levels: debug, info, warn, error, critical - an "
            "unknown name is treated as info rather than raising.")
        .def(
            "set_min_level",
            [](huaxin::core::Logger& self, const std::string& level) {
                self.set_min_level(huaxin::core::Logger::parse_level(level));
            },
            py::arg("level"),
            "Drops everything below this level, from both the file and the console.")
        .def_property(
            "file_enabled", &huaxin::core::Logger::file_enabled,
            &huaxin::core::Logger::set_file_enabled,
            "Turn file output on or off without forgetting the path.")
        .def_property(
            "console_enabled", &huaxin::core::Logger::console_enabled,
            &huaxin::core::Logger::set_console_enabled,
            "Turn the UI console mirror on or off.")
        .def_property("max_file_bytes", &huaxin::core::Logger::max_file_bytes,
                      &huaxin::core::Logger::set_max_file_bytes,
                      "Size past which the log rotates to a single .1 backup. 0 disables "
                      "rotation. The limit is checked against a running byte count, so the "
                      "file never overshoots by more than the line that crossed it.")
        .def("rotate_if_needed", &huaxin::core::Logger::rotate_if_needed,
             "Rotates now if the file has reached the limit. Called automatically on write.")
        .def("log_exception", &huaxin::core::Logger::log_exception, py::arg("error"),
             "Writes a classified failure at the level its severity deserves: a bug in the "
             "tool lands at CRITICAL, a cable problem at ERROR, a cancellation at WARNING.")
        .def_static("level_name", [](const std::string& level) {
            return std::string(huaxin::core::Logger::level_name(
                huaxin::core::Logger::parse_level(level)));
        }, py::arg("level"), "The five-character tag a level is written with.")
        .def_static("colourise",
                    [](const std::string& level, const std::string& text, bool enabled) {
                        return huaxin::core::Logger::colourise(
                            huaxin::core::Logger::parse_level(level), text, enabled);
                    },
                    py::arg("level"), py::arg("text"), py::arg("enabled") = true,
                    "Wraps text in the ANSI colour for a level, for a terminal that can show "
                    "it. The Qt console does its own colouring and passes enabled=False.")
        .def_static("rotated_path_for", &huaxin::core::Logger::rotated_path_for, py::arg("path"),
                    "Where the previous log is put when the current one rotates.")
        .def_static("default_path", &huaxin::core::Logger::default_path);

    m.def("log_timestamp", &huaxin::core::Logger::timestamp,
          "The format the log uses for timestamps, so a caller can match it.");

    m.def("known_target_count", &huaxin::core::known_target_count,
          "Number of VID:PID pairings in the catalogue (module-level convenience).");
    m.def("libusb_version", []() { return UsbManager::libusb_version(); },
          "Version of the libusb that is actually linked in. Callable without a "
          "HardwareBridge, so a build can be diagnosed before touching hardware.");
}
