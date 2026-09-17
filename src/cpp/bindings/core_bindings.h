#pragma once

// =============================================================================
//  pybind11 registration for the core-level types: the error system and the
//  unified progress report. Registered from module.cpp like the vendor
//  bindings, so the module's entry point stays readable.
// =============================================================================

#include <pybind11/pybind11.h>

namespace huaxin::bindings {

void register_core(pybind11::module_& m);

}  // namespace huaxin::bindings
