#include "core/flash_timeouts.h"

#include <atomic>

namespace huaxin::core {

namespace {

// Atomics rather than a mutex: these are read on every packet, from whichever
// thread is driving the device, and a settings change happens once. The readers
// pay nothing for the writer's convenience.
std::atomic<unsigned int> g_command_override{0};
std::atomic<unsigned int> g_transfer_override{0};
std::atomic<unsigned int> g_connect_override{0};

}  // namespace

unsigned int timeout_ms(TimeoutKind kind, unsigned int fallback) noexcept {
    unsigned int override_value = 0;
    switch (kind) {
        case TimeoutKind::Command:
            override_value = g_command_override.load(std::memory_order_relaxed);
            break;
        case TimeoutKind::Transfer:
            override_value = g_transfer_override.load(std::memory_order_relaxed);
            break;
        case TimeoutKind::Connect:
            override_value = g_connect_override.load(std::memory_order_relaxed);
            break;
    }
    return override_value != 0 ? override_value : fallback;
}

void set_timeout_overrides(unsigned int command_ms, unsigned int transfer_ms,
                           unsigned int connect_ms) noexcept {
    g_command_override.store(command_ms, std::memory_order_relaxed);
    g_transfer_override.store(transfer_ms, std::memory_order_relaxed);
    g_connect_override.store(connect_ms, std::memory_order_relaxed);
}

void timeout_overrides(unsigned int& command_ms, unsigned int& transfer_ms,
                       unsigned int& connect_ms) noexcept {
    command_ms = g_command_override.load(std::memory_order_relaxed);
    transfer_ms = g_transfer_override.load(std::memory_order_relaxed);
    connect_ms = g_connect_override.load(std::memory_order_relaxed);
}

}  // namespace huaxin::core
