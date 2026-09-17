#pragma once

#include <pybind11/pybind11.h>

namespace huaxin::bindings {

/// Registers the MediaTek BROM surface on the `huaxin_core` module.
void register_mediatek(pybind11::module_& module);

}  // namespace huaxin::bindings
