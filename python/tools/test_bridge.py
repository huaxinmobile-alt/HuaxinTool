"""Phase 1 smoke test: prove the Python <-> C++ (pybind11) bridge works.

Build first, then run:

    cmake --build build --config Release
    python python/tools/test_bridge.py

Exit code 0 means the bridge is healthy. The script also exercises the error
path (calling into the backend before init()) so a wiring mistake fails loudly
here instead of inside the Qt event loop later.
"""

from __future__ import annotations

import sys
from pathlib import Path

# <repo>/python is the Python source root: it holds both the `huaxin` package and
# the compiled `huaxin_core` extension that CMake stages there after each build.
PYTHON_ROOT = Path(__file__).resolve().parents[1]
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

try:
    import huaxin_core
except ImportError as exc:  # pragma: no cover - this is the failure we explain
    raise SystemExit(
        f"Could not import huaxin_core ({exc}).\n"
        f"Looked in: {PYTHON_ROOT}\n"
        "Build the native module first:\n"
        "    cmake -S . -B build -Dpybind11_DIR=\"$(python -c "
        "\"import pybind11,pathlib;print(pybind11.get_cmake_dir())\")\"\n"
        "    cmake --build build --config Release"
    )


def main() -> int:
    print(f"huaxin_core {huaxin_core.__version__}")
    print(f"module file : {huaxin_core.__file__}")
    print(f"docstring   : {huaxin_core.__doc__.splitlines()[0]}")
    print()

    bridge = huaxin_core.HardwareBridge()
    print(f"backend version        : {bridge.backend_version()}")
    print(f"is_initialized() before: {bridge.is_initialized()}")

    if not bridge.init():
        print("ERROR: init() returned False", file=sys.stderr)
        return 1
    print(f"is_initialized() after : {bridge.is_initialized()}")
    print(f"init() is idempotent   : {bridge.init()}")

    print(f"libusb                 : {bridge.libusb_version()}")
    print(f"catalogue              : {bridge.known_target_count()} known VID:PID targets")
    print()

    devices = bridge.get_device_list()
    targets = [device for device in devices if device.recognised]
    print(f"{len(devices)} USB device(s) visible, {len(targets)} recognised target(s):")
    for index, device in enumerate(devices, start=1):
        print(f"  {index}. [{device.usb_id}] bus {device.bus_number} port {device.port_number}")
        print(f"      vendor={device.vendor!r} mode={device.mode!r} kind={device.kind.name}")
        print(f"      recognised={device.recognised} verified={device.verified} root_hub={device.is_root_hub}")
        if device.product or device.manufacturer or device.serial:
            print(f"      product={device.product!r} manufacturer={device.manufacturer!r}")
            print(f"      serial={device.serial!r}")
        print(f"      repr: {device!r}")

    # Error path: a fresh bridge that was never initialised must raise, and that
    # exception must survive the C++ -> Python translation.
    try:
        huaxin_core.HardwareBridge().get_device_list()
    except RuntimeError as exc:
        print(f"\nuninitialised call correctly raised RuntimeError: {exc}")
    else:
        print("\nERROR: uninitialised get_device_list() did not raise", file=sys.stderr)
        return 1

    bridge.shutdown()
    print(f"\nshutdown() -> is_initialized() = {bridge.is_initialized()}")
    print("\nBridge OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
