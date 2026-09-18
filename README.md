# Huaxin Tool

Multi-vendor Android firmware flashing and software repair tool.

- **Front-end:** Python 3 / PyQt6 (UI and high-level orchestration)
- **Backend:** C++17 (USB/serial transport, vendor protocols, file parsing)
- **Bridge:** pybind11, compiled with CMake into the extension module `huaxin_core`
- **Concurrency:** one dedicated worker thread owns every backend call

A plugin-free alternative to vendor flashing suites: it talks to devices over raw
USB and serial using the documented vendor protocols.

---

## ⚠ Read this before flashing anything

**Flashing firmware can permanently destroy a device.** A phone that fails
mid-write can be left unable to boot, and some failures — a wrong preloader, a
wrong PIT — are not recoverable by reflashing the same package.

- **Back up first.** Anything on the device that matters should be copied off it
  before you connect it here. Several operations below are irreversible.
- **Use the firmware for the exact model.** A package for a similar-looking
  variant can overwrite the boot chain with something the device cannot start.
- **Never unplug during a write.** Wait until the operation reports that it
  finished, or failed, and read what it says. A cancelled or interrupted write
  leaves the partition in an unknown state.
- **This tool is provided as-is, with no warranty.** See `LICENSE`; the third-party
  components and product names are in `THIRD_PARTY_NOTICES.md`. You are
  responsible for the device you connect to it.
- **Verify against your own hardware before relying on it.** See
  [Status](#status) for exactly what has and has not been confirmed. No vendor
  download protocol in this project has been exercised against real hardware —
  they are verified against recorded bytes, vendor specifications and scripted
  transports, which is real verification and is not the same thing as a device
  on your bench.

If a device holds data you cannot lose, use the vendor's own tool.

## Supported targets

| Vendor / mode      | Transport  | Protocol                              | Status              |
| ------------------ | ---------- | ------------------------------------- | ------------------- |
| Android (standard) | USB        | ADB / Fastboot (platform-tools)       | **Working** — drives the official binaries |
| Qualcomm           | USB 9008   | Sahara + Firehose                     | **Sahara + full Firehose**, wired to the UI |
| MediaTek           | USB        | BROM + Preloader (DA injection)       | **BROM + full DA flashing**, wired to the UI |
| Unisoc / SPD       | USB        | Research Download (PAC, BSL)          | **Protocol implemented and unit-tested; not yet bound to Python or wired to its tab** — see [docs/spd-status.md](docs/spd-status.md) |
| Samsung            | USB        | Download mode (Odin, PIT, tar.md5)    | **Protocol implemented and unit-tested; no USB transport and not bound to Python yet** |

"Implemented and unit-tested" means the packet codec and the session state machine
exist in C++ and are covered by the native suites, driven through a scripted
transport. It does **not** mean the tab can flash a device. Those tabs say so on
screen.

## Status

| Phase | Scope                                              | State |
| ----- | -------------------------------------------------- | ----- |
| 1     | Project structure, CMake, pybind11 bridge          | Done  |
| 2     | PyQt6 UI, worker-thread architecture               | Done  |
| 3     | libusb enumeration, real device matching           | Done  |
| 4     | ADB / Fastboot (subprocess)                        | Done  |
| 5     | Qualcomm EDL: Sahara handshake, programmer upload, basic Firehose | Done |
| 5b    | Qualcomm Firehose: GPT read, partition read/write/erase, rawprogram + patch replay | Done |
| 6     | MediaTek BROM: handshake, chip id, download agent  | Done  |
| 6b    | MediaTek DA: flash info, scatter replay, readback, erase | Done |
| 7     | Samsung: PIT parser, Odin session, tar.md5 reader  | Done in C++, 97 native checks |
| 7     | Unisoc Research Download: PAC parser (both layouts), BSL protocol, four checksums | Done in C++, 122 native checks |
| 8     | Unified error system, centralised logging, progress reporting, settings, device database | Done |
| 8     | USB driver guide, PyInstaller packaging, documentation, test suite | Done |
| 9a    | Unisoc: session facade, bindings, wrapper, tab actions | Done — reads a device; package replay is not offered, and `docs/spd-status.md` says why |
| 9b    | Samsung: USB transport, session, bindings, wrapper, tab actions | **Next** |

Device discovery is real: the backend enumerates the USB bus through libusb and
identifies each device by VID/PID against a catalogue. ADB and Fastboot work —
they drive the official platform-tools binaries. The Qualcomm path runs a full
Firehose session: read the partition table, read, write and erase partitions,
and replay a `rawprogram0.xml` + `patch0.xml` package. The MediaTek path runs the
bootrom handshake, uploads a download agent, and then drives that agent's own
protocol to read the flash geometry, replay a scatter package, read partitions
back and erase them.

Unisoc now speaks to a device as well: a phone in Research Download mode can be
opened, asked what it is, read back, reset and powered off. What that tab does
*not* do is replay a `.pac`, and it says so in as many words — the FDL1-to-FDL2
handover is not settled by any source this project holds, and a guessed sequence
against a phone is worse than no sequence.

Samsung is the remaining gap: its PIT parser, Odin session and `.tar.md5` reader
are implemented and tested, but there is no USB transport behind them, so a
device in Download mode can be listed and its packages inspected and nothing
more. That tab says exactly that rather than offering a button that cannot work.

## Repository layout

```
HUAXIN TOOL/
├── CMakeLists.txt              Root build: toolchain, dependencies, target tree
├── requirements.txt            Python dependencies (pybind11, PyQt6)
├── README.md                   This file
├── LICENSE                     MIT
├── THIRD_PARTY_NOTICES.md      What the components and product names are
├── cmake/Libusb.cmake          libusb resolution (package > pkg-config > source)
├── src/cpp/                    Native backend  ->  builds `huaxin_core`
│   ├── CMakeLists.txt          Extension module target, staging, native test targets
│   ├── bindings/               pybind11 glue only - no hardware logic
│   │   ├── module.cpp          Module entry point, core types, the Logger
│   │   ├── core_bindings.cpp   Errors, progress, the retry policy, the translator
│   │   └── {qualcomm,mediatek,samsung}_bindings.cpp
│   ├── core/
│   │   ├── device_info.h       DeviceInfo: one device as it appears on the bus
│   │   ├── device_catalog.*    VID/PID -> vendor, mode and verification status
│   │   ├── hardware_bridge.*   The facade the Python side talks to
│   │   ├── flash_error.*       FlashError, FlashException, classify(), with_retry()
│   │   ├── flash_progress.*    FlashProgress + the 1%-or-1-second update rule
│   │   ├── flash_timeouts.*    Fallback-aware timeout overrides
│   │   ├── flash_throttle.*    The optional transfer speed cap
│   │   └── logger.*            The process-wide log: levels, rotation, colour
│   ├── usb/
│   │   ├── usb_manager.*       libusb context + enumeration
│   │   ├── edl_transport.*     libusb bulk link to a device in EDL mode
│   │   ├── mtk_transport.*     MediaTek BROM / Preloader
│   │   ├── spd_transport.*     Unisoc Research Download
│   │   └── usb_errors.h        Disconnection detection and classification
│   ├── protocols/qualcomm/
│   │   ├── sahara.*            Sahara packet codec, handshake, IByteTransport
│   │   ├── firehose.*          Firehose XML builders, response parser, rawprogram/patch
│   │   ├── gpt.*               GPT header/entry parser (UEFI spec, via qdl)
│   │   └── qualcomm_edl.*      The facade the UI drives
│   ├── protocols/mediatek/
│   │   ├── brom.*              BROM framing, handshake, DA upload
│   │   ├── da.*                Download agent protocol: framing, geometry, read/write/erase
│   │   ├── scatter.*           MTK scatter file parser (legacy + modern)
│   │   ├── mediatek_flash.*    FlashInfo/FlashResult value types
│   │   └── mediatek_brom.*     The facade the UI drives
│   ├── protocols/samsung/
│   │   ├── odin.*              Control packets, the handshake, the session
│   │   ├── odin_flash.*        PIT build/parse, file transfer, partition writes
│   │   └── tar.*               tar reader and the appended .tar.md5 digest
│   └── protocols/spd/
│       ├── pac.*               The .pac container, both layouts, FDL extraction
│       ├── bsl.*               HDLC/BSL framing, the command set, the session
│       └── checksum.*          CRC-16/ARC, CRC-16/XMODEM, Spreadtrum sum, CRC-32
├── python/                     Python source root - put this on PYTHONPATH
│   ├── main.py                 Launcher: python python/main.py
│   ├── huaxin/
│   │   ├── app.py              QApplication bootstrap, theme, exception routing
│   │   ├── core/
│   │   │   ├── backend.py      Backend (thread-confined) + BackendService (Qt)
│   │   │   ├── config.py       The JSON settings file, with validation
│   │   │   ├── devices.py      Device profiles: aliases, settings, known issues
│   │   │   ├── errors.py       Classification, translation, and the retry loop
│   │   │   ├── parsers.py      Reading .pac, .pit and .tar.md5 packages
│   │   │   ├── adb_fastboot_wrapper.py   adb/fastboot subprocess driver
│   │   │   ├── qualcomm.py     EDL operations (Sahara / Firehose)
│   │   │   ├── mediatek.py     BROM operations (handshake / DA)
│   │   │   ├── samsung.py      PIT parsing; Odin transfer pending its transport
│   │   │   └── unisoc.py       Device check and honest status - see docs/spd-status.md
│   │   ├── workers/worker.py   Job / JobContext / WorkerThread
│   │   ├── ui/tokens.py        Design tokens: three themes, metrics
│   │   ├── ui/styles/huaxin.qss  The stylesheet, as a template of @tokens
│   │   ├── ui/style.py         Token substitution, the Qt palette, restyling
│   │   ├── ui/icons.py         40 SVG icons, recoloured at render time
│   │   ├── ui/components.py    Buttons, cards, chips, tables, progress, inputs
│   │   ├── ui/animations.py    Fade, hover, collapse, stagger; respects reduce-motion
│   │   ├── ui/theme_controller.py  Live theme switching
│   │   ├── ui/titlebar.py      Frameless window with custom chrome
│   │   ├── ui/gallery.py       Every component on one page
│   │   ├── ui/tabkit.py         Tab furniture: header, badge, device box, action grid
│   │   ├── ui/tabviews.py       Package displays: PAC contents, PIT, firmware members
│   │   ├── ui/theme.py         Back-compatible facade over the above
│   │   ├── ui/safe_slot.py     Keeps a broken slot from aborting the process
│   │   ├── ui/widgets.py       DevicePanel, DeviceTable, LogConsole
│   │   ├── ui/dialogs.py       Flash / erase / wipe dialogs and confirmations
│   │   ├── ui/settings_dialog.py  Settings, and the USB driver help
│   │   ├── ui/panels.py        One tab per vendor
│   │   └── ui/main_window.py   Frameless window, toolbar, menus, docks, tabs
│   ├── tools/                  test_bridge.py, screenshot_ui.py
│   └── huaxin_core*.pyd|.so    Compiled backend, staged here by CMake
├── tests/                      Python suites (see Test below)
│   ├── test_integration.py     Settings, device database, error layer, retries
│   ├── startup_check.py        Starts and stops the real app; no window needed
│   └── cpp/                    Native suites, run without hardware
├── drivers/                    Per-vendor USB driver folders, the guide and the helper script
├── packaging/                  huaxin.spec, packaging README, settings template
├── scripts/                    build.ps1, build.sh
├── docs/
│   ├── architecture.md         The four layers, the threading model, why the boundaries are here
│   ├── design-system.md        Tokens, components, animation rules, how to add an icon
│   ├── contributing.md         The rules, what a test has to do, how to add a vendor
│   ├── errors.md               The seven classifications and how they are chosen
│   ├── firehose-commands.md    Every Firehose command this tool sends, with provenance
│   ├── mtk-da-protocol.md      The download agent protocol: framing, commands, statuses
│   ├── mtk-scatter-format.md   Both MTK scatter formats, and what is not verified
│   ├── mediatek-notes.md       BROM/DA protocol notes and the endianness traps
│   ├── spd-status.md           Unisoc: what is done, what is unverified, what remains
│   ├── spd-pac-format.md       The .pac container, both layouts, what is not interpreted
│   ├── spd-bsl-protocol.md     The Research Download wire protocol and the checksum trap
│   └── screenshots/            Rendered UI captures
├── third_party/  assets/
```

Layering rule: `bindings` → `core` → `usb`/`protocols`. Dependencies point
downwards only, and `bindings/` never contains hardware code. On the Python side
the UI never touches the native bridge: it goes through `BackendService`.

## Build

Prerequisites: Python 3.9+ (pybind11 ≥ 3.0 is required for Python 3.14), CMake
≥ 3.20, and a C++17 compiler (MSVC 2019+ on Windows, GCC/Clang on Linux/macOS).

```bash
python -m pip install -r requirements.txt

# Windows (PowerShell)
./scripts/build.ps1

# Linux / macOS
./scripts/build.sh          # needs libusb-1.0-0-dev / libusb via brew
```

### libusb

libusb ships no CMake build system at all — autotools on Unix, raw `.vcxproj`
files on Windows, and no `CMakeLists.txt` anywhere in the tree — so there is
nothing to `add_subdirectory`. `cmake/Libusb.cmake` therefore tries, in order:

1. a CMake package (`find_package(libusb-1.0 CONFIG)`) — vcpkg or a distro that
   provides one;
2. pkg-config — the normal case on Linux and macOS;
3. a source build from the pinned official release archive, which is what
   happens on Windows. The source list is transcribed from libusb's own
   `msvc/libusb_static.vcxproj`, and the download is pinned by SHA-256.

Force the last route with `-DHUAXIN_BUILD_LIBUSB=ON`.

Or configure CMake by hand:

```bash
BRIDGE_DIR="$(python -c 'import pybind11, pathlib; print(pybind11.get_cmake_dir())')"
cmake -S . -B build -Dpybind11_DIR="$BRIDGE_DIR" -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release
```

Every build copies the resulting extension module into `python/`, so the Python
side always imports the binary that was just produced.

## Run

```bash
python python/main.py
```

Then press **Scan Devices** (F5). The device list fills from the backend, the log
console shows what happened, and selecting a device enables that vendor's actions.

The window layout (dock sizes, visibility, geometry) is remembered between runs;
**View → Reset Layout** puts it back.

## Install

Three ways, depending on what you want.

### From a release build (no Python needed)

Unpack `HuaxinTool.zip` anywhere and run `HuaxinTool.exe`. It is a folder, not an
installer: nothing is written to Program Files and nothing is added to the
registry. Keep the folder together — the executable needs the `_internal`
directory beside it.

`flash_log.txt` is written next to the executable, and `drivers\` holds the USB
driver guide and helper script.

**Windows will warn you the first time.** The build is not code-signed, so
SmartScreen says "Windows protected your PC". *More info* → *Run anyway*. If that
is not acceptable, build it yourself from source — see below.

### From source

Requirements: Python 3.9+ (3.14 is what this is developed against), CMake ≥ 3.20,
and a C++17 compiler (MSVC 2019+ on Windows, GCC or Clang elsewhere).

```bash
python -m pip install -r requirements.txt
./scripts/build.ps1                 # Windows   (builds the native backend)
./scripts/build.sh                  # Linux / macOS
python python/main.py
```

`scripts/build.ps1` configures CMake, builds libusb from source if no system copy
is found, compiles `huaxin_core`, and stages the compiled module into `python/`
so `import huaxin_core` works straight from the source tree.

### As a single folder to hand to someone else

```bash
./scripts/build.ps1
python -m pip install pyinstaller
pyinstaller packaging/huaxin.spec --noconfirm --clean
```

See [packaging/README.md](packaging/README.md) for what lands in the bundle and
how to verify one.

### First run

1. **Install the USB driver for your device's flash mode.** This is the one step
   that cannot be automated and it is where most "the device is not detected"
   problems come from. Start with [drivers/README.md](drivers/README.md), or
   **Help → USB driver help** in the app.
2. Scan for devices. If the list is empty, see
   [Troubleshooting](#troubleshooting) — and note that turning off
   *Read USB serial and model strings* in Settings makes a scan much faster.
3. Change the settings if you need to: **File → Settings** (`Ctrl+,`) covers
   timeouts, retries, logging and behaviour, and they are stored in
   `%APPDATA%\HuaxinTool\settings.json` — a file you can copy to another machine.

## Using it

### Android (ADB / Fastboot) — works today

The device must be booted with USB debugging on, or in Fastboot mode.

| Action | What it does |
|---|---|
| Check Toolchain | Reports where `adb` and `fastboot` were found, and their versions. Start here if a button does nothing. |
| List Devices | `adb devices -l` — shows what ADB can see. |
| Reboot → System / Bootloader / Recovery | The three reboots, with a confirmation. |
| Flash Partition | `fastboot flash <partition> <image>`. Shows the exact command before running it. |
| Erase Partition | `fastboot erase <partition>`. **Destructive**, confirmed. |
| Sideload | `adb sideload <zip>` for an OTA package. |

### Qualcomm EDL (9008) — works today

Put the device into EDL: power off, then hold both volume keys while connecting,
or use `adb reboot edl`. It appears as `05c6:9008`.

| Action | What it does |
|---|---|
| Configure Device | Sahara handshake, uploads a Firehose programmer, and reads the storage geometry. **Do this first** — every other action needs it. |
| Read GPT | Reads and decodes the partition table into the table below. |
| Flash Firmware | Replays a `rawprogram0.xml` (plus `patch0.xml` if present) — the normal way to flash a full package. |
| Read / Flash / Erase Partition | Single-partition work, by name. |
| Reset | Leaves EDL and boots the device. |

UFS and eMMC are both handled; the sector size comes from the device rather than
being assumed. The programmer is whatever your firmware package ships — a generic
one usually fails on a secure-boot device, and the tool reports that as an
authentication failure rather than retrying it.

### MediaTek (BROM / Preloader) — works today

Power the device off completely; it enters BROM as it connects. If it shows up as
Preloader instead, that is fine — both modes are handled.

| Action | What it does |
|---|---|
| Connect (BROM handshake) | The bootrom handshake. Required before anything else. |
| Upload DA | Sends the Download Agent. |
| Read Flash Info | Chip ID and flash geometry. |
| Flash Scatter Package | Replays a scatter file and its images — the normal full-package path. |
| Read / Erase Partition | Single-partition work. |

Secure-boot devices need the DA from their own firmware package; a generic DA
stalls, and the tool reports that as an authentication failure.

### Where the other two stand

**Unisoc (Research Download)** and **Samsung (Download mode)** can read their
firmware packages, and cannot yet write to a device.

Reading is real and useful: the Unisoc tab opens a `.pac` and shows its header,
entry table and loaders; the Samsung tab opens a `.pit` and a `.tar.md5` and
shows the partitions and the package members, mapping each member to the
partition it belongs to. That is the check worth making before anything touches
hardware — it is how a wrong or truncated download gets caught.

Writing is not implemented for either vendor. The protocols are complete and
covered by the native suites (133 checks for Unisoc, 107 for Samsung), but
neither has a USB transport and neither is exposed for writing, so those buttons
say so rather than offering a step that cannot be undone. `docs/spd-status.md`
lists exactly what is done, what is unverified, and what remains.

### The vendor tabs

All five tabs are built from the same furniture, so they look and behave alike:
a title with a one-line description of the mode, a status badge saying what works
(**green** ready, **amber** partial, **red** unavailable), a device box that
turns accent-coloured when a device is selected, a two-column action grid, and a
line of help at the bottom.

Every button carries a tooltip; destructive ones are outlined in red and confirm
before acting; everything is disabled while a job is running. Tables are
sortable, styled for the dark theme, and have a right-click menu with actions
that depend on the row — copying a partition name, showing an entry's details.

| Tab | Buttons | Tables |
|---|---|---|
| ADB / Fastboot | 10 | — |
| Qualcomm (EDL) | 12 | GPT: name, start LBA, sectors, size, LUN, type GUID |
| MediaTek (BROM) | 11 | Scatter plan: name, index, action, address, size, region, image |
| SPD / Unisoc | 6 | PAC contents: file ID, name, role, size, address, offset, notes |
| Samsung | 6 | PIT partitions, and package members with their partition mapping |

## The interface

Dark by default, with a custom title bar, a theme picker, and three colour
schemes: **Midnight** (default), **Graphite** (neutral greys, for a long shift),
and **Daylight** (light, for a bench with a window behind it). Switch them from
the picker in the title bar; the choice is remembered.

Every colour, radius, font size and animation duration is a token in
`python/huaxin/ui/tokens.py`, and the stylesheet is a template of `@token`
placeholders that the loader fills in. That is what makes a theme switch one
substitution rather than a second stylesheet, and it is why no control gets left
behind.

Contrast is checked rather than judged: every foreground/background pair the
interface uses is held to WCAG AA (4.5:1 for body text, 3:1 for non-text), and a
test reports the measured ratio for each. The worst pair across the three themes
is 4.70:1.

**Help → Component gallery** opens every button, input, indicator and surface in
one window, in whatever theme is active. It is how to see what the design system
contains without reading the source, and it is the first thing to check after
changing a colour.

### Working with the interface

| | |
|---|---|
| **Drag a package onto the window** | A `.pac` opens in the Unisoc tab, a `.tar.md5` or `.pit` in the Samsung tab. The overlay says where the file is going before you let go, and says so plainly when the file is not one this tool reads. |
| **Ctrl+S / Ctrl+L** | Save the log to a file / clear it. Both are in the console's right-click menu as well. |
| **F5, Esc, F1, Ctrl+,** | Scan, cancel the running operation, About, Settings. `Ctrl+.` also cancels, for anyone used to Qt's own binding. |
| **Alt+1 … Alt+5** | Jump to a vendor tab. Tabs are built the first time they are opened, so a launch only pays for the tab you are looking at. |
| **Right-click** | On a device: copy its USB ID, description or full detail. On the log: copy, save or clear. On a table row: that row's actions. |
| **Notifications** | Bottom-right. They never take focus, they pause while the pointer is over them, and everything they say is also written to the log. |

The status bar carries a live readout while a job runs: what is happening, how
fast, how long it has been going and how much is left. An ETA the device reported
is shown plainly; one the tool worked out from the rate of progress is marked
with a `~`, because an estimate presented as a fact is how somebody ends up
watching "2 minutes left" for twenty.

See [docs/design-system.md](docs/design-system.md) for the rules, how to add an
icon, and how a visual change is verified.

## USB drivers

A device with a warning triangle in Device Manager will never be flashed. The
drivers and the guide are in [`drivers/`](drivers/README.md); the app links to the
same page under **Help → USB driver help**.

```
drivers\install_drivers.bat          # run as administrator
```

The script checks what is bound for each vendor, tells you what is missing, and
offers the vendor installer one vendor at a time. It does not install all four
"just in case" — binding a driver to something that already works is how a
working setup gets broken.

Full instructions, including the Zadig fallback and how to undo a binding, are in
[drivers/README.md](drivers/README.md).

## Troubleshooting

### The device does not appear at all

In rough order of likelihood:

1. **It is not in the right mode.** Every vendor needs its own key combination,
   and most require the device to be powered off first. A device that boots
   normally exposes no flash interface.
2. **The cable is charge-only.** More common than it sounds. Use the one the
   device shipped with.
3. **The port cannot hold the link.** Use a rear USB port directly on the
   motherboard. Hubs, front-panel headers and extension cables fail in exactly
   this way: the device enumerates and then disappears during a transfer.
4. **The driver is not bound.** Device Manager, with the device in flash mode:
   a warning triangle under *Other devices* is the signature. See
   [drivers/README.md](drivers/README.md).
5. **Another program holds the interface.** Samsung's software, phone managers,
   the ADB server and every other flashing tool seize these interfaces. Close
   them.
6. **The device is damaged.** If nothing appears in any mode on any machine, the
   USB or the boot ROM is the problem, and no host-side change will fix it.

Turn on **Show USB root hubs** in Settings to confirm the bus itself is visible.

### It appears, but every action fails immediately

Read the log. **Help → Open log folder** takes you to it, and the failure is
classified on the line it is reported on:

| In the log | What it means | What to do |
|---|---|---|
| `USB_ERROR` | The device went away, or stopped answering. | Reconnect with a known-good cable into a rear port. The tool has already retried. |
| `PROTOCOL_ERROR` | It answered, and the answer did not fit the protocol. | Disconnect, put it back into the right mode, start again from the beginning. The device may be mid-write. |
| `FILE_ERROR` | A missing, corrupt or unsupported file. | Check the package; verify its checksum. |
| `FLASH_ERROR` | The device refused or failed the write. | **Do not unplug.** Retry once; if it fails again, flash the whole package. |
| `AUTH_ERROR` | Secure boot, SLA, or a locked bootloader. | You need signed firmware or an authorised account. Another package will not help. |
| `INTERNAL_ERROR` | A bug in this tool. | Save the log and report it. |

Every one of these lines carries a `fix:` sentence. That is the intended reading.

### The UI freezes

It should not: every device operation runs on a worker thread, and the UI thread
is forbidden from calling into the backend at all. If it does freeze, the log
will show which operation was in flight — press **Cancel Operation**, and set a
shorter *Command timeout* in Settings if a device consistently stalls.

### A flash fails partway through

**Do not unplug the device.** Reflash the complete package from the beginning
rather than the partition that failed: the partition that was being written is in
an unknown state, and a partial write of a boot-critical partition is worse than
no write at all.

### The log is enormous

Set the level to *warning* in Settings, or turn the console mirror off. The file
rotates at 10 MB into a single `.1` backup, so it cannot grow without bound.

## FAQ

**Does it need root, or a specific Windows version?**
No root on the device for the flash modes. On Windows, 10 or 11 64-bit; the
native backend is built from source by the build script, so no redistributable is
needed. On Linux, a udev rule for non-root USB access — see
[drivers/README.md](drivers/README.md).

**Is there a language setting?**
No, and there deliberately is not one. The interface strings have never been
extracted for translation, so a language picker would change nothing on screen —
and a settings control that does not change anything is exactly what
`tests/test_integration.py` exists to catch. Doing it properly means a Qt
translation pass: wrap every user-facing string in `self.tr()`, extract with
`pylupdate6`, ship compiled `.qm` files beside the executable, and load one with
`QTranslator` at startup. That is a real piece of work, and half of it — the
picker without the translations — is worse than none.

**Does it work on macOS?**
The native layer is portable and the code has no Windows-specific dependencies,
but nothing here has been verified on macOS. Treat it as untested.

**Where are the settings stored?**
`%APPDATA%\HuaxinTool\settings.json` on Windows,
`~/.config/HuaxinTool/settings.json` on Linux. Human-readable JSON, because an
operator who has a working setup wants to copy it to the next machine. Every key
in it changes something; see [docs/architecture.md](docs/architecture.md#settings-and-the-rule-about-them). An
administrator can drop a `settings.template.json` beside the executable and every
new user starts from it — see `packaging/settings.template.json`. A user's own
saved settings always win over the template.

**Where is the log?**
Next to the executable (`flash_log.txt`), appended rather than truncated, so a
second run after a failure does not erase the first. **Help → Open log folder**.
With *Save the log automatically* on, a copy of each session is also kept in
`sessions/` beside it, and the ten most recent are kept.

**Do the timeout and speed settings actually do anything?**
Yes, and they apply to every vendor at once. The three timeouts and the speed cap
are published into the native layer when the settings change, so they reach the
protocol code rather than sitting in a file. Leaving a timeout at its default
leaves each protocol's own value alone - the MediaTek bootrom still gives up after
one second on a dead link, which thirty seconds would make worse, not better. The
speed cap slows the whole conversation including command frames, which is the
point for a link that cannot take the pace.

**Can I flash a phone I do not own, or one that is locked?**
A locked bootloader is a device security policy, and the tool reports the refusal
as an authentication failure. It does not attempt to bypass it, and it will not
pretend a different firmware package will help.

**Why does it refuse to retry a failed write?**
Because retrying blindly is how a device that was merely mid-write becomes a
device with a half-written bootloader. The tool tells you to retry once,
deliberately, and to reflash the whole package if that fails. It does not make
that decision for you. See [docs/errors.md](docs/errors.md) for the reasoning
behind every classification.

**Can it recover a bricked device?**
Sometimes. A Qualcomm device that still reaches EDL, or a MediaTek device that
still reaches BROM, can usually be reflashed in full. A device whose boot ROM
does not enumerate at all cannot be fixed from the host side.

**Why is the download 105 MB?**
Qt. The spec excludes the Qt modules this app never uses (WebEngine, Quick/QML,
Multimedia, 3D) and disables UPX on purpose — a flashing tool that gets
quarantined by antivirus is worse than a large one.

**There is a pybind11 message when I kill the app with Task Manager.**
That is a Debug-build assertion from pybind11 during forced termination, not a
flash failure. It does not appear on a normal exit, and it is not caused by this
project's error handling — an experimental build with the error system removed
still printed it. See [packaging/README.md](packaging/README.md) for the bisection
and why the check has not been switched off.

**How do I know this actually works?**
Run the test suites (`python tests/test_integration.py` and the native binaries
under `build/src/cpp/`). Then test on your own hardware: no vendor download
protocol here has been exercised against a real device, and that limitation is
stated rather than dressed up. See [Status](#status).

## Test

```bash
# Native: packet codecs, session state machines, error system, logger.
# Debug is the build the scripts make; Release if you configured one.
for t in protocol mediatek samsung spd core; do
    ./build/src/cpp/Debug/huaxin_${t}_tests.exe
done

# Python
python tests/test_integration.py       # settings, device database, errors, retries
python python/tools/test_bridge.py     # the C++ <-> Python bridge
python tests/test_threading.py         # threads, signals, error isolation
python tests/test_safe_slot.py         # slots cannot abort the process
python tests/test_device_catalog.py    # VID/PID -> vendor/mode mapping
python tests/test_adb_fastboot.py      # streaming, timeouts, cancellation
python tests/test_dialogs.py           # dialog validation and confirmations
python tests/test_qualcomm.py          # EDL + Firehose bindings, GPT, rawprogram
python tests/test_mediatek.py          # BROM + DA bindings, scatter, tab
python tests/test_samsung.py           # PIT parsing, tab wiring, honest status text
python tests/test_polish.py            # notifications, operation readout, drops, lazy tabs
python tests/test_logging.py           # file log, cable-pull handling
QT_QPA_PLATFORM=offscreen python tests/startup_check.py   # starts and stops the real app

python python/tools/screenshot_ui.py   # renders docs/screenshots/ headlessly
```

Current state:

| Suite | Checks |
|---|---|
| `huaxin_protocol_tests` — Sahara, Firehose, GPT, patch | 137 |
| `huaxin_mediatek_tests` — BROM framing, DA protocol, scatter | 186 |
| `huaxin_samsung_tests` — PIT, Odin session, tar.md5, streaming list | 107 |
| `huaxin_spd_tests` — PAC, checksums, BSL framing, header-only read | 133 |
| `huaxin_core_tests` — errors, logger, progress, timeouts, speed cap | 196 |
| `python/tools/test_bridge.py` — the native module loads, init, catalogue | report |
| `tests/test_integration.py` — settings, devices, errors, retries | 285 |
| `tests/test_ui_design.py` — tokens, contrast, stylesheet, icons, pixels, picker | 204 |
| `tests/check_tabs.py` — every tab against real PAC/PIT/tar files | 64 |
| `tests/test_polish.py` — notifications, readout, file browser, drops, lazy tabs | 155 |
| `tests/test_qualcomm.py` — EDL bindings, GPT, rawprogram, tab | 118 |
| `tests/test_mediatek.py` — BROM + DA bindings, scatter, tab | 95 |
| `tests/test_samsung.py` — PIT, Odin packages, honest status text | 40 |
| `tests/test_device_catalog.py` — VID/PID to vendor and mode | 52 |
| `tests/test_adb_fastboot.py` — streaming, timeouts, cancellation | 57 |
| `tests/test_logging.py` — file log, cable-pull handling | 32 |
| `tests/test_dialogs.py` — dialog validation and confirmations | 27 |
| `tests/test_threading.py` — worker thread, signals, error isolation | 21 |
| `tests/test_safe_slot.py` — a broken slot cannot abort the process | 11 |

**759 native checks and 1161 Python checks**, all passing.

All of them run headless (`QT_QPA_PLATFORM=offscreen`), so they work over SSH and
in CI. The adb/fastboot tests do not need a device: they run a Python script as
the "tool", which exercises the process handling — real-time streaming, timeout,
cancellation — for real.

**What the tests prove, and what they do not.** They prove the encoders and state
machines against specifications, recorded bytes and scripted devices. They do not
prove that a particular phone accepts the result: no vendor download protocol in
this project has been run against real hardware. See
[docs/contributing.md](docs/contributing.md) for what a test in this project is
expected to do.

## Architecture notes

### USB device detection

`UsbManager` owns the libusb context and enumerates every device on every bus.
Each one is matched against the catalogue in `core/device_catalog.cpp`, which
maps a VID:PID pair to a vendor, a mode name and the phase that implements its
protocol. That table is the single source of truth: it drives the UI, the logs
and (from Phase 4) which protocol handler a device is routed to. There is
deliberately no second copy in Python.

Each entry carries a `verified` flag. `true` means the pairing is documented and
reproduces on real hardware; `false` means it is reported in the wild but has
**not** been confirmed by this project. Unverified entries are shown as
`(unverified)` in the device list and asserted as unverified by
`tests/test_device_catalog.py`, so the distinction cannot quietly rot.

Two details that matter in practice:

- **Root hubs are filtered out.** libusb lists the host controller's own hubs on
  Windows as if they were devices; they are identified by having no parent
  device and are skipped unless explicitly requested.
- **String descriptors are read with our own timeout.** `libusb_`'s ASCII string
  helper hardcodes a 1 s control-transfer timeout and issues two transfers per
  string, so three strings on one unresponsive device would stall a scan for six
  seconds. We issue the transfers directly with a 250 ms bound. Reads are also
  best effort: on Windows they only succeed for devices with a WinUSB-class
  driver bound, and the device is still identified by VID/PID regardless.

### ADB and Fastboot

These drive the **official platform-tools binaries** as subprocesses rather than
reimplementing the protocols. ADB is not really a wire protocol so much as a
client/server system — the `adb` binary starts a server that owns the USB
endpoints — so reimplementing it would mean reimplementing the server and losing
interoperability with every other tool on the machine. It does mean the
application depends on platform-tools being installed; the **Check Toolchain**
button reports exactly where `adb` and `fastboot` were found, or that they are
missing.

Three things in `adb_fastboot_wrapper.py` are less obvious than they look:

- **Output is assembled by hand, not read with `for line in stdout`.** Both tools
  draw progress bars with carriage returns, and universal-newline handling turns
  each of those updates into its own line — about 100 per partition. Carriage
  returns are collapsed the way a terminal would, so only the settled line
  reaches the log.
- **Commands are validated before they become argv.** A partition name is
  passed as an argument, so a name like `--slot` would be read by fastboot as an
  option rather than a partition. Names and serials must match a strict pattern
  and cannot start with `-`. Nothing goes through a shell.
- **Every command has a timeout, and cancellation kills the child.** A wedged
  `adb` otherwise hangs the worker thread forever.

Destructive actions (flash, erase, `fastboot -w`) show the exact command and
default to *No*; the flash dialog keeps its button disabled until the input is
valid, using the same partition-name rule the command layer enforces.

### Qualcomm EDL (Sahara + Firehose)

The download-mode protocols are implemented in C++ against libusb, because there
is no system tool to shell out to the way there is for ADB.

**Provenance.** These are proprietary protocols, and getting a packet layout
wrong is worse than not implementing it: a wrong field offset produces plausible
nonsense. Every constant and field layout in `protocols/qualcomm/` was therefore
**transcribed from Qualcomm's own upstream Linux EDL tool `linux-msm/qdl`
(BSD-3-Clause)** — `src/sahara.c`, `src/firehose.c` — rather than written from
memory. Anything that could not be verified from a primary source is marked with
a `TODO` instead of a guess.

The device drives the conversation: it announces itself with `SAHARA_HELLO_REQ`,
and the host answers. There are two distinct flows:

- **Identity** — the host asks for `SAHARA_MODE_COMMAND` in the HELLO reply, then
  issues `EXECUTE`/`EXECUTE_DATA` pairs to read the serial number, the MSM/OEM/
  model ids and the OEM public key hash. The device is then reset back to its
  initial state.
- **Upload** — the host echoes the device's mode, the device requests image
  chunks with `READ_DATA`, and the host answers with raw bytes plus
  `END_OF_IMAGE` per chunk. Once the programmer is running, the device speaks
  Firehose instead.

Firehose is a short XML request/response protocol on the same bulk endpoint.
Responses are unframed, so the host reads until it sees `</data>`, and a
`rawmode="true"` reply means a raw payload follows.

**The full Firehose session.** After the programmer is running, the Qualcomm tab
drives it through:

| Step | Command | What it does |
| --- | --- | --- |
| Configure Device | `<configure>` | Negotiates payload size and, optionally, the storage type. Required first. |
| Get Storage Info | `<getstorageinfo>` | Total blocks and block size for a LUN. |
| Read GPT | `<read>` ×2 | LBA 1, then the entry array wherever the header says it lives. |
| Flash Firmware | `<program>` per partition | Replays a `rawprogram0.xml`, then its `patch0.xml`. |
| Read Partition | `<read>` | Dumps a sector range to a file. |
| Erase Partition | `<erase>` | Erases a sector range. |

The partition table is shown in a sortable table in the tab, and a selected row
supplies the sector range for the read and erase actions, so the numbers come
from the device's own GPT rather than from typing.

The **session is persistent**, and that is not a detail. Uploading a programmer
takes seconds and re-initialises the storage engine, so configure → read GPT →
flash has to happen inside one session; rebuilding it per click would make the
sequence impossible. "Close Firehose Session" ends it without resetting the
device.

**Error handling.** Each command has a timeout sized to what it does (30 s for a
plain command, 10 s for a write's setup but 120 s for its completion), and a NAK
is retried 3 times. A protocol error or a disconnect is *not* retried — repeating
a command into a desynchronised stream makes things worse — and both are raised
as `ProtocolError` rather than guessed at. A device that answers
`getstorageinfo` without a geometry gets a warning and a documented 512-byte
fallback, never an invented one.

**Sample commands and provenance.** Every command this tool sends is documented
in [`docs/firehose-commands.md`](docs/firehose-commands.md), including the
attribute sets, the `rawmode` payload handoff, the `rawprogram`/`patch` file
formats, the ordering the device requires, and the four things that are **not**
verified against hardware (the `MemoryName` spelling, whether programmers want the
program payload padded to a boundary, and how `<patch>` values are encoded).

Two things that catch people out, both learned from testing rather than reading:

- `EXECUTE_RESP` is **16** bytes, not the 12 of the host's `EXECUTE` packet — qdl
  has one length constant for both, but the response carries an extra field, and
  using 12 makes every identity read come back empty.
- `EXECUTE_DATA` (`0x0f`) is a **different command** from `EXECUTE` (`0x0d`).
  Sending `EXECUTE` twice leaves the device waiting and the payload read times
  out.

**Windows driver.** libusb cannot open the EDL interface through Qualcomm's own
QDLoader driver — that driver does not expose the device to WinUSB. Bind WinUSB
or libusbK to interface 0 with Zadig first. `open()` says exactly this on
failure rather than surfacing a bare libusb error code.

**Not verified against hardware.** No Qualcomm device was available, so the whole
layer is verified against a scripted transport and recorded packet bytes, not a
phone. Treat the first run against real hardware as a test.

### MediaTek BROM

Implemented in C++ against libusb, like the Qualcomm path. The protocol notes —
endianness traps, the complemented handshake echo, the target-config bit layout,
and a licensing point worth reviewing — are in
[docs/mediatek-notes.md](docs/mediatek-notes.md).

In short: it is **big-endian** (the opposite of Sahara/Firehose), commands are
acknowledged by **echo** rather than a status field, and the handshake echo is
the **bitwise complement** of what was sent. The bootrom also exposes no storage
commands at all, so nothing can be read or written until a download agent has
been uploaded into SRAM and started.

**Two protocols, one cable.** The bootrom can handshake, identify itself, receive
an agent and jump to it. That is the entire list. Flashing is the *agent's* own
protocol, and it is a different conversation on the same wire — little-endian
with a `0xFEEEEEEF` magic on every frame, where the bootrom layer is big-endian
with an echo and no magic. Assuming the two are consistent is the most expensive
mistake available here, and it does not produce an error, only garbage addresses.

Once the agent is running, the MediaTek tab drives it through:

| Step | Command | What it does |
| --- | --- | --- |
| Read Flash Info | `GET_*` queries | Agent version, which stage it took over from, storage geometry, negotiated packet sizes. |
| Load Scatter File | — | Parses the package and shows the plan. Nothing is touched on the device. |
| Flash Firmware | `WRITE_DATA` per partition | Replays the scatter file in file order. |
| Readback Partition | `READ_DATA` | Dumps one partition to a file. |
| Format / Erase Flash | `FORMAT` | Erases the partitions the package lists with no image. |

Two checksums are in play and they are **different algorithms**: the bootrom
XORs 16-bit words to accept an agent, and the agent sums bytes for its data
transfers. A build that used one where the other belongs is rejected with an
unhelpful generic error, so the tests assert the two disagree on the same input.

**Scatter files are read in both shapes** — the modern YAML form and the older
`key = value` form. Fields with a confirmed meaning are parsed; everything else
(`type`, `boundary_check`, `reserve`, vendor extras) is preserved and shown
rather than interpreted, because guessing at a field's meaning is how a partition
gets written to the wrong place. See
[docs/mtk-scatter-format.md](docs/mtk-scatter-format.md).

**A failed write stops the run**, by default. The report says which partitions
were written and which were not, because a device that is part new and part old
is harder to recover than one that failed early and said where.

**Not verified against hardware.** No MediaTek device was available; the
handshake, both framings, both checksums, the region parameter block, the chunked
read and write paths, the erase flow control, and the scatter parser are verified
against a scripted device in `tests/cpp/test_mediatek.cpp`. The NAND and NOR
geometry layouts are marked `TODO` in the source: mtkclient parses those two
positionally without naming the layout, so the offsets here are inferred and
should be checked against a recorded reply before being trusted.

### Samsung Odin and PIT

Transcribed from [Heimdall](https://github.com/Benjamin-Dobell/Heimdall) (**MIT**),
`libpit/source/libpit.h` and the `heimdall/source/*Packet.h` files. The PIT is a
little-endian table: a 28-byte header carrying magic `0x12349876` and an entry
count, then 132-byte entries with nine 32-bit fields and three 32-byte name
buffers.

Two things are worth knowing:

- **The entry count comes off the wire and drives an allocation**, so it is
  checked against what the buffer can actually hold before anything is indexed or
  reserved. A device claiming `0xFFFFFFFF` entries is refused, not believed.
- **The second word of an Odin response is not always a status.** For a session
  response it is the device's preferred packet size, and for a PIT response it is
  the length of the table that follows. Reading it as a status makes every
  session look refused — a bug this project shipped and then caught with a test.

A PIT *file* can be parsed today with no device attached, which is useful: the
table can be inspected before anything is written to it. Reading one off a device
needs the Samsung USB transport, which is Phase 7b.

### Unisoc / SPD — the blocker, and how it was resolved

This phase was blocked for a while, and the reason was recorded rather than
guessed around: the `.pac` container layout and the Research Download framing
could not be verified, and a proprietary byte structure is never invented here.
A guess produces code that compiles, looks right, and leaves a device
half-flashed.

**The blocker was a bad search, not a missing source.** Looking again with
different terms found four:

- **The vendor's own header** — `unisoc-dloader/include/BinPack.h` carries
  Spreadtrum's definition of the packet format and file table, including
  `PAC_MAGIC` and the annotations that explain the V1→V2 field changes. More
  authoritative than any tool.
- **Three independent clients** that agree with it byte for byte:
  `sprdflash-rs` (MIT), `sprdclient` (GPL-3.0) with `unpac`, and `unpac_py`.
- **A complete command enum** in `uwpflash` (Apache-2.0) `command.h`, with the
  meaning of every BSL command written next to it.

The `protocols/spd/` layer is written and tested: the PAC parser for both
layouts, the four checksums this protocol family uses, the HDLC framing and the
whole command set, FDL1/FDL2 extraction, and a USB transport. `huaxin_spd_tests`
runs 122 checks against a scripted device.

Two traps in this protocol are worth knowing about even if you never read the
code:

- **There are two checksums and the host does not choose.** Classic Spreadtrum
  boot ROMs use CRC-16 (poly 0x1021, **init 0**); the RDA8910/UIS8910 family uses
  a ones-complement word sum. A frame with the wrong one gets **no reply at all**,
  which looks exactly like a dead device. The initial value matters too: init 0
  makes it the variant catalogued as CRC-16/XMODEM, not CCITT-FALSE. So the
  checksum is *detected* from the device's first reply, which is free.
- **The PAC has two layouts**, and the vendor header says exactly how they differ:
  V2 took two words out of the version string for a 64-bit file size, and four out
  of each entry's version field for 64-bit sizes and offsets. Both headers are
  still 2124 bytes and every field a host needs is at the same offset in both,
  which is why one parser reads either.

**What is not done:** the Python wrapper and the tab's buttons. The protocol is
not exposed through pybind11 yet, so the SPD tab says its actions are unavailable
— it says *that*, and not "blocked", because blocked is no longer true. See
[docs/spd-status.md](docs/spd-status.md) for the full list of what remains,
including four things about this protocol that no source confirmed and this
build therefore does not do.

### USB error handling

Every transfer in the codebase has a bounded timeout — bulk transfers run
against a deadline computed from the caller's timeout, and control transfers
pass one explicitly. A device that stops answering therefore raises; it cannot
hang the worker thread.

The failure that matters most in practice is simpler than a timeout: someone
pulls the cable. libusb reports `LIBUSB_ERROR_NO_DEVICE`, which used to surface
as `USB read failed: LIBUSB_ERROR_NO_DEVICE (-4)` — true, and useless to the
person holding the device. It is now `UsbDisconnectedError`, which says what
happened and what to do, and the transport **closes itself** so the next call
fails fast with "not open" instead of hammering a handle whose device is gone.

`UsbDisconnectedError` derives from `ProtocolError` in C++ **and in Python** —
registered with `ProtocolError` as its Python base — so existing handlers keep
working while a caller that wants to react specifically to a cable pull can
catch the narrower type.

### Logging

`flash_log.txt` is written next to the executable and appended to, never
truncated: a second run after a failure must not erase the evidence from the
first. Each line carries a millisecond timestamp and a thread tag, because a
flash emits bursts of lines within the same second from both the worker thread
and the UI thread.

The C++ side opens the log and records hardware events; the UI's own lines are
mirrored into the same file, so it is one chronological record rather than the
C++ half of it. Every line is flushed immediately — a crashed flash must not
lose the last thing that happened, which is the entire point of the file.

Logging degrades rather than blocking: if the path is not writable, the tool
warns and runs without it.

### Packaging

See [packaging/README.md](packaging/README.md). In short:

```bash
./scripts/build.ps1                                  # native backend first
python -m pip install pyinstaller
pyinstaller packaging/huaxin.spec --noconfirm
```

Produces `dist/HuaxinTool/`, about 95 MB, verified to load the real C++ backend
and libusb from a directory containing no Python and no source tree.

Two things deliberately do **not** ship, and cannot:

- **USB drivers.** WinUSB is a Windows in-box driver and libusbK is a signed
  third-party one; what a device needs is for one of them to be *bound* to its
  interface, which is an administrator action per device model (Zadig or an INF),
  not a file in a folder. The tool reports exactly this when `libusb_open` fails.
- **adb and fastboot**, unless `third_party/platform-tools/` exists at build
  time. They are Google's binaries under their own licence, and redistributing
  them is your decision rather than a build script's.

### Threading

All backend work happens on **one** long-lived `QThread` draining a FIFO job
queue, rather than a thread per operation. Flashing is strictly serial by nature
— you cannot interleave two conversations with the same device — so a single
ordered queue gives that guarantee structurally, keeps the stateful C++ bridge
single-threaded, and makes the log order match the order the operator clicked.

```
UI thread      --submit(Job)-->  queue  -->  worker thread runs job.fn(ctx)
worker thread  --pyqtSignal-->   Qt queued connection  -->  UI thread slots
```

Three properties are enforced by construction, not by convention:

- **`Backend` is thread-confined.** It records the thread that opened it and
  raises if any other thread calls in, so "the UI must never call the native
  bridge" fails loudly instead of racing.
- **Results cross the boundary as plain data.** The worker converts the native
  `DeviceInfo` objects into frozen Python `Device` snapshots, so the UI never
  holds a reference to memory the worker could invalidate.
- **A failing job is data, not a crash.** Exceptions are caught at the job
  boundary, logged with their traceback, and the thread moves on to the next job.

### UI robustness

PyQt6 **aborts the process** when a Python exception escapes a slot. In a tool
that may be midway through writing a partition, that is unacceptable, so every
signal-connected slot is wrapped with `@safe_slot`, which logs the exception and
keeps the process alive.

Cancellation is cooperative and works both on a running job (when it polls
`ctx.check_cancelled()`) and on one still queued — the second case matters
because a Cancel click often lands in the gap before the worker picks the job up.

## Development conventions

- **Threading:** every call that touches hardware blocks its caller. It runs on
  the worker thread — never on the Qt UI thread.
- **Errors:** device disconnects and timeouts are caught, logged and surfaced as
  a status; they must never propagate out of a worker and kill the process.
- **Protocols:** only documented packet structures are implemented. Anything
  unverified carries an explicit `TODO` rather than a guess.

## Changelog

### 0.8.3 — Unisoc reads a device

**The Research Download link works.** `protocols/spd/unisoc_bsl.{h,cpp}` is the
SPD counterpart of `MediaTekBrom`: one object that owns the transport and the BSL
session, with log, progress and cancel callbacks. It adds no protocol logic of
its own — every call it makes was already implemented and covered by the 133
native checks — which is the point of separating it from the protocol layer.

Bound as `huaxin_core.UnisocBsl` with `py::gil_scoped_release` around every call
that blocks on USB, because without that release opening a device would freeze
the interface for as long as the device takes to answer. Wrapped in
`core/unisoc.py`, which is where the session, the error messages and the job
functions live.

**The tab has buttons that do something.** Handshake, Read Device Info, Read Back
Entry, Reset Device and Power Off replace the four that used to say the transport
was missing. Read-back takes its address and length from the *selected package
entry*, which is what makes it a button rather than a form: the package already
says where the data should be, and comparing what comes back against it is the
check worth doing.

**What is still refused, and why.** `Flash PAC Firmware` is not wired, and the
button says so. The parser, the loaders and the payload primitive are all present
and tested; what is missing is the FDL1-to-FDL2 handover, which no source this
project holds settles. A guessed sequence would be run against somebody's phone.
Erase and write exist in the session, bound and usable by a caller that knows the
range, but neither has a button — an erase control that guessed an address range
would be the worst control in the tool.

**Four bugs of the user's own, found by checking rather than assuming.**
`apply_gsm_layout()` and the `showEvent` override called `hasattr(self,
'log_dock')` against attributes actually named `_log_dock`, so both were dead
code that silently did nothing. Locking the docks with `NoDockWidgetFeatures`
also *disables* Qt's `toggleViewAction`, leaving two permanently greyed-out
entries in the View menu — so the docks are locked and those two menu items are
gone rather than dead. And `main.py` had grown a fallback that constructed
`MainWindow()` with no service, which would have raised TypeError on a path
nothing exercises; the high-DPI policy it also set was worth keeping and moved to
`huaxin/app.py`, where every entry point gets it and where the ordering
requirement (before the QApplication exists) can be stated.

### 0.8.2 — a missing adb now leads somewhere

**The failure is still a failure, and it says so.** `get_fastboot_devices` raises
`ToolNotFoundError` with the tool it wanted, everywhere it looked and what to do
about it; `Check Toolchain` still treats a missing tool as a *finding* rather
than an error, because that check is a diagnostic and painting it red would be
wrong.

**What changed is where it points.** The message named `HUAXIN_ADB` /
`HUAXIN_FASTBOOT`, which was the only route when it was written and is no longer
the discoverable one — the paths now live in **Settings → Tools & folders**. The
hint names the page first and the environment variables second, for a machine
configured by script.

**And it is now one click from the fix.** A job that fails because a tool is
missing raises a notification that stays for twenty seconds and carries its own
button:

```
⌗ adb or fastboot is not installed
  fastboot devices needs it. Set the path in Settings → Tools & folders,
  or install Android platform-tools.              [ Open Settings ]
```

Pressing it opens the settings dialog *on the Tools & folders page*, so the box
that takes the path is already on screen. `show_settings_dialog(..., page=...)`
takes a category by name rather than by index, because a caller that has to know
its position in a list breaks the next time a category is added in the middle.
This is the first use of the action button the toast system was built with.

The log keeps the whole story: the traceback, every location searched, and one
line naming where the fix is.

### 0.8.1 — three bugs the screenshots were hiding

**The theme picker never worked.** Its options were built as `(name, label)`
where `StyledComboBox` takes `(label, name)`, so it displayed the raw key,
never selected the active theme, and handed the *label* back to
`set_theme("Graphite")` — which rejected it with a `KeyError` in the log and
changed nothing. Two fixes, and the second is the one that matters: the pairs
are the right way round, and the check that was supposed to cover it no longer
asserts the wrong contract —

```python
# before: passed while the widget could not change a theme
[name for name, _ in controller.themes()] == tokens.theme_names()
# now: drives the widget, entry by entry, the way a click does
picker.setCurrentIndex(row)  # and asserts the theme actually changed
```

`theme_controller.themes()` now returns `(label, name)`, and the picker exposes
`syncToTheme()` so the settings dialog can move it without telling the
controller to change anything. The `hasattr` guard that used to hide the
failure is gone.

**The window's contents did not follow a resize offscreen.** `QMainWindow`
resizes its central widget in response to a platform resize event, which never
arrives on Qt's offscreen platform — the window became 1440 wide while the
interface inside it stayed at its startup width, and everything past the old
right edge of the title bar (the picker, the window buttons) was not drawn.
`FramelessWindow.resizeEvent` now resizes it explicitly.

**The screenshots had been wrong for several phases**, in three ways at once:
they grabbed the top-level window, which on the offscreen platform loses most of
the custom chrome (the title bar's gradient paints; the picker, the buttons and
the subtitle in it do not); they inherited the window geometry the tool itself
had saved on the previous run, so a file named `size-1920x1080` held a 1230x827
window; and `--all` rendered the size and feature images *after* the theme pass,
so they came out in Daylight with nothing saying so. `screenshot_ui.capture()`
now renders the central widget at the size asked for, each pass states its theme,
and the saved layout is cleared before every window. All 31 images are the size
and the theme their names claim.

The lesson is the one this project keeps relearning: a check that asserts the
wrong thing is worse than no check, because it is the reason nobody looked.

### 0.8.0 — the advanced features

**Notifications instead of dialogs, where a dialog was never right.**
`ui/toasts.py` stacks up to four cards in the corner, slides them in, pauses their
countdown while the pointer is over them, and counts a repeated message
(`×3`) rather than stacking three identical cards. Everything a toast says is
also written to the log — a message that vanishes after four seconds is not a
record of anything. A device arriving or leaving is announced; the *first* scan
is not, because everything in it is news.

**A live operation readout** (`ui/opstatus.py`) in the status bar: what is
running, how fast, how long it has been going, how much is left, and a **stall
warning** when the percentage has not moved for 45 seconds — which is the
question a progress bar never answers. An ETA the device reported is shown
plainly; the one the tool derives from the rate of progress is marked `~`.
`BackendService.job_progress_detail` and `JobContext.detail()` carry the native
`FlashProgress` — speed, byte counts, ETA — through to the interface, normalised
in `huaxin/core/progress.py`.

**A file browser that says what is in the file** (`ui/filedialog.py`), replacing
every `QFileDialog` call in the application. It has places, recent files and
type filters, and a preview pane that reads the package's own header: a PAC shows
its product name, entry count and payload; a `.tar.md5` shows its members. The
preview states that the payload CRC was *not* checked, because a header-only read
cannot check it.

**Settings as a rail of six categories** — General, Appearance, Timeouts,
Logging, Tools & folders, Advanced — with a heading that says what each category
is for. Two settings were added and both do something: animation on/off, honoured
by every helper in `ui/animations.py`, and explicit `adb`/`fastboot` paths,
honoured by `configure_tools()`. A path that does not exist falls back to the
same discovery the tool would have done anyway, and the page shows what was
actually found.

**An About dialog** that states the whole build — interface, native backend,
libusb, Qt, Python — with a button to copy it as text for a report. It also
states, in the tool's own words, that Unisoc and Samsung are **read-only in this
build**: packages can be inspected, flashing is not implemented. No updater is
offered, because there is no update channel to check.

**Drag and drop.** Drop a package on the window and the overlay says which tab
will open it before you let go. The panel's own loader does the work, so a
dropped file and a chosen one take exactly the same path.

**Lazy tabs.** The five vendor panels are built on first visit rather than at
startup. Everything that can reach a tab — the tab bar, Alt+1…Alt+5, a dropped
file — goes through `_ensure_panel`, so a panel built by one route has the same
state as one built by another.

**Device arrival and departure.** A new device's row is tinted for 900 ms and a
ring plays once beside the filter; a device that disappears raises a notification
saying so. Deliberately one-shot: an indicator that pulses forever stops being a
signal within ten seconds.

**The crash this found.** A `ToastManager` installed as an event filter on a
widget that outlives it is a crash: PyQt hands the still-living C++ object a
*fresh Python wrapper without running `__init__`*, every attribute is missing,
and an exception raised from an event filter is not a Python error — PyQt routes
it to `qFatal` and the process aborts with no traceback. `eventFilter` now reads
its state through `__dict__.get` and does nothing when it is not there. The
symptom was an intermittent abort in the test suite; in the field it would have
been a crash on exit, in a tool that may be halfway through writing a partition.

### 0.7.0 — the vendor tabs

**Every tab rebuilt on one kit.** `ui/tabkit.py` holds the header, status badge,
device box, two-column action grid and help note, so the five tabs cannot drift
apart. The grid fills row by row from one ordered list, which makes the order of
the list the layout — and `left_column()`/`right_column()` let a test assert the
arrangement the specification asks for rather than a reader working it out.

**The SPD and Samsung tabs now read real files.** The PAC parser and the tar
reader are exposed, so those tabs open a package and show what is in it: the
entry table and loaders for Unisoc, the partitions and package members with their
partition mapping for Samsung. That is the check worth making before anything
touches hardware. Writing to either vendor's device is still not implemented, and
the buttons say so.

**Two streaming readers, because a package does not fit in memory.** `load_pac`
reads a whole file, which for a two-gigabyte package means two gigabytes of RAM
to display a table about its first few kilobytes; `read_pac_header` reads the
header and the entry table and stops. `list_tar_file` seeks past each member
rather than reading it, and stops at the appended `.md5` digest — which is raw
binary, and parsing it as a tar header reports a good package as corrupt. Both
are covered by new native tests (SPD +11, Samsung +10).

**Context menus on every table**, built per row so the entries can depend on what
was clicked, and opened through Qt's own policy so the keyboard's context key
works too.

**Scatter actions are colour-coded**: write in the accent, erase in the warning
colour, skip dimmed to the point of being ignorable.

Running this for real found two faults a source-level check could not: a
`QLabel` holds either a pixmap or text, so the status badge and the help note
were showing an icon and **no message at all**; and the tab screenshots were
being rendered without the theme applied, which showed Qt's default grey.

### 0.6.0 — the interface

**A design system.** Every colour, radius, spacing, font size and duration is a
token in `ui/tokens.py`; the stylesheet is a real `.qss` file of `@token`
placeholders that the loader fills in. Three themes — Midnight, Graphite and
Daylight — switchable live from the title bar and remembered between runs.

**Contrast is measured, not judged.** Every foreground/background pair the
interface uses is held to WCAG AA, and the palettes were tuned until they passed:
the accent family became a saturated blue with white text because a light blue
with dark text could not darken for its pressed state without dropping below
4.5:1. The worst pair across the three themes is 4.70:1.

**A component library.** Buttons in five roles, icon buttons, status dots, chips,
stat tiles, cards, callouts, section headers, empty states, styled tables, a
segmented progress bar for multi-partition flashes, and custom-styled inputs,
combo boxes and checkboxes with a real SVG chevron and tick rather than the
platform's.

**Forty vector icons**, drawn as SVG source and recoloured at render time, so a
theme change reaches them. Rendered at 2x for HiDPI displays.

**A custom title bar.** The window is frameless with chrome drawn in Python:
dragging with a movement threshold, double-click to maximise, eight resize
handles, Windows Snap preserved, and a theme picker in the bar.

**Animations** for hover, tab changes, panel collapse, log entries and modals —
all short, all interruptible, and all skipped when the system's reduce-motion
preference is set.

**Help → Component gallery** shows every component on one page, in the active
theme.

**Verified by pixels.** A Qt stylesheet fails silently, so the test suite renders
real windows and reads the colours back: the background must be the theme's `bg`,
the primary button must be painted in the accent family with its gradient running
the right way, and the three themes must paint three different backgrounds. 197
checks.

Running that build for real found two faults that no source-level check would
have: the packaged application died at startup because the stylesheet is a data
file and was not being bundled, and the hover animation reused an object Qt had
already deleted, which threw from inside an event filter and — as a side effect —
made the startup device scan repeat several times a second. Both are fixed, and
the spec now refuses to build without the stylesheet.

### 0.5.0 — final integration

**Error handling.** A unified error system: seven classifications
(`USB_ERROR`, `PROTOCOL_ERROR`, `FILE_ERROR`, `FLASH_ERROR`, `AUTH_ERROR`,
`INTERNAL_ERROR`, `CANCELLED`), each with its own advice, retry decision and
risk assessment. `FlashException` carries the kind, the vendor, a timestamp and
a context stack, and reaches Python as a typed exception deriving from
`RuntimeError` with those fields as attributes. Automatic retry with exponential
backoff — three attempts by default, 250 ms doubling to a 5 s ceiling, with a
total budget — and only for failures that could plausibly succeed. See
[docs/errors.md](docs/errors.md).

**Logging.** Five levels (the new `CRITICAL` included), rotation at 10 MB into a
single `.1` backup, colour-coded console output, and separate switches for file
and console. Rotation is measured against a running byte count, so the file never
overshoots by more than the line that crossed it.

**Progress.** One `FlashProgress` type for every vendor and a tracker that emits
on every 1% or every second plus the first and last events, with speed and ETA
suppressed until the average means something.

**Settings.** A JSON settings file, a settings dialog (`Ctrl+,`), and a
deployment template an administrator can ship. Every value is validated on the
way in: a negative timeout is clamped rather than passed to a USB transfer, and a
non-boolean boolean falls back rather than being coerced - `bool("no")` is `True`,
which is exactly the sort of thing a hand-edited file contains.

Every setting in the dialog changes something, and a test enforces that. The
first version did not: it offered nineteen switches and honoured six. The
timeouts, the retry policy and the speed cap are pushed into the native layer and
apply to every vendor at once; see
[docs/architecture.md](docs/architecture.md#settings-and-the-rule-about-them).

**Device database.** Per-family profiles with aliases, default flash settings,
and known issues — each written as symptom, cause, workaround, with the source
stated. A profile marked unverified says so.

**Packaging.** A PyInstaller spec that refuses to build without the native
module, ships the driver guide, the protocol documentation, the licence and the
settings template alongside the executable, and excludes the Qt modules the app
never uses. The unpacked build comes to about 105 MB.

**Documentation.** [docs/architecture.md](docs/architecture.md) on how the layers
fit together and why each boundary is where it is,
[docs/contributing.md](docs/contributing.md) on the rules and how to test a
protocol change, [docs/errors.md](docs/errors.md), and
[drivers/README.md](drivers/README.md).

**Tests.** 738 native checks across five suites, and 927 Python checks across the
twelve Python suites. The integration suite grew a guard that fails if any
setting is offered without an effect - which is how the six decorative switches
from the first cut were found. The suites found real bugs during this phase, including a
`build_pit` that wrote the PIT entry count at the wrong offset — a rebuilt
partition table parsed as *empty*, so a repartition would have silently wiped it.

### 0.4.0 — vendor protocols

Qualcomm EDL (Sahara + full Firehose: GPT, partition read/write/erase,
rawprogram + patch replay), MediaTek BROM and the DA protocol (flash geometry,
scatter replay, readback, erase), Unisoc PAC parsing and the BSL protocol,
Samsung PIT parsing and Odin session framing, and the `.tar.md5` reader.

### 0.3.0 — real device discovery

libusb enumeration, the VID/PID catalogue with a verification flag per entry, and
per-vendor transports.

### 0.2.0 — the UI

PyQt6 window, one tab per vendor, the device dock, the log console, and the
worker-thread architecture that keeps the UI responsive during a flash.

### 0.1.0 — the bridge

Project structure, the CMake build, and the pybind11 bridge between Python and
C++.
