# =============================================================================
#  Huaxin Tool - multi-vendor Android firmware flashing / software repair tool
#
#  Root build script. Its only job in Phase 1 is to configure the toolchain and
#  build the native backend as the Python extension module `huaxin_core`,
#  which the PyQt6 front-end imports directly.
#
#  Typical usage (from the repository root):
#     cmake -S . -B build -Dpybind11_DIR="$(python -c "import pybind11,pathlib;print(pybind11.get_cmake_dir())")"
#     cmake --build build --config Release
#
#  Or simply run ./scripts/build.ps1 (Windows) / ./scripts/build.sh (Linux, macOS).
# =============================================================================
cmake_minimum_required(VERSION 3.20)

# C is required as well as CXX: libusb is C, and on Windows we compile it from
# source (see cmake/Libusb.cmake).
project(HuaxinTool
        VERSION 0.8.4
        DESCRIPTION "Multi-vendor Android firmware flashing and software repair tool"
        LANGUAGES C CXX)

# --- Global language settings ------------------------------------------------
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
    set_property(CACHE CMAKE_BUILD_TYPE PROPERTY STRINGS Debug Release RelWithDebInfo MinSizeRel)
endif()

set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)  # clangd / IDE assistance

# --- Options -----------------------------------------------------------------
option(HUAXIN_BUILD_TESTS "Build the native (C++) unit tests" ON)
option(HUAXIN_FETCH_PYBIND11 "Fetch pybind11 via FetchContent when no local copy exists" ON)

# Pinned so builds stay reproducible. Override with -DHUAXIN_PYBIND11_TAG=vX.Y.Z.
# NOTE: Python 3.14 requires pybind11 >= 3.0, so do not downgrade this blindly.
set(HUAXIN_PYBIND11_TAG "v3.1.0" CACHE STRING "pybind11 git tag used by FetchContent")

# Directory that is put on PYTHONPATH. The compiled extension module is staged
# here after every build so `import huaxin_core` works straight from the source tree.
set(HUAXIN_PYTHON_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/python" CACHE PATH "Python source root")

# --- Dependencies ------------------------------------------------------------
find_package(Python3 COMPONENTS Interpreter Development REQUIRED)

find_package(pybind11 CONFIG QUIET)
if(NOT pybind11_FOUND)
    if(NOT HUAXIN_FETCH_PYBIND11)
        message(FATAL_ERROR
            "pybind11 was not found. Run `pip install pybind11`, or configure with "
            "-DHUAXIN_FETCH_PYBIND11=ON to download it automatically.")
    endif()
    message(STATUS "pybind11 not found locally - fetching ${HUAXIN_PYBIND11_TAG} (needs git)")
    include(FetchContent)
    set(PYBIND11_FINDPYTHON ON)  # use the same CMake FindPython3 machinery as this project
    FetchContent_Declare(pybind11
        GIT_REPOSITORY https://github.com/pybind/pybind11.git
        GIT_TAG        ${HUAXIN_PYBIND11_TAG}
        GIT_SHALLOW    TRUE)
    FetchContent_MakeAvailable(pybind11)
endif()

# --- libusb ------------------------------------------------------------------
# Defines the imported target huaxin::libusb.
list(APPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}/cmake")
include(Libusb)

message(STATUS "Huaxin Tool ${PROJECT_VERSION} | pybind11 ${pybind11_VERSION} | Python ${Python3_VERSION} (${Python3_EXECUTABLE})")

if(HUAXIN_BUILD_TESTS)
    enable_testing()
endif()

add_subdirectory(src/cpp)
