#include <cstdio>
#include <stdexcept>
#include "usb/usb_discovery.h"

using namespace huaxin::usb;
namespace {
int init_result = 0, list_result = 0, descriptor_result = 0;
int exits = 0, frees = 0, lists = 0;
bool matching = true;
libusb_device* devices[2] = {reinterpret_cast<libusb_device*>(1), nullptr};
int LIBUSB_CALL init(libusb_context** context) {
    *context = nullptr; // No real context or USB calls in these tests.
    return init_result;
}
ssize_t LIBUSB_CALL list(libusb_context*, libusb_device*** result) {
    ++lists;
    *result = devices;
    return list_result;
}
int LIBUSB_CALL descriptor(libusb_device*, libusb_device_descriptor* result) {
    result->idVendor = matching ? 0x05c6 : 0x1234;
    result->idProduct = 0x9008;
    return descriptor_result;
}
void LIBUSB_CALL free_list(libusb_device**, int) { ++frees; }
void LIBUSB_CALL exit_context(libusb_context*) { ++exits; }
void require(bool value, const char* label) {
    if (!value) throw std::runtime_error(label);
    std::printf("PASS: %s\n", label);
}
}

int main() {
    const UsbProbeApi api{init, list, descriptor, free_list, exit_context};
    try {
        init_result = LIBUSB_ERROR_OTHER;
        try { probe_usb_device(0x05c6, 0x9008, api); require(false, "init must throw"); }
        catch (const UsbDiscoveryError& e) {
            const std::string text = e.what();
            require(text.find("libusb_init") != std::string::npos &&
                    text.find("05c6:9008") != std::string::npos &&
                    text.find("LIBUSB_ERROR_OTHER") != std::string::npos &&
                    text.find("unknown") != std::string::npos, "init error carries stage, ID and cause");
        }
        require(lists == 0 && exits == 0 && frees == 0, "failed init does not enumerate or clean unowned resources");
        init_result = 0;
        list_result = LIBUSB_ERROR_ACCESS;
        try { probe_usb_device(0x05c6, 0x9008, api); require(false, "enumeration must throw"); }
        catch (const UsbDiscoveryError& e) {
            require(std::string(e.what()).find("libusb_get_device_list") != std::string::npos,
                    "enumeration failure is distinct from an empty bus");
        }
        require(exits == 1 && frees == 0, "enumeration error releases its context");
        list_result = 0;
        require(!probe_usb_device(0x05c6, 0x9008, api), "empty bus reports absent");
        require(exits == 2 && frees == 1, "empty allocated list is freed");
        list_result = 1;
        require(probe_usb_device(0x05c6, 0x9008, api), "matching device reports present");
        require(exits == 3 && frees == 2, "early match releases resources");
        matching = false;
        require(!probe_usb_device(0x05c6, 0x9008, api), "unrelated device reports absent");
        descriptor_result = LIBUSB_ERROR_IO;
        try { probe_usb_device(0x05c6, 0x9008, api); require(false, "descriptor must throw"); }
        catch (const UsbDiscoveryError&) {}
        require(exits == 5 && frees == 4, "descriptor error releases list and context");
    } catch (const std::exception& e) {
        std::fprintf(stderr, "FAIL: %s\n", e.what());
        return 1;
    }
    return 0;
}
