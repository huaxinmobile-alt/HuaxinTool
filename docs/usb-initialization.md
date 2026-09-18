# USB initialization and discovery

`UsbDiscoveryError` means the host could not initialize libusb, enumerate the
USB bus, or read a device descriptor. It does **not** mean that the phone is
absent. Qualcomm's `device_present()` returns `False` only after a successful
scan finds no matching device. The error retains the failed stage and libusb
error name; the device probe also includes the requested VID/PID.

For Linux, check whether `/dev/bus/usb` exists and is accessible to the account
running the application. A container or VM needs USB passthrough configured by
its host. Run the desktop application on the host when passthrough is unavailable.
The application cannot grant itself USB access or override host restrictions.

For Windows, check that the packaged libusb runtime is available and the OS USB
services are working. A failure during initialization is different from a phone
that enumerates successfully but cannot be opened. For that later case, follow
the device-specific instructions in [the driver guide](../drivers/README.md).

After correcting the host problem, retry discovery. No cached failure or
automatic flashing is involved. EDL discovery errors propagate through the
worker's normal failure reporting instead of showing an absent-phone warning.

The `usb-discovery` CTest suite injects libusb entry points to test initialization,
enumeration, empty buses, matching devices, descriptor failures and cleanup
without hardware. The Qualcomm Python tests mock presence and identity calls,
so they do not accidentally send commands to an attached phone. A successful
test run does not establish real USB access or validate flashing on a phone.
