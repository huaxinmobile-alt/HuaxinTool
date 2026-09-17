#pragma once

#include <pybind11/pybind11.h>

namespace huaxin::bindings {

/// Registers the Samsung PIT parser and Odin packet builders on `huaxin_core`.
void register_samsung(pybind11::module_& module);

}  // namespace huaxin::bindings
