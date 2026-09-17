// =============================================================================
//  Python bindings for the Unisoc / Spreadtrum PAC package format.
//
//  Reading a package, not talking to a device: the research-download protocol is
//  implemented and tested in protocols/spd/, but it is not exposed yet and the
//  tab says so. What this file enables is the useful half that needs no device -
//  opening a .pac and showing what is in it, which is how an operator checks
//  they have the right package before touching any hardware.
//
//  `read_pac_header` is the one the UI uses. It reads the header and the entry
//  table and stops, because a PAC is hundreds of megabytes to a few gigabytes
//  and `load_pac` would hold all of it in memory to answer a question about its
//  first few kilobytes.
// =============================================================================

#include "bindings/spd_bindings.h"

#include <pybind11/stl.h>

#include <cstdint>
#include <string>
#include <vector>

#include "core/flash_error.h"  // Vendor
#include "protocols/spd/pac.h"

namespace py = pybind11;

using huaxin::protocols::spd::PacEntry;
using huaxin::protocols::spd::PacEntryRole;
using huaxin::protocols::spd::PacFile;
using huaxin::protocols::spd::PacHeaderInfo;
using huaxin::protocols::spd::PacVersion;

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
