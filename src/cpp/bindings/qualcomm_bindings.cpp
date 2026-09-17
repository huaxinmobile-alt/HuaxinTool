// =============================================================================
//  Python bindings for the Qualcomm EDL protocol layer.
//
//  Two things here are worth knowing about.
//
//  GIL. These calls block for seconds (a programmer upload) or longer (a
//  partition read), so each one releases the GIL while it runs - otherwise the
//  worker thread would hold it and the Qt event loop would stall, freezing the
//  UI despite the work being off the UI thread. The callbacks a caller passes in
//  (log/progress/cancelled) are Python callables, so they re-acquire the GIL
//  before being invoked. That is the only reason those wrappers exist rather
//  than passing std::function straight through.
//
//  Callback failures. A raising log callback must not abort a flash, so such
//  exceptions are reported as unraisable and then ignored.
// =============================================================================

#include "bindings/qualcomm_bindings.h"

#include <pybind11/functional.h>
#include <pybind11/stl.h>

#include <cstdint>
#include <exception>
#include <cstdio>
#include <functional>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include "protocols/qualcomm/firehose.h"
#include "protocols/qualcomm/gpt.h"
#include "protocols/qualcomm/qualcomm_edl.h"
#include "protocols/qualcomm/sahara.h"
#include "usb/usb_errors.h"
#include "usb/usb_discovery.h"

namespace py = pybind11;

using huaxin::protocols::qualcomm::ConfigureRequest;
using huaxin::protocols::qualcomm::EraseRequest;
using huaxin::protocols::qualcomm::FirehoseResponse;
using huaxin::protocols::qualcomm::FirehoseStatus;
using huaxin::protocols::qualcomm::GptEntry;
using huaxin::protocols::qualcomm::GptGuid;
using huaxin::protocols::qualcomm::GptHeader;
using huaxin::protocols::qualcomm::GptTable;
using huaxin::protocols::qualcomm::PatchEntry;
using huaxin::protocols::qualcomm::PowerRequest;
using huaxin::protocols::qualcomm::ProgramRequest;
using huaxin::protocols::qualcomm::ProtocolError;
using huaxin::protocols::qualcomm::QualcommEdl;
using huaxin::protocols::qualcomm::RawProgramEntry;
using huaxin::protocols::qualcomm::ReadRequest;
using huaxin::protocols::qualcomm::SaharaDeviceInfo;
using huaxin::protocols::qualcomm::StorageInfo;

namespace huaxin::bindings {

namespace {

/// Wraps a Python callable so it can be invoked from C++ that is running
/// without the GIL. Never lets a Python exception escape into the protocol code.
std::function<void(const std::string&, const std::string&)> make_log_callback(const py::object& callable) {
    if (callable.is_none()) {
        return {};
    }
    auto function = py::reinterpret_borrow<py::function>(callable);
    return [function](const std::string& level, const std::string& message) {
        py::gil_scoped_acquire acquire;
        try {
            function(level, message);
        } catch (py::error_already_set& error) {
            error.discard_as_unraisable("huaxin log callback");
        }
    };
}

std::function<void(int, const std::string&)> make_progress_callback(const py::object& callable) {
    if (callable.is_none()) {
        return {};
    }
    auto function = py::reinterpret_borrow<py::function>(callable);
    return [function](int percent, const std::string& message) {
        py::gil_scoped_acquire acquire;
        try {
            function(percent, message);
        } catch (py::error_already_set& error) {
            error.discard_as_unraisable("huaxin progress callback");
        }
    };
}

std::function<bool()> make_cancel_callback(const py::object& callable) {
    if (callable.is_none()) {
        return {};
    }
    auto function = py::reinterpret_borrow<py::function>(callable);
    return [function]() -> bool {
        py::gil_scoped_acquire acquire;
        try {
            return function().cast<bool>();
        } catch (py::error_already_set& error) {
            error.discard_as_unraisable("huaxin cancel callback");
            return false;  // a broken check must not silently abort an operation
        }
    };
}

/// Releases the GIL for the duration of a blocking device operation.
template <typename Callable>
auto without_gil(Callable&& callable) {
    py::gil_scoped_release release;
    return callable();
}

/// A byte buffer handed to Python as `bytes`. Flashed images are tens of
/// megabytes, so this is deliberately a move of the buffer's contents into a
/// fresh Python object rather than a list of ints.
py::bytes to_bytes(const std::vector<std::uint8_t>& data) {
    return py::bytes(reinterpret_cast<const char*>(data.data()), data.size());
}

/// The reverse: a Python bytes-like object as a span of bytes.
std::vector<std::uint8_t> to_buffer(const py::bytes& data) {
    const std::string text = data.cast<std::string>();
    return std::vector<std::uint8_t>(text.begin(), text.end());
}

}  // namespace

void register_qualcomm(py::module_& module) {
    static py::exception<ProtocolError>& protocol_error =
        py::register_exception<ProtocolError>(module, "ProtocolError", PyExc_RuntimeError);

    py::register_exception<huaxin::usb::UsbDiscoveryError>(
        module, "UsbDiscoveryError", protocol_error.ptr());

    // UsbDisconnectedError derives from ProtocolError in C++, and the Python side
    // mirrors that: it is registered with the ProtocolError type as its base, so
    // an existing `except ProtocolError` keeps catching it, while a caller that
    // wants to react specifically to a cable pull can catch the narrower type.
    static py::exception<huaxin::usb::UsbDisconnectedError> disconnected(
        module, "UsbDisconnectedError", protocol_error.ptr());
    py::register_exception_translator([](std::exception_ptr failure) {
        try {
            if (failure) {
                std::rethrow_exception(failure);
            }
        } catch (const huaxin::usb::UsbDisconnectedError& error) {
            disconnected(error.what());
        }
    });

    py::class_<SaharaDeviceInfo>(module, "SaharaDeviceInfo",
                                 "Chip identity read over Sahara command mode.")
        .def(py::init<>())
        .def_readonly("protocol_version", &SaharaDeviceInfo::protocol_version)
        .def_readonly("device_mode", &SaharaDeviceInfo::device_mode)
        .def_readonly("have_serial", &SaharaDeviceInfo::have_serial)
        .def_readonly("serial", &SaharaDeviceInfo::serial)
        .def_readonly("have_hardware_id", &SaharaDeviceInfo::have_hardware_id)
        .def_readonly("hardware_id", &SaharaDeviceInfo::hardware_id)
        .def_readonly("msm_id", &SaharaDeviceInfo::msm_id)
        .def_readonly("oem_id", &SaharaDeviceInfo::oem_id)
        .def_readonly("model_id", &SaharaDeviceInfo::model_id)
        .def_readonly("have_pk_hash", &SaharaDeviceInfo::have_pk_hash)
        .def_readonly("pk_hash", &SaharaDeviceInfo::pk_hash)
        .def_readonly("chip_name", &SaharaDeviceInfo::chip_name)
        .def("__repr__", [](const SaharaDeviceInfo& self) {
            char buffer[192];
            std::snprintf(buffer, sizeof(buffer),
                          "SaharaDeviceInfo(protocol=%u, msm=%#x, oem=%#x, model=%#x, serial=%#x)",
                          self.protocol_version, self.msm_id, self.oem_id, self.model_id, self.serial);
            return std::string(buffer);
        });

    py::enum_<FirehoseStatus>(module, "FirehoseStatus")
        .value("Unknown", FirehoseStatus::Unknown)
        .value("Ack", FirehoseStatus::Ack)
        .value("Nak", FirehoseStatus::Nak)
        .value("Log", FirehoseStatus::Log);

    py::class_<FirehoseResponse>(module, "FirehoseResponse",
                                 "One parsed Firehose reply.")
        .def_readonly("status", &FirehoseResponse::status)
        .def_readonly("raw_mode", &FirehoseResponse::raw_mode,
                      "The device is switching to raw bytes; a read must follow.")
        .def_readonly("logs", &FirehoseResponse::logs)
        .def_readonly("raw", &FirehoseResponse::raw)
        .def_property_readonly("acknowledged", [](const FirehoseResponse& self) {
            return self.status == FirehoseStatus::Ack;
        })
        .def("__repr__", [](const FirehoseResponse& self) {
            return std::string("FirehoseResponse(") + to_string(self.status)
                   + (self.raw_mode ? ", rawmode" : "") + ", " + std::to_string(self.logs.size())
                   + " log lines)";
        });

    // -- request structs ---------------------------------------------------
    py::class_<ConfigureRequest>(module, "ConfigureRequest")
        .def(py::init<>())
        .def_readwrite("max_payload_size_to_target", &ConfigureRequest::max_payload_size_to_target)
        .def_readwrite("verbose", &ConfigureRequest::verbose)
        .def_readwrite("zero_length_packet_aware", &ConfigureRequest::zero_length_packet_aware)
        .def_readwrite("skip_storage_init", &ConfigureRequest::skip_storage_init)
        .def_readwrite("memory_name", &ConfigureRequest::memory_name,
                       "Value for <configure MemoryName=\"...\">. Empty omits the attribute; "
                       "the exact spelling is device-specific and not guessed here.");

    py::class_<ReadRequest>(module, "ReadRequest")
        .def(py::init<>())
        .def_readwrite("sector_size_in_bytes", &ReadRequest::sector_size_in_bytes)
        .def_readwrite("physical_partition_number", &ReadRequest::physical_partition_number)
        .def_readwrite("start_sector", &ReadRequest::start_sector)
        .def_readwrite("num_partition_sectors", &ReadRequest::num_partition_sectors)
        .def_readwrite("filename", &ReadRequest::filename);

    py::class_<ProgramRequest>(module, "ProgramRequest")
        .def(py::init<>())
        .def_readwrite("sector_size_in_bytes", &ProgramRequest::sector_size_in_bytes)
        .def_readwrite("physical_partition_number", &ProgramRequest::physical_partition_number)
        .def_readwrite("start_sector", &ProgramRequest::start_sector)
        .def_readwrite("num_partition_sectors", &ProgramRequest::num_partition_sectors)
        .def_readwrite("filename", &ProgramRequest::filename);

    py::class_<EraseRequest>(module, "EraseRequest")
        .def(py::init<>())
        .def_readwrite("sector_size_in_bytes", &EraseRequest::sector_size_in_bytes)
        .def_readwrite("physical_partition_number", &EraseRequest::physical_partition_number)
        .def_readwrite("start_sector", &EraseRequest::start_sector)
        .def_readwrite("num_partition_sectors", &EraseRequest::num_partition_sectors);

    py::class_<PowerRequest>(module, "PowerRequest")
        .def(py::init<>())
        .def_readwrite("value", &PowerRequest::value, "\"reset\" or \"off\".")
        .def_readwrite("delay_seconds", &PowerRequest::delay_seconds);

    py::class_<PatchEntry>(module, "PatchEntry",
                           "One <patch> element: a value written into an image already on flash.")
        .def(py::init<>())
        .def_readwrite("filename", &PatchEntry::filename)
        .def_readwrite("value", &PatchEntry::value,
                       "The text to write, e.g. a bootloader version string.")
        .def_readwrite("sector_size_in_bytes", &PatchEntry::sector_size_in_bytes)
        .def_readwrite("physical_partition_number", &PatchEntry::physical_partition_number)
        .def_readwrite("start_sector", &PatchEntry::start_sector)
        .def_readwrite("byte_offset", &PatchEntry::byte_offset,
                       "Offset within the sector where the value starts.")
        .def_readwrite("size_in_bytes", &PatchEntry::size_in_bytes,
                       "Bytes of the value to write. 0 means the whole value.");

    py::class_<RawProgramEntry>(module, "RawProgramEntry",
                                "One <program> element from a rawprogramN.xml.")
        .def(py::init<>())
        .def_readwrite("program", &RawProgramEntry::program,
                       "Where and how much to write; `filename` names the image on disk.")
        .def_readwrite("label", &RawProgramEntry::label,
                       "The GPT partition name this maps to, when the file states one.")
        .def("__repr__", [](const RawProgramEntry& self) {
            return "RawProgramEntry(" + self.program.filename + " -> sector "
                   + std::to_string(self.program.start_sector) + ", "
                   + std::to_string(self.program.num_partition_sectors) + " sectors)";
        });

    py::class_<StorageInfo>(module, "StorageInfo",
                            "Flash geometry as reported by <getstorageinfo>.")
        .def(py::init<>())
        .def_readonly("total_blocks", &StorageInfo::total_blocks)
        .def_readonly("block_size", &StorageInfo::block_size)
        .def_readonly("storage_type", &StorageInfo::storage_type,
                      "\"UFS\", \"eMMC\", ... when the device states it; empty otherwise.")
        .def_readonly("raw_json", &StorageInfo::raw_json,
                      "The JSON object the device sent, including fields not modelled here.")
        .def_property_readonly("total_bytes", &StorageInfo::total_bytes)
        .def("__repr__", [](const StorageInfo& self) {
            return "StorageInfo(" + std::to_string(self.total_blocks) + " x "
                   + std::to_string(self.block_size) + " bytes"
                   + (self.storage_type.empty() ? "" : ", " + self.storage_type) + ")";
        });

    // -- GPT -----------------------------------------------------------------
    py::class_<GptGuid>(module, "GptGuid", "A partition type or unique identifier.")
        .def(py::init<>())
        .def("is_zero", &GptGuid::is_zero)
        .def("to_string", &GptGuid::to_string,
             "Canonical xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx text.")
        .def("__str__", &GptGuid::to_string)
        .def("__repr__", [](const GptGuid& self) { return "GptGuid(" + self.to_string() + ")"; })
        .def("__eq__", [](const GptGuid& self, const GptGuid& other) {
            return self.to_string() == other.to_string();
        });

    py::class_<GptHeader>(module, "GptHeader", "The GPT header stored at LBA 1.")
        .def(py::init<>())
        .def_readonly("revision", &GptHeader::revision)
        .def_readonly("header_size", &GptHeader::header_size)
        .def_readonly("current_lba", &GptHeader::current_lba)
        .def_readonly("backup_lba", &GptHeader::backup_lba)
        .def_readonly("first_usable_lba", &GptHeader::first_usable_lba)
        .def_readonly("last_usable_lba", &GptHeader::last_usable_lba)
        .def_readonly("disk_guid", &GptHeader::disk_guid)
        .def_readonly("part_entry_lba", &GptHeader::part_entry_lba,
                      "Where the entry array lives; read from here after this header.")
        .def_readonly("num_part_entries", &GptHeader::num_part_entries)
        .def_readonly("part_entry_size", &GptHeader::part_entry_size)
        .def_readonly("part_array_crc32", &GptHeader::part_array_crc32)
        .def_property_readonly("revision_string", &GptHeader::revision_string);

    py::class_<GptEntry>(module, "GptEntry", "One partition entry.")
        .def(py::init<>())
        .def_readonly("type_guid", &GptEntry::type_guid)
        .def_readonly("unique_guid", &GptEntry::unique_guid)
        .def_readonly("first_lba", &GptEntry::first_lba)
        .def_readonly("last_lba", &GptEntry::last_lba)
        .def_readonly("attributes", &GptEntry::attributes)
        .def_readonly("name", &GptEntry::name)
        .def_property_readonly("sector_count", &GptEntry::sector_count)
        .def_property_readonly("is_unused", &GptEntry::is_unused)
        .def("size_bytes", &GptEntry::size_bytes, py::arg("sector_size"))
        .def("__repr__", [](const GptEntry& self) {
            return "GptEntry(" + (self.name.empty() ? std::string("<unnamed>") : self.name)
                   + ", LBA " + std::to_string(self.first_lba) + ".." + std::to_string(self.last_lba)
                   + ")";
        });

    py::class_<GptTable>(module, "GptTable", "A parsed partition table.")
        .def(py::init<>())
        .def_readonly("header", &GptTable::header)
        .def_readonly("entries", &GptTable::entries,
                      "Every slot, including unused ones, so the UI can report occupancy.")
        .def("used_entries", &GptTable::used_entries)
        // reference_internal, not the default: the returned pointer aims into this
        // table's own vector, so the Python object must keep the table alive.
        .def("find", &GptTable::find, py::return_value_policy::reference_internal,
             py::arg("name"), "The named partition, or None.")
        .def("total_bytes", &GptTable::total_bytes, py::arg("sector_size"))
        .def("__len__", [](const GptTable& self) { return self.used_entries().size(); })
        .def("__repr__", [](const GptTable& self) {
            return "GptTable(" + std::to_string(self.used_entries().size()) + " of "
                   + std::to_string(self.entries.size()) + " entries, disk "
                   + self.header.disk_guid.to_string() + ")";
        });

    // -- XML builders ------------------------------------------------------
    // Pure functions, exposed so the UI can preview a command before sending it
    // and so the encoding can be tested without a device.
    module.def("build_configure_xml", &huaxin::protocols::qualcomm::build_configure_xml,
               py::arg("request"));
    module.def("build_read_xml", &huaxin::protocols::qualcomm::build_read_xml, py::arg("request"));
    module.def("build_program_xml", &huaxin::protocols::qualcomm::build_program_xml, py::arg("request"));
    module.def("build_erase_xml", &huaxin::protocols::qualcomm::build_erase_xml, py::arg("request"));
    module.def("build_power_xml", &huaxin::protocols::qualcomm::build_power_xml, py::arg("request"));
    module.def("build_ping_xml", &huaxin::protocols::qualcomm::build_ping_xml);
    module.def("build_get_storage_info_xml",
               &huaxin::protocols::qualcomm::build_get_storage_info_xml);
    module.def("parse_firehose_response", &huaxin::protocols::qualcomm::parse_firehose_response,
               py::arg("xml"), "Parses a Firehose reply. Raises ProtocolError on non-XML input.");

    // -- storage info / rawprogram / patch ---------------------------------
    module.def("extract_storage_info",
               [](const FirehoseResponse& response) -> py::object {
                   StorageInfo info;
                   if (!huaxin::protocols::qualcomm::extract_storage_info(response, info)) {
                       return py::none();
                   }
                   return py::cast(info);
               },
               py::arg("response"),
               "Geometry from a <getstorageinfo> reply, or None when the device sent only an ACK.");

    module.def("json_scalar",
               [](const std::string& object, const std::string& key) -> py::object {
                   std::string value;
                   if (!huaxin::protocols::qualcomm::json_scalar(object, key, value)) {
                       return py::none();
                   }
                   return py::cast(value);
               },
               py::arg("object"), py::arg("key"),
               "One scalar member of a JSON object, as text. None when absent.");

    module.def("parse_rawprogram_xml", &huaxin::protocols::qualcomm::parse_rawprogram_xml,
               py::arg("xml"),
               "The <program> elements of a rawprogramN.xml, in file order. Raises "
               "ProtocolError when the document contains none.");
    module.def("parse_patch_xml", &huaxin::protocols::qualcomm::parse_patch_xml,
               py::arg("xml"), "The <patch> elements of a patchN.xml, in file order.");
    module.def("build_patch_xml", &huaxin::protocols::qualcomm::build_patch_xml,
               py::arg("patch"));

    // -- GPT ---------------------------------------------------------------
    module.def("parse_gpt_header",
               [](const py::bytes& data) {
                   const auto buffer = to_buffer(data);
                   return huaxin::protocols::qualcomm::parse_gpt_header(buffer.data(), buffer.size());
               },
               py::arg("data"),
               "Parses a GPT header from the raw bytes of LBA 1. Raises ProtocolError "
               "when the signature is missing or the geometry is implausible.");
    module.def("parse_gpt_entries",
               [](const py::bytes& data, std::uint32_t count, std::uint32_t entry_size) {
                   const auto buffer = to_buffer(data);
                   return huaxin::protocols::qualcomm::parse_gpt_entries(buffer.data(), buffer.size(),
                                                                        count, entry_size);
               },
               py::arg("data"), py::arg("count"), py::arg("entry_size"),
               "Parses a partition entry array. Unused slots are kept, not dropped.");
    module.def("parse_gpt",
               [](const py::bytes& header_data, const py::bytes& entries_data) {
                   const auto header = to_buffer(header_data);
                   const auto entries = to_buffer(entries_data);
                   return huaxin::protocols::qualcomm::parse_gpt(
                       header.data(), header.size(), entries.data(), entries.size());
               },
               py::arg("header_data"), py::arg("entries_data"),
               "Header plus entries, from two buffers.");
    module.def("utf16le_to_utf8",
               [](const py::bytes& data) {
                   const auto buffer = to_buffer(data);
                   const auto* units = reinterpret_cast<const std::uint16_t*>(buffer.data());
                   return huaxin::protocols::qualcomm::utf16le_to_utf8(units, buffer.size() / 2);
               },
               py::arg("data"), "Decodes a UTF-16LE byte string, stopping at the first NUL.");

    // -- session -----------------------------------------------------------
    py::class_<QualcommEdl>(module, "QualcommEdl",
                            "Sahara handshake plus Firehose for a device in EDL mode.")
        // A factory returning unique_ptr rather than a by-value constructor:
        // QualcommEdl owns a USB handle and is deliberately not movable, which
        // is what pybind11's return-by-value init would require.
        .def(py::init([](const py::object& log, const py::object& progress,
                         const py::object& cancelled) {
                 QualcommEdl::Callbacks callbacks;
                 callbacks.log = make_log_callback(log);
                 callbacks.progress = make_progress_callback(progress);
                 callbacks.cancelled = make_cancel_callback(cancelled);
                 return std::make_unique<QualcommEdl>(std::move(callbacks));
             }),
             py::arg("log") = py::none(), py::arg("progress") = py::none(),
             py::arg("cancelled") = py::none(),
             "Callbacks are invoked from the calling thread; log(level, message), "
             "progress(percent, message), cancelled() -> bool.")
        .def_static("device_present", &QualcommEdl::device_present,
                    "True when a device with the EDL USB ID is on the bus, openable or not.")
        .def("connect", [](QualcommEdl& self) { without_gil([&] { self.connect(); }); },
             "Opens the USB link. Raises ProtocolError with driver guidance on failure.")
        .def("disconnect", &QualcommEdl::disconnect, "Closes the USB link.")
        .def("connected", &QualcommEdl::connected)
        .def("describe", &QualcommEdl::describe)
        .def("read_device_info",
             [](QualcommEdl& self) {
                 return without_gil([&] { return self.read_device_info(); });
             },
             "Sahara: reads serial, MSM id and OEM PK hash without uploading anything.")
        .def("load_programmer",
             [](QualcommEdl& self, const std::string& path, bool read_identity) {
                 return without_gil([&] { return self.load_programmer(path, read_identity); });
             },
             py::arg("path"), py::arg("read_identity") = true,
             "Sahara: uploads a Firehose programmer and leaves the device in Firehose mode.")
        .def("programmer_loaded", &QualcommEdl::programmer_loaded,
             "True once Firehose commands will be accepted.")
        .def("device_info", &QualcommEdl::device_info, "Identity from the last Sahara conversation.")
        .def("send_firehose",
             [](QualcommEdl& self, const std::string& xml, unsigned int timeout_ms) {
                 return without_gil([&] { return self.send_firehose(xml, timeout_ms); });
             },
             py::arg("xml"), py::arg("timeout_ms") = 10000,
             "Sends one XML document and returns the parsed reply.")
        .def("configure",
             [](QualcommEdl& self, const ConfigureRequest& request, unsigned int timeout_ms) {
                 return without_gil([&] { return self.configure(request, timeout_ms); });
             },
             py::arg("request"), py::arg("timeout_ms") = 5000)
        .def("power", [](QualcommEdl& self, const PowerRequest& request) {
            return without_gil([&] { return self.power(request); });
        }, py::arg("request"))
        .def("get_storage_info",
             [](QualcommEdl& self, unsigned int lun) {
                 return without_gil([&] { return self.get_storage_info(lun); });
             },
             py::arg("lun") = 0,
             "Firehose <getstorageinfo>: total blocks and block size for one LUN. "
             "Raises ProtocolError when the programmer reports no geometry.")
        .def("read_gpt",
             [](QualcommEdl& self, unsigned int lun, std::uint64_t sector_size) {
                 return without_gil([&] { return self.read_gpt(lun, sector_size); });
             },
             py::arg("lun") = 0, py::arg("sector_size") = 0,
             "Reads and parses the GPT of a LUN. `sector_size` 0 asks the device "
             "for it first, which also fills in the storage geometry.")
        .def("program_partition",
             [](QualcommEdl& self, const ProgramRequest& request, const py::bytes& image,
                unsigned int setup_timeout_ms, unsigned int completion_timeout_ms) {
                 // Copied before releasing the GIL: the Python buffer must not be
                 // touched once other threads may be running.
                 const auto buffer = to_buffer(image);
                 without_gil([&] {
                     self.program_partition(request, buffer, setup_timeout_ms, completion_timeout_ms);
                 });
             },
             py::arg("request"), py::arg("image"),
             py::arg("setup_timeout_ms") = QualcommEdl::kProgramSetupTimeoutMs,
             py::arg("completion_timeout_ms") = QualcommEdl::kProgramCompletionTimeoutMs,
             "Firehose <program>: writes `image` to a partition. Progress is reported "
             "through the callback set by set_progress_callback.")
        .def("read_partition",
             [](QualcommEdl& self, const ReadRequest& request, unsigned int timeout_ms) {
                 return to_bytes(without_gil([&] { return self.read_partition(request, timeout_ms); }));
             },
             py::arg("request"), py::arg("timeout_ms") = QualcommEdl::kReadTimeoutMs,
             "Firehose <read>: reads a sector range and returns it as bytes.")
        .def("erase_sectors",
             [](QualcommEdl& self, const EraseRequest& request, unsigned int timeout_ms) {
                 without_gil([&] { self.erase_sectors(request, timeout_ms); });
             },
             py::arg("request"), py::arg("timeout_ms") = QualcommEdl::kEraseTimeoutMs,
             "Firehose <erase>: erases a sector range.")
        .def("patch",
             [](QualcommEdl& self, const PatchEntry& entry, unsigned int timeout_ms) {
                 return without_gil([&] { return self.patch(entry, timeout_ms); });
             },
             py::arg("entry"), py::arg("timeout_ms") = QualcommEdl::kCommandTimeoutMs,
             "Firehose <patch>: writes a value into an image already on flash, the "
             "step a patchN.xml describes.")
        .def("set_progress_callback",
             [](QualcommEdl& self, const py::object& callable) {
                 if (callable.is_none()) {
                     self.set_progress_callback({});
                     return;
                 }
                 auto function = py::reinterpret_borrow<py::function>(callable);
                 self.set_progress_callback([function](std::uint64_t done, std::uint64_t total) {
                     py::gil_scoped_acquire acquire;
                     try {
                         function(done, total);
                     } catch (py::error_already_set& error) {
                         error.discard_as_unraisable("huaxin flash progress");
                     }
                 });
             },
             py::arg("callback"),
             "Called as callback(bytes_done, bytes_total) while a partition is written "
             "or read. Invoked from the worker thread with the GIL held; a raising "
             "callback is reported as unraisable and does not abort the transfer.")
        .def("ping", [](QualcommEdl& self) {
            return without_gil([&] { return self.ping(); });
        })
        .def_readonly_static("COMMAND_TIMEOUT_MS", &QualcommEdl::kCommandTimeoutMs)
        .def_readonly_static("PROGRAM_SETUP_TIMEOUT_MS", &QualcommEdl::kProgramSetupTimeoutMs)
        .def_readonly_static("PROGRAM_COMPLETION_TIMEOUT_MS", &QualcommEdl::kProgramCompletionTimeoutMs)
        .def_readonly_static("READ_TIMEOUT_MS", &QualcommEdl::kReadTimeoutMs)
        .def_readonly_static("ERASE_TIMEOUT_MS", &QualcommEdl::kEraseTimeoutMs)
        .def_readonly_static("COMMAND_ATTEMPTS", &QualcommEdl::kCommandAttempts,
                             "A NAK is retried this many times before failing.")
        .def("last_response", &QualcommEdl::last_response);
}

}  // namespace huaxin::bindings
