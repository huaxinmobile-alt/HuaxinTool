// =============================================================================
//  Python bindings for the MediaTek BROM protocol.
//
//  Same GIL discipline as the Qualcomm bindings: the blocking calls release the
//  GIL so the Qt event loop keeps running, and the Python callbacks re-acquire
//  it before being invoked.
// =============================================================================

#include "bindings/qualcomm_bindings.h"  // for the callback helpers' pattern

#include <pybind11/functional.h>
#include <pybind11/stl.h>

#include <memory>
#include <string>
#include <utility>

#include "protocols/mediatek/brom.h"
#include "protocols/mediatek/da.h"
#include "protocols/mediatek/mediatek_brom.h"
#include "protocols/mediatek/mediatek_flash.h"
#include "protocols/mediatek/scatter.h"

namespace py = pybind11;

using huaxin::protocols::mediatek::BromChipInfo;
using huaxin::protocols::mediatek::BromCommand;
using huaxin::protocols::mediatek::ChipId;
using huaxin::protocols::mediatek::DaCommand;
using huaxin::protocols::mediatek::DaStorage;
using huaxin::protocols::mediatek::EmmcInfo;
using huaxin::protocols::mediatek::EmmcPartition;
using huaxin::protocols::mediatek::FlashInfo;
using huaxin::protocols::mediatek::FlashProgress;
using huaxin::protocols::mediatek::FlashResult;
using huaxin::protocols::mediatek::kDefaultDaLoadAddress;
using huaxin::protocols::mediatek::MediaTekBrom;
using huaxin::protocols::mediatek::NandInfo;
using huaxin::protocols::mediatek::NorInfo;
using huaxin::protocols::mediatek::PacketLengths;
using huaxin::protocols::mediatek::PartitionOutcome;
using huaxin::protocols::mediatek::RamInfo;
using huaxin::protocols::mediatek::ScatterFile;
using huaxin::protocols::mediatek::ScatterGeneral;
using huaxin::protocols::mediatek::ScatterFormat;
using huaxin::protocols::mediatek::ScatterPartition;
using huaxin::protocols::mediatek::TargetConfig;

namespace huaxin::bindings {

void register_mediatek(py::module_& module) {
    py::enum_<BromCommand>(module, "BromCommand", "MediaTek bootrom command codes.")
        .value("Read16", BromCommand::Read16)
        .value("Read32", BromCommand::Read32)
        .value("Write16", BromCommand::Write16)
        .value("Write32", BromCommand::Write32)
        .value("JumpDa", BromCommand::JumpDa)
        .value("SendDa", BromCommand::SendDa)
        .value("GetTargetConfig", BromCommand::GetTargetConfig)
        .value("GetHwCode", BromCommand::GetHwCode)
        .value("GetVersion", BromCommand::GetVersion)
        .value("GetHwSwVer", BromCommand::GetHwSwVer);

    py::class_<TargetConfig>(module, "TargetConfig",
                             "The bootrom's own security configuration.")
        .def(py::init<>())
        .def_readonly("raw", &TargetConfig::raw)
        .def_readonly("status", &TargetConfig::status)
        .def_readonly("secure_boot", &TargetConfig::secure_boot)
        .def_readonly("sla_required", &TargetConfig::sla_required,
                      "Serial link authentication: a signed handshake is needed before a "
                      "download agent will be accepted.")
        .def_readonly("da_authentication", &TargetConfig::da_authentication)
        .def_readonly("sw_jtag", &TargetConfig::sw_jtag)
        .def_readonly("epp_supported", &TargetConfig::epp_supported)
        .def_readonly("certificate_required", &TargetConfig::certificate_required)
        .def_readonly("memory_read_allowed", &TargetConfig::memory_read_allowed)
        .def_readonly("memory_write_allowed", &TargetConfig::memory_write_allowed)
        .def_readonly("cmd_c8_supported", &TargetConfig::cmd_c8_supported)
        .def("describe", &TargetConfig::describe)
        .def("__repr__", [](const TargetConfig& self) {
            char buffer[32];
            std::snprintf(buffer, sizeof(buffer), "TargetConfig(0x%08x, ", self.raw);
            return std::string(buffer) + self.describe() + ")";
        });

    py::class_<BromChipInfo>(module, "BromChipInfo", "Chip identity reported by the bootrom.")
        .def(py::init<>())
        .def_readonly("have_hw_code", &BromChipInfo::have_hw_code)
        .def_readonly("hardware_code", &BromChipInfo::hardware_code,
                      "e.g. 0x0672. A number, not a marketing name - see chip_name.")
        .def_readonly("hardware_version", &BromChipInfo::hardware_version)
        .def_readonly("have_hw_sw_version", &BromChipInfo::have_hw_sw_version)
        .def_readonly("hw_sw_hw_code", &BromChipInfo::hw_sw_hw_code)
        .def_readonly("hw_sw_hw_sub_code", &BromChipInfo::hw_sw_hw_sub_code)
        .def_readonly("hw_sw_hw_version", &BromChipInfo::hw_sw_hw_version)
        .def_readonly("hw_sw_sw_version", &BromChipInfo::hw_sw_sw_version)
        .def_readonly("have_brom_version", &BromChipInfo::have_brom_version)
        .def_readonly("brom_version", &BromChipInfo::brom_version)
        .def_readonly("chip_name", &BromChipInfo::chip_name,
                      "Always empty: the hardware-code to chip-name table is not implemented.")
        .def("__repr__", [](const BromChipInfo& self) {
            char buffer[96];
            std::snprintf(buffer, sizeof(buffer), "BromChipInfo(hwcode=0x%04x, hwver=0x%04x)",
                          self.hardware_code, self.hardware_version);
            return std::string(buffer);
        });

    module.attr("MTK_DA_DEFAULT_LOAD_ADDRESS") = kDefaultDaLoadAddress;

    // -- scatter ------------------------------------------------------------
    py::enum_<ScatterFormat>(module, "ScatterFormat",
                             "Which of the two scatter file shapes a file is in.")
        .value("Unknown", ScatterFormat::Unknown)
        .value("Legacy", ScatterFormat::Legacy, "key = value blocks. The older layout.")
        .value("Modern", ScatterFormat::Modern, "YAML key: value stanzas. The current layout.");

    py::class_<ScatterGeneral>(module, "ScatterGeneral",
                               "The general block at the top of a scatter file.")
        .def(py::init<>())
        .def_readonly("config_version", &ScatterGeneral::config_version)
        .def_readonly("platform", &ScatterGeneral::platform)
        .def_readonly("project", &ScatterGeneral::project)
        .def_readonly("storage", &ScatterGeneral::storage)
        .def_readonly("boot_channel", &ScatterGeneral::boot_channel)
        .def_readonly("block_size", &ScatterGeneral::block_size)
        .def_readonly("extra", &ScatterGeneral::extra)
        .def("is_empty", &ScatterGeneral::is_empty)
        .def("__repr__", [](const ScatterGeneral& self) {
            return "ScatterGeneral(" + (self.platform.empty() ? "no platform" : self.platform)
                   + ", " + (self.storage.empty() ? "no storage" : self.storage) + ")";
        });

    py::class_<ScatterPartition>(module, "ScatterPartition",
                                 "One partition entry from a scatter file.")
        .def(py::init<>())
        .def_readonly("name", &ScatterPartition::name)
        .def_readonly("index", &ScatterPartition::index, "The file's own key, e.g. SYS0.")
        .def_readonly("file_name", &ScatterPartition::file_name)
        .def_readonly("is_download", &ScatterPartition::is_download)
        .def_readonly("is_download_stated", &ScatterPartition::is_download_stated)
        .def_readonly("start_address", &ScatterPartition::start_address, "linear_start_addr, bytes.")
        .def_readonly("size", &ScatterPartition::size, "partition_size, bytes.")
        .def_readonly("region", &ScatterPartition::region)
        .def_readonly("storage", &ScatterPartition::storage)
        .def_readonly("operation_type", &ScatterPartition::operation_type,
                      "Passed through as text. The value set is not verified, so nothing here "
                      "acts on it.")
        .def_readonly("extra", &ScatterPartition::extra,
                      "Every other key in the entry, as written. This is what keeps type, "
                      "boundary_check, reserve and vendor extensions visible without this "
                      "build claiming to know what they mean.")
        .def("downloadable", &ScatterPartition::downloadable)
        .def("describe", &ScatterPartition::describe)
        .def("extra_value", &ScatterPartition::extra_value, py::arg("key"))
        .def("__repr__", [](const ScatterPartition& self) {
            return "ScatterPartition(" + self.name + ", " + std::to_string(self.size) + " bytes)";
        });

    py::class_<ScatterFile>(module, "ScatterFile", "A parsed scatter file.")
        .def(py::init<>())
        .def_readonly("format", &ScatterFile::format)
        .def_readonly("general", &ScatterFile::general)
        .def_readonly("partitions", &ScatterFile::partitions)
        .def("downloads", &ScatterFile::downloads, "The entries that name an image to write.")
        // reference_internal: the pointer aims into this file's own vector, so
        // the Python object has to keep the file alive.
        .def("find", &ScatterFile::find, py::return_value_policy::reference_internal,
             py::arg("name"), "A partition by name, case-insensitively. None when absent.")
        .def("total_download_bytes", &ScatterFile::total_download_bytes)
        .def("unmodelled_keys", &ScatterFile::unmodelled_keys,
             "Keys the parser preserved but does not interpret.")
        .def("__len__", [](const ScatterFile& self) { return self.partitions.size(); })
        .def("__repr__", [](const ScatterFile& self) {
            return std::string("ScatterFile(") + std::to_string(self.partitions.size())
                   + " partitions, " + to_string(self.format) + ")";
        });

    module.def("parse_scatter", &huaxin::protocols::mediatek::parse_scatter, py::arg("text"),
               "Parses scatter text. Raises ProtocolError when it holds no partition entries.");
    module.def("load_scatter", &huaxin::protocols::mediatek::load_scatter, py::arg("path"),
               "Reads and parses a scatter file from disk.");
    module.def("detect_scatter_format", &huaxin::protocols::mediatek::detect_scatter_format,
               py::arg("text"));

    // -- download agent -----------------------------------------------------
    py::enum_<DaCommand>(module, "DaCommand", "Download agent command ids.")
        .value("Format", DaCommand::Format)
        .value("WriteData", DaCommand::WriteData)
        .value("ReadData", DaCommand::ReadData)
        .value("FormatPartition", DaCommand::FormatPartition)
        .value("Shutdown", DaCommand::Shutdown)
        .value("DeviceCtrl", DaCommand::DeviceCtrl)
        .value("InitExtRam", DaCommand::InitExtRam)
        .value("SetupEnvironment", DaCommand::SetupEnvironment)
        .value("GetEmmcInfo", DaCommand::GetEmmcInfo)
        .value("GetDaVersion", DaCommand::GetDaVersion)
        .value("GetPacketLength", DaCommand::GetPacketLength)
        .value("GetConnectionAgent", DaCommand::GetConnectionAgent)
        .value("GetChipId", DaCommand::GetChipId)
        .value("SlaEnabledStatus", DaCommand::SlaEnabledStatus);

    py::enum_<DaStorage>(module, "DaStorage", "Storage types the agent drives.")
        .value("Emmc", DaStorage::Emmc)
        .value("Sdmmc", DaStorage::Sdmmc)
        .value("Nand", DaStorage::Nand)
        .value("NandSpi", DaStorage::NandSpi)
        .value("Nor", DaStorage::Nor)
        .value("Ufs", DaStorage::Ufs);

    py::enum_<EmmcPartition>(module, "EmmcPartition", "eMMC hardware partitions.")
        .value("Boot1", EmmcPartition::Boot1)
        .value("Boot2", EmmcPartition::Boot2)
        .value("Rpmb", EmmcPartition::Rpmb)
        .value("Gp1", EmmcPartition::Gp1)
        .value("Gp2", EmmcPartition::Gp2)
        .value("Gp3", EmmcPartition::Gp3)
        .value("Gp4", EmmcPartition::Gp4)
        .value("User", EmmcPartition::User);

    module.attr("MTK_DA_MAGIC") = huaxin::protocols::mediatek::kDaProtocolMagic;

    module.def("compute_da_checksum",
               [](const py::bytes& data) {
                   const std::string text = data.cast<std::string>();
                   return huaxin::protocols::mediatek::compute_da_checksum(
                       reinterpret_cast<const std::uint8_t*>(text.data()), text.size());
               },
               py::arg("data"),
               "The agent's data checksum: the low 16 bits of the sum of the bytes. NOT the "
               "bootrom's XOR checksum - the two belong to different protocols.");

    module.def("describe_da_status", &huaxin::protocols::mediatek::describe_da_status,
               py::arg("status"), "Names a status code, or reports it as a number.");

    py::class_<EmmcInfo>(module, "EmmcInfo", "eMMC geometry reported by the agent.")
        .def(py::init<>())
        .def_readonly("type", &EmmcInfo::type)
        .def_readonly("block_size", &EmmcInfo::block_size)
        .def_readonly("boot1_size", &EmmcInfo::boot1_size)
        .def_readonly("boot2_size", &EmmcInfo::boot2_size)
        .def_readonly("rpmb_size", &EmmcInfo::rpmb_size)
        .def_readonly("gp1_size", &EmmcInfo::gp1_size)
        .def_readonly("gp2_size", &EmmcInfo::gp2_size)
        .def_readonly("gp3_size", &EmmcInfo::gp3_size)
        .def_readonly("gp4_size", &EmmcInfo::gp4_size)
        .def_readonly("user_size", &EmmcInfo::user_size, "The user area, which is the whole flash.")
        .def_readonly("firmware_version", &EmmcInfo::firmware_version)
        .def_property_readonly("cid", [](const EmmcInfo& self) {
            return py::bytes(reinterpret_cast<const char*>(self.cid.data()), self.cid.size());
        }, "The card identification register, as the device reports it.")
        .def("__repr__", [](const EmmcInfo& self) {
            return "EmmcInfo(" + std::to_string(self.user_size) + " bytes user, "
                   + std::to_string(self.block_size) + "-byte blocks)";
        });

    py::class_<NandInfo>(module, "NandInfo")
        .def(py::init<>())
        .def_readonly("type", &NandInfo::type)
        .def_readonly("page_size", &NandInfo::page_size)
        .def_readonly("block_size", &NandInfo::block_size)
        .def_readonly("spare_size", &NandInfo::spare_size)
        .def_readonly("total_size", &NandInfo::total_size);

    py::class_<NorInfo>(module, "NorInfo")
        .def(py::init<>())
        .def_readonly("type", &NorInfo::type)
        .def_readonly("available_size", &NorInfo::available_size);

    py::class_<RamInfo>(module, "RamInfo")
        .def(py::init<>())
        .def_readonly("is_64bit", &RamInfo::is_64bit)
        .def_property_readonly("sram_base", [](const RamInfo& self) { return self.sram.base_address; })
        .def_property_readonly("sram_size", [](const RamInfo& self) { return self.sram.size; })
        .def_property_readonly("dram_base", [](const RamInfo& self) { return self.dram.base_address; })
        .def_property_readonly("dram_size", [](const RamInfo& self) { return self.dram.size; });

    py::class_<ChipId>(module, "DaChipId", "Chip identity as the running agent reports it.")
        .def(py::init<>())
        .def_readonly("hw_code", &ChipId::hw_code)
        .def_readonly("hw_sub_code", &ChipId::hw_sub_code)
        .def_readonly("hw_version", &ChipId::hw_version)
        .def_readonly("sw_version", &ChipId::sw_version)
        .def_readonly("chip_evolution", &ChipId::chip_evolution)
        .def("__repr__", [](const ChipId& self) {
            char buffer[96];
            std::snprintf(buffer, sizeof(buffer), "DaChipId(hw=0x%04x, sub=0x%04x, ver=0x%04x)",
                          self.hw_code, self.hw_sub_code, self.hw_version);
            return std::string(buffer);
        });

    py::class_<PacketLengths>(module, "PacketLengths",
                              "The transfer sizes the agent asks for. Advisory.")
        .def(py::init<>())
        .def_readonly("write_packet_length", &PacketLengths::write_packet_length)
        .def_readonly("read_packet_length", &PacketLengths::read_packet_length);

    // Named with the vendor prefix because `FlashProgress` in the module
    // namespace is the unified report every vendor fills in (core_bindings.cpp).
    // This is the MediaTek layer's own event, with the field names its protocol
    // uses; the Python wrapper converts it to the unified one.
    py::class_<FlashProgress>(module, "MediaTekFlashProgress",
                              "MediaTek's own progress event for one flash operation: which "
                              "partition, how far, how fast. The UI reads the unified "
                              "FlashProgress instead; this is what the protocol layer emits.")
        .def(py::init<>())
        .def_readonly("phase", &FlashProgress::phase, "writing, reading, erasing or preparing.")
        .def_readonly("partition", &FlashProgress::partition)
        .def_readonly("done", &FlashProgress::done)
        .def_readonly("total", &FlashProgress::total)
        .def_readonly("percent", &FlashProgress::percent)
        .def_readonly("bytes_per_second", &FlashProgress::bytes_per_second,
                      "0 until enough time has passed for an average to mean anything.")
        .def_readonly("index", &FlashProgress::index, "Which partition of how many, 1-based.")
        .def_readonly("count", &FlashProgress::count)
        .def("speed_text", [](const FlashProgress& self) {
            if (self.bytes_per_second <= 0.0) {
                return std::string{};
            }
            char buffer[48];
            std::snprintf(buffer, sizeof(buffer), "%.1f MiB/s", self.bytes_per_second / (1024.0 * 1024.0));
            return std::string(buffer);
        })
        .def("__repr__", [](const FlashProgress& self) {
            return self.phase + " " + self.partition + " " + std::to_string(self.percent) + "%";
        });

    py::class_<PartitionOutcome>(module, "PartitionOutcome", "One partition's fate in a flash run.")
        .def(py::init<>())
        .def_readonly("name", &PartitionOutcome::name)
        .def_readonly("file_name", &PartitionOutcome::file_name)
        .def_readonly("bytes", &PartitionOutcome::bytes)
        .def_readonly("skipped", &PartitionOutcome::skipped)
        .def_readonly("succeeded", &PartitionOutcome::succeeded)
        .def_readonly("error", &PartitionOutcome::error);

    py::class_<FlashResult>(module, "FlashResult",
                            "What a flash run did, partition by partition.")
        .def(py::init<>())
        .def_readonly("partitions", &FlashResult::partitions)
        .def_readonly("completed", &FlashResult::completed)
        .def("written_count", &FlashResult::written_count)
        .def("failed_count", &FlashResult::failed_count)
        .def("total_bytes", &FlashResult::total_bytes)
        .def("written_names", &FlashResult::written_names)
        .def("summary", &FlashResult::summary);

    py::class_<FlashInfo>(module, "FlashInfo", "What the running agent reports about the flash.")
        .def(py::init<>())
        .def_readonly("storage", &FlashInfo::storage)
        .def_readonly("have_storage", &FlashInfo::have_storage)
        .def_readonly("total_size", &FlashInfo::total_size)
        .def_readonly("block_size", &FlashInfo::block_size)
        .def_readonly("emmc", &FlashInfo::emmc)
        .def_readonly("nand", &FlashInfo::nand)
        .def_readonly("nor", &FlashInfo::nor)
        .def_readonly("ram", &FlashInfo::ram)
        .def_readonly("da_version", &FlashInfo::da_version)
        .def_readonly("connection_agent", &FlashInfo::connection_agent,
                      "\"brom\" or \"preloader\": which stage the agent took over from.")
        .def_readonly("sla_enabled", &FlashInfo::sla_enabled)
        .def_readonly("target_config", &FlashInfo::target_config)
        .def("storage_label", &FlashInfo::storage_label)
        .def("describe", &FlashInfo::describe)
        .def("__repr__", [](const FlashInfo& self) { return "FlashInfo(" + self.describe() + ")"; });

    py::class_<MediaTekBrom>(module, "MediaTekBrom",
                             "MediaTek BROM session: handshake, chip identity, download agent.")
        .def(py::init([](const py::object& log, const py::object& progress,
                         const py::object& cancelled) {
                 MediaTekBrom::Callbacks callbacks;
                 if (!log.is_none()) {
                     auto function = py::reinterpret_borrow<py::function>(log);
                     callbacks.log = [function](const std::string& level, const std::string& message) {
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
                 return std::make_unique<MediaTekBrom>(std::move(callbacks));
             }),
             py::arg("log") = py::none(), py::arg("progress") = py::none(),
             py::arg("cancelled") = py::none())
        .def_static("devices_present", &MediaTekBrom::devices_present,
                    "MediaTek USB IDs currently on the bus, e.g. ['0e8d:0003'].")
        .def("connect", [](MediaTekBrom& self) {
            py::gil_scoped_release release;
            self.connect();
        })
        .def("disconnect", &MediaTekBrom::disconnect)
        .def("connected", &MediaTekBrom::connected)
        .def("describe", &MediaTekBrom::describe)
        .def("handshake",
             [](MediaTekBrom& self, bool send_lead_byte) {
                 py::gil_scoped_release release;
                 self.handshake(send_lead_byte);
             },
             py::arg("send_lead_byte") = false,
             "Runs the complemented-echo handshake. Idempotent.")
        .def("handshaked", &MediaTekBrom::handshaked)
        .def("read_chip_info",
             [](MediaTekBrom& self) {
                 py::gil_scoped_release release;
                 return self.read_chip_info();
             },
             "Reads the hardware code, version block and BROM version.")
        .def("read_target_config",
             [](MediaTekBrom& self) {
                 py::gil_scoped_release release;
                 return self.read_target_config();
             },
             "Reads the bootrom's security configuration.")
        .def("load_download_agent",
             [](MediaTekBrom& self, const std::string& path, std::uint32_t address,
                std::uint32_t signature_length, bool start) {
                 py::gil_scoped_release release;
                 self.load_download_agent(path, address, signature_length, start);
             },
             py::arg("path"), py::arg("load_address"),
             py::arg("signature_length") = 0, py::arg("start") = true,
             "Uploads a download agent (SEND_DA) and optionally starts it (JUMP_DA).")
        .def("chip_info", &MediaTekBrom::chip_info)
        // -- download agent -------------------------------------------------
        .def("agent_running", &MediaTekBrom::agent_running,
             "True once a download agent has been uploaded and started. Every flash "
             "operation needs this first.")
        .def("read_flash_info",
             [](MediaTekBrom& self) {
                 py::gil_scoped_release release;
                 return self.read_flash_info();
             },
             "Asks the running agent for its version, the storage geometry and the packet "
             "sizes. A query the agent does not implement leaves a gap in the answer rather "
             "than failing.")
        .def("flash_info", &MediaTekBrom::flash_info, "The info from the last read.")
        .def("set_flash_progress",
             [](MediaTekBrom& self, const py::object& callable) {
                 if (callable.is_none()) {
                     self.set_flash_progress({});
                     return;
                 }
                 auto function = py::reinterpret_borrow<py::function>(callable);
                 self.set_flash_progress([function](const FlashProgress& event) {
                     py::gil_scoped_acquire acquire;
                     try {
                         function(event);
                     } catch (py::error_already_set& error) {
                         error.discard_as_unraisable("huaxin flash progress");
                     }
                 });
             },
             py::arg("callback"),
             "Called with a FlashProgress while a partition is written, read or erased. "
             "Invoked from the worker thread with the GIL held; a raising callback is "
             "reported as unraisable and does not abort the transfer.")
        .def("write_region",
             [](MediaTekBrom& self, const py::bytes& image, std::uint64_t address,
                DaStorage storage, std::uint32_t partition) {
                 const std::string text = image.cast<std::string>();
                 const std::vector<std::uint8_t> bytes(text.begin(), text.end());
                 py::gil_scoped_release release;
                 return self.write_region(bytes, address, storage, partition);
             },
             py::arg("image"), py::arg("address"),
             py::arg("storage") = DaStorage::Emmc,
             py::arg("partition") = static_cast<std::uint32_t>(EmmcPartition::User),
             "Writes an image to a region. Returns the bytes written, which includes "
             "zero padding up to a 512-byte sector boundary.")
        .def("read_region",
             [](MediaTekBrom& self, std::uint64_t address, std::uint64_t length,
                DaStorage storage, std::uint32_t partition) {
                 std::vector<std::uint8_t> data;
                 {
                     py::gil_scoped_release release;
                     data = self.read_region(address, length, storage, partition);
                 }
                 return py::bytes(reinterpret_cast<const char*>(data.data()), data.size());
             },
             py::arg("address"), py::arg("length"), py::arg("storage") = DaStorage::Emmc,
             py::arg("partition") = static_cast<std::uint32_t>(EmmcPartition::User))
        .def("erase_region",
             [](MediaTekBrom& self, std::uint64_t address, std::uint64_t length,
                DaStorage storage, std::uint32_t partition) {
                 py::gil_scoped_release release;
                 self.erase_region(address, length, storage, partition);
             },
             py::arg("address"), py::arg("length"), py::arg("storage") = DaStorage::Emmc,
             py::arg("partition") = static_cast<std::uint32_t>(EmmcPartition::User))
        .def("flash_partition",
             [](MediaTekBrom& self, const ScatterPartition& entry, const std::string& image_path) {
                 py::gil_scoped_release release;
                 return self.flash_partition(entry, image_path);
             },
             py::arg("entry"), py::arg("image_path"))
        .def("flash_scatter",
             [](MediaTekBrom& self, const std::string& scatter_path, const std::string& image_dir,
                bool continue_on_error, const std::vector<std::string>& only) {
                 py::gil_scoped_release release;
                 return self.flash_scatter(scatter_path, image_dir, continue_on_error, only);
             },
             py::arg("scatter_path"), py::arg("image_dir") = "",
             py::arg("continue_on_error") = false, py::arg("only") = std::vector<std::string>{},
             "Replays every downloadable entry of a scatter file. Stops at the first failure "
             "unless continue_on_error is set, and reports what was and was not written either "
             "way. Image paths resolve against image_dir, or the scatter file's own directory.")
        .def("read_back",
             [](MediaTekBrom& self, const std::string& scatter_path,
                const std::string& partition_name, const std::string& destination) {
                 py::gil_scoped_release release;
                 return self.read_back(scatter_path, partition_name, destination);
             },
             py::arg("scatter_path"), py::arg("partition_name"), py::arg("destination"))
        .def("format_scatter",
             [](MediaTekBrom& self, const std::string& scatter_path,
                const std::vector<std::string>& only) {
                 py::gil_scoped_release release;
                 return self.format_scatter(scatter_path, only);
             },
             py::arg("scatter_path"), py::arg("only") = std::vector<std::string>{},
             "Erases every partition the scatter file lists with no image.")
        .def("shutdown_device",
             [](MediaTekBrom& self, unsigned int mode) {
                 py::gil_scoped_release release;
                 self.shutdown_device(mode);
             },
             py::arg("mode") = 0);
}

}  // namespace huaxin::bindings
