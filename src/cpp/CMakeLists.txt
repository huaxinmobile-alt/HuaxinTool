# =============================================================================
#  Native backend -> Python extension module `huaxin_core`
#
#  Layering rule for this project:
#     bindings/  - pybind11 glue only. No hardware access, no protocol logic.
#     core/      - hardware-agnostic types, the VID/PID catalogue and the
#                  HardwareBridge facade.
#     usb/       - libusb transport layer.
#     protocols/ - one subdirectory per vendor protocol implementation.
#  Higher layers may depend on lower ones, never the reverse.
# =============================================================================

set(HUAXIN_CORE_SOURCES
    bindings/module.cpp
    bindings/core_bindings.cpp
    bindings/mediatek_bindings.cpp
    bindings/qualcomm_bindings.cpp
    bindings/samsung_bindings.cpp
    bindings/spd_bindings.cpp
    core/device_catalog.cpp
    core/flash_error.cpp
    core/flash_progress.cpp
    core/flash_throttle.cpp
    core/flash_timeouts.cpp
    core/hardware_bridge.cpp
    core/logger.cpp
    protocols/mediatek/brom.cpp
    protocols/mediatek/da.cpp
    protocols/mediatek/mediatek_brom.cpp
    protocols/mediatek/mediatek_flash.cpp
    protocols/mediatek/scatter.cpp
    protocols/qualcomm/firehose.cpp
    protocols/qualcomm/gpt.cpp
    protocols/qualcomm/qualcomm_edl.cpp
    protocols/samsung/odin.cpp
    protocols/samsung/odin_flash.cpp
    protocols/samsung/tar.cpp
    protocols/spd/bsl.cpp
    protocols/spd/checksum.cpp
    protocols/spd/pac.cpp
    protocols/qualcomm/sahara.cpp
    usb/edl_transport.cpp
    usb/mtk_transport.cpp
    usb/spd_transport.cpp
    usb/usb_manager.cpp
)

pybind11_add_module(huaxin_core MODULE ${HUAXIN_CORE_SOURCES})

# Include the C++ root so headers are always written as "core/foo.h", "usb/bar.h".
target_include_directories(huaxin_core PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}")

target_compile_definitions(huaxin_core PRIVATE
    HUAXIN_CORE_VERSION="${PROJECT_VERSION}"
    HUAXIN_CORE_BUILD_TYPE="$<CONFIG>"
)

find_package(Threads REQUIRED)  # mandatory from Phase 4 on; harmless now

if(MSVC)
    target_compile_options(huaxin_core PRIVATE /W4 /permissive- /utf-8 /EHsc)
    target_compile_definitions(huaxin_core PRIVATE
        NOMINMAX                # keep std::min/std::max usable next to windows.h
        WIN32_LEAN_AND_MEAN
        _CRT_SECURE_NO_WARNINGS)
else()
    target_compile_options(huaxin_core PRIVATE -Wall -Wextra -Wpedantic)
endif()

# libusb. huaxin::libusb carries its own include directories, so they are not
# listed here; on Windows it is built from source by cmake/Libusb.cmake.
target_link_libraries(huaxin_core PRIVATE huaxin::libusb Threads::Threads)

# --- Stage the module into the Python source tree ----------------------------
# So that `import huaxin_core` works when <repo>/python is on sys.path, without
# anyone having to copy files by hand after each build.
add_custom_command(TARGET huaxin_core POST_BUILD
    COMMAND "${CMAKE_COMMAND}" -E make_directory "${HUAXIN_PYTHON_ROOT}"
    COMMAND "${CMAKE_COMMAND}" -E copy_if_different
            "$<TARGET_FILE:huaxin_core>"
            "${HUAXIN_PYTHON_ROOT}/$<TARGET_FILE_NAME:huaxin_core>"
    COMMENT "Staging huaxin_core into ${HUAXIN_PYTHON_ROOT}"
    VERBATIM)

# --- native protocol tests ---------------------------------------------------
# The vendor download protocols cannot be exercised without the matching
# hardware in the right mode, so the parts that *can* be tested in isolation -
# the packet codecs and the handshake state machine, driven through a scripted
# transport - are tested here. That is where the bugs live anyway.
if(HUAXIN_BUILD_TESTS)
    # Each suite links only the sources it exercises, so a binary stays
    # testable on its own. The protocol sources now consult the timeout and
    # speed-cap overrides in core/, which is why those two appear in suites that
    # otherwise have nothing to do with the error system.
    add_executable(huaxin_protocol_tests
        "${CMAKE_SOURCE_DIR}/tests/cpp/test_qualcomm.cpp"
        protocols/qualcomm/firehose.cpp
        protocols/qualcomm/gpt.cpp
        protocols/qualcomm/sahara.cpp
        core/flash_timeouts.cpp)

    add_executable(huaxin_mediatek_tests
        "${CMAKE_SOURCE_DIR}/tests/cpp/test_mediatek.cpp"
        protocols/mediatek/brom.cpp
        protocols/mediatek/da.cpp
        protocols/mediatek/mediatek_flash.cpp
        protocols/mediatek/scatter.cpp
        core/flash_timeouts.cpp)

    add_executable(huaxin_samsung_tests
        "${CMAKE_SOURCE_DIR}/tests/cpp/test_samsung.cpp"
        protocols/samsung/odin.cpp
        protocols/samsung/odin_flash.cpp
        protocols/samsung/tar.cpp
        core/flash_timeouts.cpp)

    # The SPD protocol layer is pure: the PAC parser and the BSL framing have no
    # USB in them, so they are tested without libusb in the link.
    add_executable(huaxin_spd_tests
        "${CMAKE_SOURCE_DIR}/tests/cpp/test_spd.cpp"
        protocols/spd/bsl.cpp
        protocols/spd/checksum.cpp
        protocols/spd/pac.cpp
        core/flash_timeouts.cpp)

    # The error system, the logger and the progress types have no USB in them
    # either, and they are the code that only runs when something has already
    # gone wrong - so they get their own suite rather than being exercised only
    # as a side effect of the protocol tests.
    add_executable(huaxin_core_tests
        "${CMAKE_SOURCE_DIR}/tests/cpp/test_core.cpp"
        core/device_catalog.cpp
        core/flash_error.cpp
        core/flash_progress.cpp
        core/flash_throttle.cpp
        core/flash_timeouts.cpp
        core/logger.cpp)

    foreach(_test_target huaxin_protocol_tests huaxin_mediatek_tests huaxin_samsung_tests huaxin_spd_tests huaxin_core_tests)
        target_include_directories(${_test_target} PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}")
        target_compile_definitions(${_test_target} PRIVATE
            HUAXIN_CORE_VERSION="${PROJECT_VERSION}")
        if(MSVC)
            target_compile_options(${_test_target} PRIVATE /W4 /permissive- /utf-8 /EHsc)
        else()
            target_compile_options(${_test_target} PRIVATE -Wall -Wextra -Wpedantic)
        endif()
        set_target_properties(${_test_target} PROPERTIES FOLDER "tests")
    endforeach()

    enable_testing()
    add_test(NAME qualcomm-protocols COMMAND huaxin_protocol_tests)
    add_test(NAME mediatek-protocols COMMAND huaxin_mediatek_tests)
    add_test(NAME samsung-protocols COMMAND huaxin_samsung_tests)
    add_test(NAME core-errors-and-logging COMMAND huaxin_core_tests)
endif()
