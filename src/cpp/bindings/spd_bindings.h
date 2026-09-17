#pragma once

// =============================================================================
//  pybind11 registration for the Unisoc / Spreadtrum PAC package format.
// =============================================================================

#include <pybind11/pybind11.h>

namespace huaxin::bindings {

void register_spd(pybind11::module_& m);

}  // namespace huaxin::bindings
