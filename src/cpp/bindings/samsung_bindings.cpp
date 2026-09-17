// =============================================================================
//  Python bindings for the Samsung PIT parser and Odin protocol.
// =============================================================================

#include "bindings/samsung_bindings.h"

#include <pybind11/stl.h>

#include <string>
#include <vector>

#include "protocols/samsung/odin.h"
#include "protocols/samsung/tar.h"

namespace py = pybind11;

using huaxin::protocols::samsung::OdinControl;
using huaxin::protocols::samsung::OdinResponse;
using huaxin::protocols::samsung::OdinSessionRequest;
using huaxin::protocols::samsung::PitData;
using huaxin::protocols::samsung::PitEntry;
using huaxin::protocols::samsung::TarArchive;
using huaxin::protocols::samsung::TarEntry;

namespace huaxin::bindings {

void register_samsung(py::module_& module) {
    py::class_<PitEntry>(module, "PitEntry", "One partition of a Samsung PIT.")
        .def(py::init<>())
        .def_readonly("binary_type", &PitEntry::binary_type)
        .def_readonly("device_type", &PitEntry::device_type)
        .def_readonly("identifier", &PitEntry::identifier)
        .def_readonly("attributes", &PitEntry::attributes)
        .def_readonly("update_attributes", &PitEntry::update_attributes)
        .def_readonly("block_size_or_offset", &PitEntry::block_size_or_offset)
        .def_readonly("block_count", &PitEntry::block_count)
        .def_readonly("file_offset", &PitEntry::file_offset,
                      "Obsolete in the format; present in every entry.")
        .def_readonly("file_size", &PitEntry::file_size, "Obsolete in the format.")
        .def_readonly("partition_name", &PitEntry::partition_name)
        .def_readonly("flash_filename", &PitEntry::flash_filename,
                      "The name the image is transferred under.")
        .def_readonly("fota_filename", &PitEntry::fota_filename)
        .def_property_readonly("writable", &PitEntry::writable)
        .def_property_readonly("secure", &PitEntry::secure)
        .def_property_readonly("size_bytes", &PitEntry::size_bytes,
                               "Block size times block count; 0 when the geometry is absent.")
        .def_property_readonly("device_type_name", &PitEntry::device_type_name)
        .def("__repr__", [](const PitEntry& self) {
            return "PitEntry(" + self.partition_name + ", id=" + std::to_string(self.identifier)
                   + ")";
        });

    py::class_<PitData>(module, "PitData", "A parsed partition information table.")
        .def_readonly("entries", &PitData::entries)
        // reference_internal, not the default: PitData::find returns a pointer to
        // an element owned by the vector. The default policy lets Python take
        // ownership of it, which frees memory the vector still holds - the
        // symptom is a lookup that works once and returns garbage or crashes
        // afterwards.
        .def("find", &PitData::find, py::arg("partition_name"),
             py::return_value_policy::reference_internal,
             "Returns the entry with that name, or None.")
        .def_property_readonly("expected_size", &PitData::expected_size)
        .def("__len__", [](const PitData& self) { return self.entries.size(); })
        .def("__repr__", [](const PitData& self) {
            return "PitData(" + std::to_string(self.entries.size()) + " entries)";
        });

    module.attr("PIT_MAGIC") = PitData::kFileIdentifier;
    module.attr("PIT_ENTRY_SIZE") = PitData::kEntryDataSize;

    module.def(
        "parse_pit",
        [](const py::bytes& data) {
            const std::string raw = data;
            return huaxin::protocols::samsung::parse_pit(
                reinterpret_cast<const std::uint8_t*>(raw.data()), raw.size());
        },
        py::arg("data"),
        "Parses a PIT image. Raises ProtocolError when the data is not a PIT or is "
        "inconsistent with the entry count it declares.");

    py::enum_<OdinControl>(module, "OdinControl")
        .value("SendFilePart", OdinControl::SendFilePart)
        .value("Session", OdinControl::Session)
        .value("PitFile", OdinControl::PitFile)
        .value("FileTransfer", OdinControl::FileTransfer)
        .value("EndSession", OdinControl::EndSession);

    py::enum_<OdinSessionRequest>(module, "OdinSessionRequest")
        .value("BeginSession", OdinSessionRequest::BeginSession)
        .value("DeviceType", OdinSessionRequest::DeviceType)
        .value("TotalBytes", OdinSessionRequest::TotalBytes)
        .value("FilePartSize", OdinSessionRequest::FilePartSize)
        .value("EnableTFlash", OdinSessionRequest::EnableTFlash);

    py::class_<OdinResponse>(module, "OdinResponse",
                             "A decoded Odin response. The meaning of `result` depends on "
                             "`type`: a packet size for session, a PIT length for pit_file, "
                             "and a status otherwise.")
        .def_readonly("type", &OdinResponse::type)
        .def_readonly("result", &OdinResponse::result)
        .def_property_readonly("accepted", &OdinResponse::accepted)
        .def("__repr__", [](const OdinResponse& self) {
            return std::string("OdinResponse(") + to_string(self.type) + ", "
                   + std::to_string(self.result) + ")";
        });

    module.def("build_session_packet",
               [](OdinSessionRequest request, py::object argument) {
                   using huaxin::protocols::samsung::build_session_packet;
                   if (argument.is_none()) {
                       return py::bytes(reinterpret_cast<const char*>(
                                            build_session_packet(request).data()),
                                        build_session_packet(request).size());
                   }
                   const auto packet = build_session_packet(request, argument.cast<std::uint32_t>());
                   return py::bytes(reinterpret_cast<const char*>(packet.data()), packet.size());
               },
               py::arg("request"), py::arg("argument") = py::none(),
               "Builds a 1024-byte session packet, exposed for inspection and testing.");
    module.def("build_control_packet",
               [](OdinControl control) {
                   const auto packet = huaxin::protocols::samsung::build_control_packet(control);
                   return py::bytes(reinterpret_cast<const char*>(packet.data()), packet.size());
               },
               py::arg("control"));
    // -- tar archives ---------------------------------------------------------
    // What a firmware package contains, without writing anything to a device.
    // The listing streams the archive's headers, so a four-gigabyte package
    // costs a few kilobytes of memory rather than four gigabytes.
    py::class_<TarEntry>(module, "TarEntry", "One member of a tar archive.")
        .def(py::init<>())
        .def_readonly("name", &TarEntry::name)
        .def_readonly("size", &TarEntry::size)
        .def_readonly("data_offset", &TarEntry::data_offset)
        .def_readonly("type_flag", &TarEntry::type_flag)
        .def_readonly("mode", &TarEntry::mode)
        .def_property_readonly("is_file", &TarEntry::is_file,
                               "True for a regular file rather than a directory, link or "
                               "device node.")
        .def_property_readonly("base_name", &TarEntry::base_name,
                               "The member name with any directory prefix removed.")
        .def("__repr__", [](const TarEntry& self) {
            return "TarEntry('" + self.name + "', " + std::to_string(self.size) + " bytes)";
        });

    py::class_<TarArchive>(module, "TarArchive",
                           "A tar archive's member list. Read-only: this describes a file "
                           "on disk, so mutating it would change nothing real.")
        .def(py::init<>())
        .def_readonly("entries", &TarArchive::entries)
        .def_readonly("terminated", &TarArchive::terminated,
                      "True when the two ending zero blocks were reached.")
        .def("files", &TarArchive::files, "Only the regular-file members.")
        .def("find", [](const TarArchive& self, const std::string& name) -> py::object {
            const TarEntry* entry = self.find(name);
            return entry == nullptr ? py::none() : py::cast(*entry);
        }, py::arg("name"), "The member with this exact name, or None.")
        .def_property_readonly("total_file_bytes", &TarArchive::total_file_bytes,
                               "The sum of the regular-file members' sizes.")
        .def("__len__", [](const TarArchive& self) { return self.entries.size(); })
        .def("__repr__", [](const TarArchive& self) {
            return "TarArchive(" + std::to_string(self.entries.size()) + " members)";
        });

    module.def("list_tar_file", &huaxin::protocols::samsung::list_tar_file,
               py::arg("path"),
               "Lists a tar archive by streaming its headers. Stops at the ending zero "
               "blocks or at an appended .md5 digest, which is raw binary rather than a "
               "tar header. Raises when the file is not an archive.");

    module.attr("ODIN_CONTROL_PACKET_SIZE") = huaxin::protocols::samsung::kControlPacketSize;
    module.attr("ODIN_RESPONSE_PACKET_SIZE") = huaxin::protocols::samsung::kResponsePacketSize;
    module.attr("ODIN_DEFAULT_FILE_PART_SIZE") = huaxin::protocols::samsung::kDefaultFilePartSize;
}

}  // namespace huaxin::bindings
