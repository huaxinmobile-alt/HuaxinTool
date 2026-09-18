// =============================================================================
//  Python bindings for the Unisoc / Spreadtrum PAC package format.
//
//  Two things: reading a package, and talking to a device.
//
//  The package half needs no hardware - opening a .pac and showing what is in it
//  is how an operator checks they have the right file before touching anything.
//
//  The device half is `UnisocBsl`, the Research Download session. It is bound the
//  same way MediaTekBrom is: a class that owns its own transport, Python callbacks
//  for the log, the progress and the cancel check, and `py::gil_scoped_release`
//  around every call that blocks on USB - without that release, opening a device
//  would freeze the whole interface for as long as the device takes to answer.
//
//  `read_pac_header` is the one the UI uses. It reads the header and the entry
//  table and stops, because a PAC is hundreds of megabytes to a few gigabytes
//  and `load_pac` would hold all of it in memory to answer a question about its
//  first few kilobytes.
// =============================================================================

#include "bindings/spd_bindings.h"

#include <pybind11/stl.h>

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "core/flash_error.h"  // Vendor
#include "protocols/spd/pac.h"
#include "protocols/spd/unisoc_bsl.h"

namespace py = pybind11;

using huaxin::protocols::spd::PacEntry;
using huaxin::protocols::spd::PacEntryRole;
using huaxin::protocols::spd::PacFile;
using huaxin::protocols::spd::PacHeaderInfo;
using huaxin::protocols::spd::ChipInfo;
using huaxin::protocols::spd::PacVersion;
using huaxin::protocols::spd::UnisocBsl;

namespace huaxin::bindings {

void register_spd(py::module_& module) {
    py::enum_<PacVersion>(module, "PacVersion",
                          "Which generation of the container this is. The two differ only "
                          "in how the header's size fields are split.")
        .value("Unknown", PacVersion::Unknown)
        .value("V1", PacVersion::V1, "32-bit sizes.")
        .value("V2", PacVersion::V2, "64-bit sizes, split across two words.");

    py::enum_<PacEntryRole>(module, "PacEntryRole",
                            "What an entry is for, worked out from its identifier.")
        .value("Unknown", PacEntryRole::Unknown, "Not one this build recognises.")
        .value("Fdl1", PacEntryRole::Fdl1, "The first-stage loader: HOST_FDL, FDL or FDL1.")
        .value("Fdl2", PacEntryRole::Fdl2,
               "The second-stage loader, which carries the storage commands.")
        .value("Marker", PacEntryRole::Marker,
               "An operation with no payload: a format, an erase, or a phase marker.")
        .value("Image", PacEntryRole::Image, "A partition image.");

    py::class_<PacHeaderInfo>(module, "PacHeaderInfo",
                              "The container's header. Read-only: this describes a file.")
        .def(py::init<>())
        .def_readonly("version", &PacHeaderInfo::version)
        .def_readonly("version_string", &PacHeaderInfo::version_string,
                      "The version string the vendor put in the package.")
        .def_readonly("product_name", &PacHeaderInfo::product_name)
        .def_readonly("product_version", &PacHeaderInfo::product_version)
        .def_readonly("product_alias", &PacHeaderInfo::product_alias)
        .def_readonly("declared_size", &PacHeaderInfo::declared_size,
                      "The size the header claims the whole file is.")
        .def_readonly("mode", &PacHeaderInfo::mode)
        .def_readonly("flash_type", &PacHeaderInfo::flash_type)
        .def_readonly("is_preload", &PacHeaderInfo::is_preload)
        .def_readonly("header_crc", &PacHeaderInfo::header_crc)
        .def_readonly("payload_crc", &PacHeaderInfo::payload_crc)
        .def_readonly("header_crc_ok", &PacHeaderInfo::header_crc_ok,
                      "True when the header's own CRC matches. A false here means the "
                      "header is damaged, which makes every other field suspect.")
        .def("__repr__", [](const PacHeaderInfo& self) {
            return "PacHeaderInfo('" + self.product_name + "', " + self.version_string + ")";
        });

    py::class_<PacEntry>(module, "PacEntry", "One file inside a package. Read-only.")
        .def(py::init<>())
        .def_readonly("file_id", &PacEntry::file_id)
        .def_readonly("file_name", &PacEntry::file_name)
        .def_readonly("role", &PacEntry::role)
        .def_readonly("size", &PacEntry::size)
        .def_readonly("data_offset", &PacEntry::data_offset,
                      "Where the data starts in the container.")
        .def_readonly("address", &PacEntry::address)
        .def_readonly("addresses", &PacEntry::addresses)
        .def_readonly("flag", &PacEntry::flag)
        .def_readonly("can_omit", &PacEntry::can_omit)
        .def_property_readonly("is_marker", &PacEntry::is_marker,
                               "True for a table entry that carries no data.")
        .def_property_readonly("is_logical_marker", &PacEntry::is_logical_marker)
        .def("describe", &PacEntry::describe)
        .def("__repr__", [](const PacEntry& self) {
            return "PacEntry('" + self.file_id + "', " + std::to_string(self.size) + " bytes)";
        });

    py::class_<PacFile>(module, "PacFile",
                        "A package's contents. Read-only: this describes a file on disk.")
        .def(py::init<>())
        .def_readonly("header", &PacFile::header)
        .def_readonly("entries", &PacFile::entries)
        .def_readonly("file_size", &PacFile::file_size, "The real size of the file.")
        .def_readonly("payload_crc_ok", &PacFile::payload_crc_ok)
        .def_readonly("payload_crc_checked", &PacFile::payload_crc_checked,
                      "False after a header-only read: the CRC covers the payload, so "
                      "checking it would mean reading the whole file. The field says so "
                      "rather than implying a check that did not happen.")
        .def_readonly("unknown_ids", &PacFile::unknown_ids,
                      "Identifiers this build does not recognise. Not an error - the "
                      "vendor adds new ones - but worth showing.")
        .def("fdl1", [](const PacFile& self) -> py::object {
            const PacEntry* entry = self.fdl1();
            return entry == nullptr ? py::none() : py::cast(*entry);
        }, "The first-stage loader, or None when the package has none.")
        .def("fdl2", [](const PacFile& self) -> py::object {
            const PacEntry* entry = self.fdl2();
            return entry == nullptr ? py::none() : py::cast(*entry);
        }, "The second-stage loader, or None.")
        .def("downloads", &PacFile::downloads, "The entries that carry data to write.")
        .def("find", [](const PacFile& self, const std::string& file_id) -> py::object {
            const PacEntry* entry = self.find(file_id);
            return entry == nullptr ? py::none() : py::cast(*entry);
        }, py::arg("file_id"), "The entry with this identifier, or None.")
        .def_property_readonly("total_download_bytes", &PacFile::total_download_bytes,
                               "What the package would write, in bytes.")
        .def_property_readonly("crc_ok", &PacFile::crc_ok)
        .def("summary", &PacFile::summary, "One line describing the package.")
        .def("__len__", [](const PacFile& self) { return self.entries.size(); })
        .def("__repr__", [](const PacFile& self) { return "PacFile(" + self.summary() + ")"; });

    // -- the device session ----------------------------------------------------
    py::class_<ChipInfo>(module, "BslChipInfo",
                         "What the Unisoc boot ROM reports about itself and its flash. "
                         "Every field is optional: a device that refuses one query leaves "
                         "that field unset rather than failing the whole reading.")
        .def_readonly("boot_version", &ChipInfo::boot_version)
        .def_readonly("have_chip_type", &ChipInfo::have_chip_type)
        .def_readonly("chip_type", &ChipInfo::chip_type)
        .def_readonly("have_flash_info", &ChipInfo::have_flash_info)
        .def_readonly("flash_info", &ChipInfo::flash_info)
        .def_readonly("have_flash_type", &ChipInfo::have_flash_type)
        .def_readonly("flash_type", &ChipInfo::flash_type)
        .def_readonly("have_sector_size", &ChipInfo::have_sector_size)
        .def_readonly("sector_size", &ChipInfo::sector_size)
        .def_readonly("have_chip_uid", &ChipInfo::have_chip_uid)
        .def_readonly("chip_uid", &ChipInfo::chip_uid)
        .def("describe", &ChipInfo::describe, "One line for the log.")
        .def("__repr__",
             [](const ChipInfo& self) { return "BslChipInfo(" + self.describe() + ")"; });

    py::class_<UnisocBsl>(module, "UnisocBsl",
                          "Unisoc Research Download session: USB transport plus the BSL "
                          "conversation. Nothing here touches a device except the calls "
                          "named for what they do.")
        .def(py::init([](const py::object& log, const py::object& progress,
                         const py::object& cancelled) {
                 UnisocBsl::Callbacks callbacks;
                 if (!log.is_none()) {
                     auto function = py::reinterpret_borrow<py::function>(log);
                     callbacks.log = [function](const std::string& level,
                                                const std::string& message) {
                         py::gil_scoped_acquire acquire;
                         try {
                             function(level, message);
                         } catch (py::error_already_set& error) {
                             error.discard_as_unraisable("huaxin log callback");
                         }
                     };
                 }
                 if (!progress.is_none()) {
                     auto function = py::reinterpret_borrow<py::function>(progress);
                     callbacks.progress = [function](int percent, const std::string& message) {
                         py::gil_scoped_acquire acquire;
                         try {
                             function(percent, message);
                         } catch (py::error_already_set& error) {
                             error.discard_as_unraisable("huaxin progress callback");
                         }
                     };
                 }
                 if (!cancelled.is_none()) {
                     auto function = py::reinterpret_borrow<py::function>(cancelled);
                     callbacks.cancelled = [function]() -> bool {
                         py::gil_scoped_acquire acquire;
                         try {
                             return function().cast<bool>();
                         } catch (py::error_already_set& error) {
                             error.discard_as_unraisable("huaxin cancel callback");
                             return false;
                         }
                     };
                 }
                 return std::make_unique<UnisocBsl>(std::move(callbacks));
             }),
             py::arg("log") = py::none(), py::arg("progress") = py::none(),
             py::arg("cancelled") = py::none())
        .def_static("devices_present", &UnisocBsl::devices_present,
                    "Unisoc download-mode USB IDs on the bus, e.g. ['1782:4d00'].")
        .def("connect", [](UnisocBsl& self) {
                 py::gil_scoped_release release;
                 self.connect();
             },
             "Opens the device and runs the BSL hello. Throws with driver guidance "
             "when there is no device, or when it does not answer as a boot ROM does.")
        .def("handshake", [](UnisocBsl& self) {
                 py::gil_scoped_release release;
                 self.handshake();
             },
             "BSL_CMD_CONNECT. Idempotent, and connect() alone does not do it.")
        .def("disconnect", &UnisocBsl::disconnect)
        .def("connected", &UnisocBsl::connected)
        .def("handshaked", &UnisocBsl::handshaked)
        .def("describe", &UnisocBsl::describe)
        .def("checksum_name", &UnisocBsl::checksum_name,
             "Which checksum the link settled on, as text.")
        .def("read_device_info", [](UnisocBsl& self) {
                 py::gil_scoped_release release;
                 return self.read_device_info();
             },
             "Every query the boot ROM answers, as a BslChipInfo.")
        .def("chip_info", &UnisocBsl::chip_info, py::return_value_policy::reference_internal)
        .def("erase_flash", [](UnisocBsl& self, std::uint32_t address, std::uint32_t length) {
                 py::gil_scoped_release release;
                 self.erase_flash(address, length);
             },
             py::arg("address"), py::arg("length"),
             "Erases an address range. DESTRUCTIVE: that is the whole point of it.")
        .def("read_flash", [](UnisocBsl& self, std::uint32_t address, std::uint32_t length) {
                 py::gil_scoped_release release;
                 return self.read_flash(address, length);
             },
             py::arg("address"), py::arg("length"), "Reads a region back as bytes.")
        .def("write_flash", [](UnisocBsl& self, std::uint32_t address,
                               const std::vector<std::uint8_t>& data,
                               const std::string& what) {
                 py::gil_scoped_release release;
                 return self.write_flash(address, data, what);
             },
             py::arg("address"), py::arg("data"), py::arg("what") = "flash data",
             "Writes data to a flash address. DESTRUCTIVE.")
        .def("reset", [](UnisocBsl& self) {
                 py::gil_scoped_release release;
                 self.reset();
             },
             "Restarts the device out of download mode.")
        .def("power_off", [](UnisocBsl& self) {
            py::gil_scoped_release release;
            self.power_off();
        });

    module.def("parse_pac", &huaxin::protocols::spd::parse_pac,
               py::arg("data"), py::arg("verify_payload") = true,
               "Parses a package from bytes.");
    module.def("load_pac", &huaxin::protocols::spd::load_pac,
               py::arg("path"), py::arg("verify_payload") = true,
               "Parses a package from a path. Reads the whole file: use read_pac_header "
               "to display a listing.");
    module.def("read_pac_header", &huaxin::protocols::spd::read_pac_header, py::arg("path"),
               "Reads only the header and the entry table - what a listing needs, and "
               "nothing else. The payload CRC is left unchecked.");
}

}  // namespace huaxin::bindings
