# USB drivers

A device that shows a warning triangle in Device Manager will never be flashed.
This folder holds the drivers and the tool that binds them.

```
drivers/
  README.md              this file
  install_drivers.bat    interactive installer, one step at a time
  zadig.exe              generic WinUSB binder (download separately - see below)
  qualcomm/              QDLoader / HS-USB 9008
  mediatek/              MediaTek USB VCOM
  unisoc/                Spreadtrum / Unisoc
  samsung/               Samsung Android USB driver
```

**Nothing here is redistributed by this project.** The vendor drivers are
proprietary and belong to their owners; `zadig.exe` is by Pete Batard and is
covered by its own licence. The folders below are where to put the installers
you obtain from the vendor. `install_drivers.bat` will tell you what is missing
rather than failing silently.

---

## Which driver each mode needs

The mode matters, not the phone. A device that works over ADB may still need a
different binding the moment it enters a flash mode, because it enumerates with
a different vendor and product ID.

| Mode | Vendor ID | Driver | Folder |
|---|---|---|---|
| Qualcomm EDL (9008) | `05C6` | QDLoader / WinUSB | `qualcomm/` |
| Qualcomm diagnostic | `05C6` | QDLoader (same package) | `qualcomm/` |
| MediaTek BROM | `0E8D` | MediaTek USB VCOM / WinUSB | `mediatek/` |
| MediaTek Preloader | `0E8D` | MediaTek USB VCOM | `mediatek/` |
| Unisoc Research Download | `1782` | Spreadtrum USB driver | `unisoc/` |
| Samsung Download mode | `04E8` | Samsung Android USB driver | `samsung/` |
| ADB / Fastboot | varies | Google USB driver, or WinUSB via Zadig | — |

---

## Install

### 1. Check what is bound right now

Open Device Manager (`Win+X`, then `M`). Put the device into its flash mode and
look for one of these:

* **A working device** — it appears under `Ports (COM & LPT)` as something like
  `Qualcomm HS-USB QDLoader 9008 (COM7)`, with no warning icon. Nothing to do.
* **A device with a warning triangle** — under `Other devices`, often as
  `QHSUSB_DLOAD`, `Android`, or `MTK USB Port`. This is the case the installer
  fixes.
* **Nothing at all** — the device is not in the mode you think it is, or the
  cable or port has failed. A driver cannot help here; see *Nothing appears* in
  the troubleshooting section below.

### 2. Run the installer

```
drivers\install_drivers.bat
```

Right-click and **Run as administrator** — binding a driver needs it. The script
walks through each vendor in turn and asks before doing anything. It does not
install all four drivers "just in case": binding a driver to a device that
already has a working one is how a working setup gets broken.

### 3. If the vendor installer does not work

Some of these drivers ship as an `.exe` that refuses to install on a modern
Windows because it is unsigned, or because it only recognises devices it has
seen before. The fallback is Zadig, which binds the generic WinUSB driver to the
interface you select:

1. Put the device into its flash mode and leave it connected.
2. Run `zadig.exe` as administrator.
3. `Options` → **List All Devices**. Without this, Zadig hides exactly the
   devices you need to see.
4. Select the device in the dropdown. Check the USB ID matches the table above
   **before** continuing.
5. Choose **WinUSB** as the target driver and press **Replace Driver**.

---

## Warnings

**Binding a driver replaces whatever was there.** That is the point, and it is
also how a mistake becomes a problem: if you bind WinUSB to a USB keyboard or
mouse, it stops working until the driver is put back.

* Confirm the USB ID before pressing Replace Driver. A name in the dropdown is
  not enough — several unrelated devices show up as `USB Serial Device`.
* Bind the flash-mode interface only. Leave every other interface on the device
  alone.
* Do this with the device in the mode you are flashing in. A phone that
  re-enumerates after the driver is bound may need the same treatment again for
  its other mode.

**To undo a binding:** Device Manager → find the device → right-click →
`Uninstall device`, tick *Delete the driver software for this device*, then
unplug and replug. Windows reinstalls its own driver on the next enumeration.

**A driver binding does not make a locked device flashable.** If the bootloader
rejects an image, that is the device's security policy, not a driver problem,
and the tool reports it as an authentication failure rather than retrying.

---

## Windows driver signing

Windows 10 and 11 in their default configuration refuse drivers whose signature
they cannot verify. If a vendor installer fails with *"the third-party INF does
not contain digital signature information"*, the options are:

* install the vendor's own signed release rather than an extracted INF,
* bind WinUSB with Zadig, which is signed, or
* disable driver signature enforcement for one reboot — this weakens the machine
  and should be undone immediately afterwards. It is not recommended.

---

## Nothing appears at all

In rough order of likelihood:

1. **The device is not in flash mode.** Every vendor has its own entry
   combination, and most require the device to be powered off first. A device
   that boots normally does not expose a flash interface.
2. **The cable is charge-only.** This is more common than it sounds. Use the
   cable the device shipped with.
3. **The port cannot hold the link.** Use a rear USB port directly on the
   motherboard. Hubs, front-panel headers and extension cables fail in exactly
   this way: the device enumerates and then disappears during a transfer.
4. **Another program holds the interface.** Samsung's own software, a phone
   manager, an emulator's ADB server and any other flashing tool all seize these
   interfaces. Close everything else and try again.
5. **The device is damaged.** If nothing appears in any mode on any machine, the
   USB or the boot ROM is the problem, and no host-side change will fix it.

---

## Linux and macOS

There are no drivers to install, but access permissions still apply. On Linux,
add a udev rule so the tool can open the device without root:

```
# /etc/udev/rules.d/51-huaxin.rules
SUBSYSTEM=="usb", ATTR{idVendor}=="05c6", MODE="0666", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="0e8d", MODE="0666", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="1782", MODE="0666", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="04e8", MODE="0666", GROUP="plugdev"
```

Reload with `sudo udevadm control --reload-rules && sudo udevadm trigger`, then
replug the device. On macOS, nothing is needed for libusb access beyond the
usual security prompt.
