# Packaging Huaxin Tool

Produces a single folder — `dist/HuaxinTool/` — that runs on a machine with no
Python installed.

## Build

```bash
# 1. Build the native backend first. The spec refuses to run without it.
./scripts/build.ps1                      # Windows
./scripts/build.sh                       # Linux / macOS

# 2. Install PyInstaller (not a runtime dependency of the app).
python -m pip install pyinstaller

# 3. Build the bundle.
pyinstaller packaging/huaxin.spec --noconfirm
```

The result is `dist/HuaxinTool/HuaxinTool.exe` plus its dependencies.

## What ends up in the bundle

| Item | Source | Notes |
| ---- | ------ | ----- |
| `huaxin_core.*.pyd` | CMake build → `python/` | The C++ backend, imported as a top-level module. PyInstaller 6 places it in `_internal/`, which is on `sys.path` for a frozen app, so the import works without further configuration. |
| `huaxin/` package | `python/huaxin/` | Including the `.py` sources, which PyInstaller compiles into the archive. |
| Qt runtime | PyQt6 | Pulled in by PyInstaller's PyQt6 hooks. |
| `platform-tools/` | `third_party/platform-tools/`, **if present** | `adb` and `fastboot`. Optional — see below. |
| `drivers/` | `packaging/drivers/`, **if present** | Documentation only. See the driver section. |

## USB drivers — read this before promising a one-click installer

The spec deliberately cannot install USB drivers, and no packaging tool can:

- **WinUSB is a Windows in-box driver.** Nothing needs shipping; what is needed
  is *binding* it to the device's interface, which is a system change.
- **Binding a driver is an administrator action against a specific device.** It
  is done with [Zadig](https://zadig.akeo.ie/) or by installing an INF. Neither
  can be folded into a PyInstaller folder and applied from there.
- **libusbK is a third-party kernel driver** with its own licence and signing.

So the honest first-run experience is:

1. The operator runs the tool, clicks **Check Toolchain**, and is told the
   device is present but cannot be opened.
2. They bind WinUSB to the interface with Zadig once, per device model.
3. After that, everything works without further setup.

The failure messages in `usb/edl_transport.cpp` and `usb/mtk_transport.cpp` say
exactly this when `libusb_open` fails. If you want to reduce that friction, ship
Zadig next to the executable and link to it from an error dialog — but the step
stays manual.

## adb and fastboot

`platform-tools` is bundled **when the folder exists**, and skipped when it does
not. It is absent by default: those binaries are Google's, carrying their own
licence, and silently redistributing them is not a build script's decision.

To include them, download platform-tools and put it at
`third_party/platform-tools/` before building. At run time the app searches, in
order: `HUAXIN_ADB` / `HUAXIN_FASTBOOT`, `PATH`, the SDK locations, and finally
`platform-tools/` next to the executable — so a bundled copy is found without any
configuration, and a system install still wins if the operator has one.

## The log file

`flash_log.txt` is written **next to the executable**, and appended to rather
than truncated: a second run after a failure must not erase the evidence from the
first. If that location is read-only (a Program Files install), logging is
disabled with a warning and the app still runs.

## Verifying a build

```bash
dist/HuaxinTool/HuaxinTool.exe          # should open with no console window
```

The build is only worth trusting once the packaged app has demonstrably loaded
the native backend. `flash_log.txt`, written next to the executable, says so on
its first lines:

```
--- log opened ---
hardware bridge initialised (libusb 1.0.30.12037)
loaded native module huaxin_core 0.5.0
hardware bridge ready (backend 0.5.0)
backend ready (version 0.5.0, libusb 1.0.30.12037)
```

`backend unavailable` in place of those lines means the extension did not make
it into the bundle — the window still opens, because the app is built to survive
that, so the log is the check that matters.

Then check, inside the app:

1. **Check Toolchain** on the ADB/Fastboot tab reports where adb and fastboot
   were found — or that they are missing, which is a finding, not a failure.
2. The log dock fills, and `flash_log.txt` appears next to the executable with
   matching timestamps.
3. Scan Devices lists the attached USB hardware.

## A known diagnostic on forced termination

If you terminate the packaged app with a signal rather than closing its window —
`timeout`, `kill`, Task Manager's *End task* — pybind11 may print this to stderr:

```
pybind11::handle::dec_ref() is being called while the GIL is either not held or
invalid. ... The failing pybind11::handle::dec_ref() call was triggered on a type
object.
```

What was established about it, by bisection rather than by guessing:

* **It does not happen on a normal exit.** Closing the window, or quitting from
  the menu, produces no output at all. It appears only when the process is killed
  from outside while the event loop is running.
* **It is not caused by this project's error handling.** An experimental build
  with the `FlashException` registration and its translator removed entirely
  still printed it, and so does a frozen script that only imports the extension.
* **It is a diagnostic, not a failure.** pybind11 compiles that assertion in for
  Debug builds. The process is being torn down at the time, and the check is
  reporting a handle released during interpreter teardown from a context without
  the GIL.

It is recorded here rather than suppressed with
`PYBIND11_NO_ASSERT_GIL_HELD_INCREF_DECREF`, because switching the check off
would also hide the case it exists to catch — a real GIL violation during a
flash. It is listed as a known issue, not as resolved: the trigger is understood,
the teardown ordering behind it has not been changed.

## Bundle size

About 95 MB for a Windows build, dominated by Qt. The spec excludes the Qt modules this app
never uses (WebEngine, Quick/QML, Multimedia, 3D) and disables UPX, which costs
size but avoids a class of false antivirus positives — a flashing tool that gets
quarantined is worse than a large one.

## Cross-platform notes

- **Windows** is the target the rest of the project is developed and tested
  against.
- **Linux** builds a folder too, but needs `libusb-1.0-0-dev` at build time and
  udev rules at run time for non-root USB access; the bundle does not install
  them.
- **macOS** is untested. The native layer is portable, but nothing here has been
  verified on it.
