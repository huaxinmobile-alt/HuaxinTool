#pragma once

#include <pybind11/pybind11.h>

namespace huaxin::bindings {

/// Registers the Qualcomm EDL surface (Sahara, Firehose, the XML builders) on
/// the `huaxin_core` module.
void register_qualcomm(pybind11::module_& module);

}  // namespace huaxin::bindings
