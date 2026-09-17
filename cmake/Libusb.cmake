# =============================================================================
#  libusb-1.0 resolution -> the imported target `huaxin::libusb`
#
#  libusb ships no CMake build system: Unix uses autotools, Windows uses raw
#  .vcxproj files, and there is no CMakeLists.txt anywhere in the tree (not even
#  on master). So there is nothing to add_subdirectory or FetchContent, and three
#  routes are tried in order of preference:
#
#    1. a CMake package   - vcpkg, or a distribution that ships
#                           libusb-1.0-config.cmake
#    2. pkg-config        - the normal case on Linux and macOS
#    3. a source build    - Windows, from the pinned official release archive,
#                           using the source list taken from libusb's own
#                           msvc/libusb_static.vcxproj
#
#  Override the choice with -DHUAXIN_BUILD_LIBUSB=ON to force route 3.
# =============================================================================

set(HUAXIN_LIBUSB_VERSION "1.0.30" CACHE STRING
    "libusb release to build from source (route 3)")
set(HUAXIN_LIBUSB_URL
    "https://github.com/libusb/libusb/releases/download/v${HUAXIN_LIBUSB_VERSION}/libusb-${HUAXIN_LIBUSB_VERSION}.tar.bz2"
    CACHE STRING "libusb source archive URL")
set(HUAXIN_LIBUSB_SHA256
    "fea36f34f9156400209595e300840767ab1a385ede1dc7ee893015aea9c6dbaf"
    CACHE STRING "SHA-256 of the libusb source archive")

option(HUAXIN_BUILD_LIBUSB
    "Build libusb from the pinned release archive instead of using a system copy" OFF)

# --- route 1: CMake package --------------------------------------------------
if(NOT HUAXIN_BUILD_LIBUSB)
    find_package(libusb-1.0 CONFIG QUIET)
endif()

if(libusb-1.0_FOUND)
    add_library(huaxin_libusb INTERFACE)
    add_library(huaxin::libusb ALIAS huaxin_libusb)
    if(TARGET libusb-1.0::libusb-1.0)
        target_link_libraries(huaxin_libusb INTERFACE libusb-1.0::libusb-1.0)
    else()
        # Older or hand-rolled config packages only export variables.
        target_include_directories(huaxin_libusb INTERFACE ${libusb-1.0_INCLUDE_DIRS})
        target_link_libraries(huaxin_libusb INTERFACE ${libusb-1.0_LIBRARIES})
    endif()
    message(STATUS "libusb: CMake package in ${libusb-1.0_DIR}")
    return()
endif()

# --- route 2: pkg-config -----------------------------------------------------
if(UNIX AND NOT HUAXIN_BUILD_LIBUSB)
    find_package(PkgConfig QUIET)
    if(PkgConfig_FOUND)
        pkg_check_modules(LIBUSB QUIET IMPORTED_TARGET libusb-1.0)
        if(TARGET PkgConfig::LIBUSB)
            add_library(huaxin::libusb ALIAS PkgConfig::LIBUSB)
            message(STATUS "libusb: pkg-config, version ${LIBUSB_VERSION}")
            return()
        endif()
    endif()
endif()

# --- route 3: build from source ---------------------------------------------
if(NOT WIN32)
    message(FATAL_ERROR
        "No libusb-1.0 found.\n"
        "  Debian/Ubuntu : sudo apt install libusb-1.0-0-dev\n"
        "  Fedora        : sudo dnf install libusb1-devel\n"
        "  macOS         : brew install libusb\n"
        "Or configure with -DHUAXIN_BUILD_LIBUSB=ON to build the pinned release from source.")
endif()

set(_libusb_archive "${CMAKE_BINARY_DIR}/_deps/libusb-${HUAXIN_LIBUSB_VERSION}.tar.bz2")
set(_libusb_root "${CMAKE_BINARY_DIR}/_deps")
set(_libusb_dir "${_libusb_root}/libusb-${HUAXIN_LIBUSB_VERSION}")

# tar.bz2 on purpose: CMake's bundled libarchive handles it, whereas the .7z that
# libusb also publishes fails with "Lzma library error: Invalid options" and
# leaves a half-extracted tree behind.
if(NOT EXISTS "${_libusb_dir}/libusb/libusb.h")
    message(STATUS "libusb: fetching ${HUAXIN_LIBUSB_URL}")
    file(DOWNLOAD "${HUAXIN_LIBUSB_URL}" "${_libusb_archive}"
         EXPECTED_HASH SHA256=${HUAXIN_LIBUSB_SHA256}
         TLS_VERIFY ON
         STATUS _libusb_download)
    list(GET _libusb_download 0 _libusb_code)
    if(NOT _libusb_code EQUAL 0)
        list(GET _libusb_download 1 _libusb_text)
        file(REMOVE "${_libusb_archive}")
        message(FATAL_ERROR "libusb download failed: ${_libusb_text}")
    endif()
    file(ARCHIVE_EXTRACT INPUT "${_libusb_archive}" DESTINATION "${_libusb_root}")
endif()

if(NOT EXISTS "${_libusb_dir}/libusb/libusb.h")
    message(FATAL_ERROR "libusb sources are missing from ${_libusb_dir}")
endif()

# Source list transcribed from libusb's own msvc/libusb_static.vcxproj.
# windows_hotplug.c is deliberately absent: libusb compiles it only in its
# dedicated *-Hotplug configurations, and the default Windows builds do not, so
# device arrival/removal on Windows is polled (which is what the Scan button is).
set(_libusb_sources
    libusb/core.c
    libusb/descriptor.c
    libusb/hotplug.c
    libusb/io.c
    libusb/strerror.c
    libusb/sync.c
    libusb/os/events_windows.c
    libusb/os/threads_windows.c
    libusb/os/windows_common.c
    libusb/os/windows_usbdk.c
    libusb/os/windows_winusb.c)
list(TRANSFORM _libusb_sources PREPEND "${_libusb_dir}/")

add_library(huaxin_libusb STATIC ${_libusb_sources})
add_library(huaxin::libusb ALIAS huaxin_libusb)
set_target_properties(huaxin_libusb PROPERTIES FOLDER "third_party")

target_include_directories(huaxin_libusb
    PUBLIC  "${_libusb_dir}/libusb"   # libusb.h, libusbi.h, version.h
    PRIVATE "${_libusb_dir}/msvc")    # config.h, the checked-in Windows configuration

# Matches libusb's msvc/Base.props.
target_compile_definitions(huaxin_libusb PRIVATE
    _WIN32_WINNT=_WIN32_WINNT_VISTA
    _CRT_SECURE_NO_WARNINGS)

# The Windows backend calls SetupAPI (SetupDi*) and CfgMgr32 (CM_*). libusb's own
# project relies on those being linked implicitly; naming them here means the
# final link cannot fail on an unresolved import.
target_link_libraries(huaxin_libusb PUBLIC setupapi cfgmgr32)

message(STATUS "libusb: built from source, version ${HUAXIN_LIBUSB_VERSION} (${_libusb_dir})")
