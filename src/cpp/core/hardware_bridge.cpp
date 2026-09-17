#include "core/hardware_bridge.h"

#include <mutex>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "core/device_catalog.h"
#include "core/logger.h"

namespace huaxin::core {

namespace {

/// Bumped alongside project(HuaxinTool VERSION ...) in the root CMakeLists.txt.
/// Injected by CMake; the fallback keeps the file compilable standalone.
#ifdef HUAXIN_CORE_VERSION
constexpr const char* kBackendVersion = HUAXIN_CORE_VERSION;
#else
constexpr const char* kBackendVersion = "0.0.0-unknown";
#endif

}  // namespace

HardwareBridge::HardwareBridge() = default;

HardwareBridge::~HardwareBridge() {
    // shutdown() is noexcept in practice, but a destructor must never let an
    // exception escape - a throwing teardown would call std::terminate.
    try {
        shutdown();
    } catch (...) {
        // Intentionally swallowed: nothing sensible is left to report to.
    }
}

bool HardwareBridge::init() {
    std::lock_guard<std::mutex> lock(m_mutex);

    if (m_initialized) {
        return true;  // idempotent
    }

    try {
        m_usb.open();
    } catch (const std::exception& error) {
        // Reported through last_error() rather than thrown, so the Python side
        // can show it in the UI without an exception unwinding through pybind11.
        m_last_error = error.what();
        return false;
    }

    Logger::instance().info("hardware bridge initialised (libusb "
                            + usb::UsbManager::libusb_version() + ")");

    // Windows note: libusb talks to the bus through its own driver stack, so
    // enumeration works without any driver bound to the target device. Only
    // per-device I/O (Phases 4+) needs WinUSB/libusbK installed via Zadig or
    // an equivalent INF.
    m_last_error.clear();
    m_initialized = true;
    return true;
}

void HardwareBridge::shutdown() {
    std::lock_guard<std::mutex> lock(m_mutex);

    if (!m_initialized && !m_usb.is_open()) {
        return;
    }

    // Phase 4+: a flash session may still be in flight here, so this is also
    // where the worker threads get signalled to stop and joined.
    Logger::instance().info("hardware bridge shutting down");
    m_usb.close();
    m_initialized = false;
}

bool HardwareBridge::is_initialized() const {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_initialized;
}

std::vector<DeviceInfo> HardwareBridge::get_device_list(bool read_string_descriptors,
                                                       bool include_root_hubs) {
    std::lock_guard<std::mutex> lock(m_mutex);

    if (!m_initialized) {
        throw std::runtime_error(
            "HardwareBridge::get_device_list() called before a successful init()"
            + (m_last_error.empty() ? std::string{} : " (" + m_last_error + ")"));
    }

    usb::EnumerateOptions options;
    options.read_string_descriptors = read_string_descriptors;
    options.include_root_hubs = include_root_hubs;
    std::vector<DeviceInfo> devices = m_usb.enumerate(options);
    Logger::instance().debug("enumerated " + std::to_string(devices.size()) + " USB device(s)");
    return devices;
}

std::string HardwareBridge::last_error() const {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_last_error;
}

std::string HardwareBridge::backend_version() const {
    std::lock_guard<std::mutex> lock(m_mutex);
    return std::string(kBackendVersion);
}

std::string HardwareBridge::libusb_version() const {
    return usb::UsbManager::libusb_version();
}

std::size_t HardwareBridge::known_target_count() const {
    return core::known_target_count();
}

}  // namespace huaxin::core
