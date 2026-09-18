"""One tab per vendor.

The ADB/Fastboot tab is implemented: its buttons run the official adb and
fastboot binaries on the worker thread. The vendor download modes (Qualcomm,
MediaTek, Unisoc, Samsung) are still stubs - their protocols land in Phases 5-8,
and a button that pretended otherwise would be lying to the operator. Those
actions log what they are and when they land.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from huaxin.core import parsers
from huaxin.core.adb_fastboot_wrapper import (
    erase_partition,
    flash_partition,
    get_adb_devices,
    get_device_properties,
    get_fastboot_devices,
    reboot_device,
    reboot_to_bootloader,
    verify_toolchain,
    wipe_data,
)
from huaxin.core.backend import BackendService, Device
from huaxin.core.mediatek import (
    DEFAULT_DA_ADDRESS,
    MtkNotReadyError,
)
from huaxin.core.mediatek import (
    flash_firmware as mtk_flash_firmware,
)
from huaxin.core.mediatek import (
    format_flash as mtk_format_flash,
)
from huaxin.core.mediatek import (
    load_download_agent as mtk_load_download_agent,
)
from huaxin.core.mediatek import (
    read_chip_info as mtk_read_chip_info,
)
from huaxin.core.mediatek import (
    read_flash_info as mtk_read_flash_info,
)
from huaxin.core.mediatek import (
    read_target_config as mtk_read_target_config,
)
from huaxin.core.mediatek import (
    readback_partition as mtk_readback_partition,
)
from huaxin.core.mediatek import (
    release_session as mtk_release_session,
)
from huaxin.core.mediatek import (
    session_open as mtk_session_open,
)
from huaxin.core.mediatek import (
    shutdown_device as mtk_shutdown_device,
)
from huaxin.core.mediatek import (
    verify_toolchain as mtk_verify_toolchain,
)
from huaxin.core.samsung import parse_pit_file as samsung_parse_pit
from huaxin.core.samsung import verify_toolchain as samsung_verify_toolchain
from huaxin.core.unisoc import PENDING_REASON
from huaxin.core.unisoc import bsl_connect as spd_connect
from huaxin.core.unisoc import bsl_power_off as spd_power_off
from huaxin.core.unisoc import bsl_read_device_info as spd_read_device_info
from huaxin.core.unisoc import bsl_read_region as spd_read_back
from huaxin.core.unisoc import bsl_reset as spd_reset
from huaxin.core.unisoc import verify_toolchain as spd_verify_toolchain
from huaxin.core.qualcomm import (
    apply_patch as edl_apply_patch,
)
from huaxin.core.qualcomm import (
    configure as edl_configure,
)
from huaxin.core.qualcomm import (
    describe_rawprogram,
    erase_partition as edl_erase_partition,
    flash_firmware as edl_flash_firmware,
    load_programmer,
    power_reset,
    read_device_info,
    read_gpt as edl_read_gpt,
    read_partition as edl_read_partition,
    release_session as edl_release_session,
    storage_info,
)
from huaxin.core.qualcomm import (
    memory_name_choices,
)
from huaxin.core.qualcomm import (
    verify_toolchain as edl_verify_toolchain,
)
from huaxin.ui import components as ui
from huaxin.ui import tabkit, tabviews, tokens
from huaxin.ui.dialogs import FlashPartitionDialog, ask_partition, confirm_destructive
from huaxin.ui import filedialog
from huaxin.ui.safe_slot import safe_slot
from huaxin.ui.widgets import JobProgressBar, PartitionTableWidget, ScatterTableWidget

__all__ = [
    "ActionSpec",
    "AndroidPanel",
    "MediaTekPanel",
    "QualcommPanel",
    "SamsungPanel",
    "SpdPanel",
    "VendorPanel",
]

_BUTTON_COLUMNS = 2


def _human_size(count: float) -> str:
    """Bytes as text for a confirmation dialog."""
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """Declarative description of one action button.

    `handler` is a bound method of the panel, called with the selected device
    (or None). It runs on the UI thread, so it may open dialogs but must submit
    the actual work to the worker thread via the service.
    """

    key: str
    label: str
    help: str
    requires_device: bool = True
    danger: bool = False
    handler: Callable[[Device | None], None] | None = None
    #: Overrides the panel's phase for this action alone, for a panel where some
    #: actions are implemented and others are not.
    phase: str = ""
    #: False for an action whose button is shown but cannot work in this build.
    #:
    #: The button is then disabled rather than merely explaining itself in a
    #: tooltip: a control that is pressable and does nothing is a control the
    #: operator presses twice and then stops trusting. Disabled with the reason in
    #: the tooltip is the honest form - it says what the action is, that this build
    #: cannot do it, and why.
    implemented: bool = True
    #: Icon name from huaxin.ui.icons. Optional: an action without one renders
    #: as text only, which is what a rarely used button should do.
    icon: str | None = None


class VendorPanel(QWidget):
    """Base class: header, selected-device banner, and an action button grid."""

    #: Label of this panel's tab in the main window.
    tab_name = "Vendor"
    tab_icon = "usb"

    #: Shown in the banner and in the log line an unimplemented action produces.
    phase = "a later phase"

    #: Which badge the header shows: ready, partial, pending or unavailable.
    #: Declared rather than guessed from the banner text, because "partly
    #: implemented" and "implemented" are the difference between an operator
    #: trusting the buttons and checking each one first.
    status_state = "ready"

    def __init__(
        self,
        service: BackendService,
        *,
        title: str,
        subtitle: str,
        actions: tuple[ActionSpec, ...],
        banner: str | None = None,
        body: QWidget | None = None,
        description: str = "",
        state: str = "pending",
        info: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._actions = actions
        self._selected: Device | None = None
        self._buttons: dict[str, QPushButton] = {}

        # The header is one widget rather than four labels assembled here, so all
        # five tabs get the same title, description, status badge and device box
        # from one place - which is what stops five tabs looking almost alike.
        self.header = tabkit.TabHeader(
            self,
            title=title,
            description=description or subtitle,
            status=banner if banner is not None else (
                f"Actions are wired to the worker thread but not implemented yet - "
                f"protocol lands in {self.phase}."
            ),
            state=state if state != "pending" else (
                self.status_state if banner is not None else "pending"
            ),
            icon=self.tab_icon,
        )
        self._phase_label = self.header.badge
        self._context = self.header.device_box

        self.actions = tabkit.ActionGrid(actions, self, on_action=self._on_action)
        self._buttons = self.actions.buttons()

        self.info = tabkit.InfoNote(
            info or "Operations run on the backend worker thread; the window stays "
                    "responsive while a job is in flight.",
            self,
            icon="info",
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            tokens.METRICS.pad, tokens.METRICS.pad, tokens.METRICS.pad, tokens.METRICS.pad
        )
        layout.setSpacing(tokens.METRICS.gap)
        layout.addWidget(self.header)
        layout.addWidget(self.actions)
        if body is not None:
            # The body takes the spare height instead of a stretch, so a table
            # inside it gets real rows rather than collapsing to its size hint.
            layout.addWidget(body, 1)
        else:
            layout.addStretch(1)
        layout.addWidget(self.info)

        self.set_selected_device(None)

    # -- context -----------------------------------------------------------

    def set_selected_device(self, device: Device | None) -> None:
        self._selected = device
        self._context.setDevice(device)
        self.refresh_actions()

    def refresh_actions(self) -> None:
        """Re-evaluate button enablement against selection and busy state."""
        busy = self._service.is_busy
        self.actions.set_enabled_states(
            has_device=self._selected is not None, busy=busy
        )
        self.actions.set_busy(busy)

    def set_status(self, text: str, state: str = "pending") -> None:
        """Replaces the header's status badge.

        For a tab whose state changes while it is open - the SPD and Samsung tabs
        go from "load a package" to "package loaded, flashing still unavailable",
        which is not known when the tab is built.
        """
        self._phase_label.setText(text)
        self._phase_label.setState(state)

    # -- actions -----------------------------------------------------------

    def _on_action(self, spec: ActionSpec) -> None:
        if spec.handler is not None:
            try:
                spec.handler(self._selected)
            except Exception as exc:  # a broken handler must not kill the UI
                self._service.log_message("error", f"action '{spec.label}' raised {type(exc).__name__}: {exc}")
            return

        self._service.log_message(
            "warn",
            f"'{spec.label}' is not implemented yet — it lands in {spec.phase or self.phase}.",
        )

    def _pick_file(self, kind: str, patterns: str, description: str) -> None:
        """Shared implementation for the firmware-package pickers."""
        path_str, _ = filedialog.open_file_name(self, description, str(Path.home()), patterns)
        if not path_str:
            self._service.log_message("info", f"{kind}: file selection cancelled")
            return
        path = Path(path_str)
        try:
            size_mb = path.stat().st_size / (1024 * 1024)
        except OSError as exc:
            self._service.log_message("error", f"{kind}: cannot read {path} ({exc})")
            return
        self._service.log_message("ok", f"{kind}: selected {path.name} ({size_mb:.1f} MiB) — {path}")
        self._service.log_message("warn", f"{kind}: parsing is not implemented yet — lands in {self.phase}.")


class AndroidPanel(VendorPanel):
    """Stock Android: ADB and Fastboot. Implemented - these run real commands."""

    tab_name = "ADB / Fastboot"
    tab_icon = "phone"
    phase = "Phase 4 (ADB / Fastboot)"

    def __init__(self, service: BackendService, parent: QWidget | None = None) -> None:
        super().__init__(
            service,
            title="ADB / Fastboot",
            subtitle=(
                "Stock Android devices. ADB talks to a booted system, Fastboot to the "
                "bootloader. Both run the official platform-tools binaries as subprocesses, so "
                "adb and fastboot must be installed; the toolchain button reports where they "
                "were found."
            ),
            banner="✔  Implemented — runs the official adb and fastboot binaries on the worker thread.",
            actions=(
                ActionSpec(
                    "toolchain", "Check Toolchain",
                    "Locate adb and fastboot and report their versions.",
                    icon="success", requires_device=False, handler=self._act_toolchain,
                ),
                ActionSpec(
                    "list_adb", "List ADB Devices",
                    "adb devices -l: devices visible to the ADB server and their state.",
                    icon="read", requires_device=False, handler=self._act_list_adb,
                ),
                ActionSpec(
                    "list_fastboot", "List Fastboot Devices",
                    "fastboot devices -l: devices currently in fastboot or fastbootd mode.",
                    icon="read", requires_device=False, handler=self._act_list_fastboot,
                ),
                ActionSpec(
                    "adb_info", "Read Device Info (adb)",
                    "adb shell getprop: model, build, security patch level and the active slot.",
                    icon="info", handler=self._act_device_info,
                ),
                ActionSpec(
                    "reboot_bootloader", "Reboot to Bootloader",
                    "adb reboot bootloader — the usual way into fastboot mode.",
                    icon="power", handler=self._act_reboot_bootloader,
                ),
                ActionSpec(
                    "reboot_edl", "Reboot to EDL",
                    "adb reboot edl — Qualcomm-based devices only; other vendors ignore it.",
                    handler=self._act_reboot_edl,
                ),
                ActionSpec(
                    "reboot_download", "Reboot to Download Mode",
                    "adb reboot download — Samsung devices only.",
                    handler=self._act_reboot_download,
                ),
                ActionSpec(
                    "flash_partition", "Flash Partition (fastboot)",
                    "fastboot flash <partition> <image>. The device must be in fastboot mode.",
                    icon="flash", handler=self._act_flash_partition,
                ),
                ActionSpec(
                    "erase_partition", "Erase Partition (fastboot)",
                    "fastboot erase <partition>. Destructive and not recoverable.",
                    danger=True, handler=self._act_erase_partition,
                ),
                ActionSpec(
                    "wipe_data", "Wipe Data (fastboot -w)",
                    "Erases userdata and cache. Every account, file and setting on the device is lost.",
                    danger=True, handler=self._act_wipe_data,
                ),
            ),
            parent=parent,
        )

    # -- helpers -----------------------------------------------------------

    def _target_serial(self, device: Device | None) -> str | None:
        """Which device to address, or None to let adb/fastboot pick the only one.

        The USB serial we read during enumeration is the same string adb uses,
        so a selected device can be targeted explicitly. Without one, adb and
        fastboot operate on the single connected device and report clearly if
        there is more than one.
        """
        if device is None:
            return None
        return device.serial or None

    def _describe_target(self, serial: str | None) -> str:
        return f"device {serial}" if serial else "the only connected device"

    # -- discovery actions -------------------------------------------------

    def _act_toolchain(self, _device: Device | None) -> None:
        self._service.submit("adb.toolchain", verify_toolchain)

    def _act_list_adb(self, _device: Device | None) -> None:
        self._service.submit("adb.devices", get_adb_devices)

    def _act_list_fastboot(self, _device: Device | None) -> None:
        self._service.submit("fastboot.devices", get_fastboot_devices)

    # -- informational -----------------------------------------------------

    def _act_device_info(self, device: Device | None) -> None:
        serial = self._target_serial(device)
        self._service.log_message("info", f"reading properties from {self._describe_target(serial)}")
        self._service.submit("adb.info", get_device_properties, serial=serial)

    # -- reboot ------------------------------------------------------------

    def _note_reenumeration(self, _result: object) -> None:
        """After a reboot the device leaves and rejoins the bus under a new identity."""
        self._service.log_message(
            "info",
            "the device will disconnect and re-enumerate as a different USB device; "
            "wait a few seconds, then run Scan Devices",
        )

    def _act_reboot_bootloader(self, device: Device | None) -> None:
        self._service.submit(
            "adb.reboot-bootloader",
            reboot_to_bootloader,
            serial=self._target_serial(device),
            on_success=self._note_reenumeration,
        )

    def _act_reboot_edl(self, device: Device | None) -> None:
        self._service.submit(
            "adb.reboot-edl",
            reboot_device,
            serial=self._target_serial(device),
            target="edl",
            on_success=self._note_reenumeration,
        )

    def _act_reboot_download(self, device: Device | None) -> None:
        self._service.submit(
            "adb.reboot-download",
            reboot_device,
            serial=self._target_serial(device),
            target="download",
            on_success=self._note_reenumeration,
        )

    # -- fastboot writes ---------------------------------------------------

    def _act_flash_partition(self, device: Device | None) -> None:
        serial = self._target_serial(device)
        dialog = FlashPartitionDialog(self, serial=serial)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._service.log_message("info", "flash cancelled")
            return
        partition, image = dialog.values()
        self._service.submit(
            "fastboot.flash", flash_partition, serial=serial, partition=partition, image=image
        )

    def _act_erase_partition(self, device: Device | None) -> None:
        serial = self._target_serial(device)
        partition = ask_partition(
            self,
            title="Erase partition",
            prompt="Partition to erase:",
            suggestion="userdata",
        )
        if partition is None:
            self._service.log_message("info", "erase cancelled")
            return
        if not confirm_destructive(
            self,
            title="Erase partition",
            message=f"Erase <b>{partition}</b> on {self._describe_target(serial)}?",
            command=f"fastboot erase {partition}",
            confirm_label="Erase",
        ):
            self._service.log_message("info", "erase cancelled at the confirmation step")
            return
        self._service.submit("fastboot.erase", erase_partition, serial=serial, partition=partition)

    def _act_wipe_data(self, device: Device | None) -> None:
        serial = self._target_serial(device)
        if not confirm_destructive(
            self,
            title="Wipe user data",
            message=(
                "This erases <b>userdata</b> and <b>cache</b> on "
                f"{self._describe_target(serial)}.<br><br>"
                "Every account, photo, file and setting on the device is destroyed, and the "
                "device will boot into setup again. This cannot be undone."
            ),
            command="fastboot -w",
            confirm_label="Erase everything",
        ):
            self._service.log_message("info", "wipe cancelled at the confirmation step")
            return
        self._service.submit("fastboot.wipe", wipe_data, serial=serial)


class QualcommPanel(VendorPanel):
    status_state = "partial"
    """Qualcomm EDL: Sahara handshake, then Firehose programming.

    Firehose commands need a programmer uploaded first, and every button here
    says so rather than failing obscurely. The session that upload opens stays
    open - configure, read GPT, flash and erase all happen inside it, because
    tearing it down between clicks would reset the storage engine the next step
    depends on.
    """

    tab_name = "Qualcomm"
    tab_icon = "chip"
    phase = "Phase 5 (Sahara / Firehose)"

    #: Jobs whose progress drives the bar. Everything else the panel submits
    #: (identity, configure) is over in a moment and would only flicker.
    _PROGRESS_JOBS = frozenset(
        {"edl.flash-firmware", "edl.flash-partition", "edl.read-partition"}
    )

    def __init__(self, service: BackendService, parent: QWidget | None = None) -> None:
        self._table = PartitionTableWidget()
        # Enough for the header plus several partitions even in a short window;
        # a table that shows one row is worse than no table.
        self._table.setMinimumHeight(180)
        self._progress = JobProgressBar()
        self._geometry: object | None = None

        super().__init__(
            service,
            title="Qualcomm (EDL)",
            subtitle=(
                "Emergency Download mode, USB 05C6:9008. Sahara runs first: it reads the chip "
                "identity and uploads a Firehose programmer, after which the device accepts XML "
                "commands. On Windows the interface must have a WinUSB-class driver bound "
                "(Zadig), because libusb cannot open it through Qualcomm's own driver."
            ),
            banner=(
                "◐  Partly implemented — Sahara, programmer upload, configure, storage info, "
                "GPT read, partition read/write/erase and rawprogram/patch replay are wired. "
                "None of it has been run against real hardware in this build."
            ),
            actions=(
                ActionSpec(
                    "edl_status", "Check EDL Device",
                    "Reports whether a device in EDL mode is on the bus and whether it can be opened.",
                    requires_device=False, handler=self._act_status,
                ),
                ActionSpec(
                    "read_chip_info", "Read Chip Info (Sahara)",
                    "Reads the serial number, MSM/OEM/model ids and the OEM PK hash over Sahara "
                    "command mode. Nothing is uploaded and the device is left as it was. "
                    "Unavailable while a Firehose session is open.",
                    requires_device=False, handler=self._act_read_info,
                ),
                ActionSpec(
                    "load_programmer", "Load Firehose Programmer…",
                    "Uploads the signed programmer (prog_firehose_ddr.elf) through Sahara and "
                    "opens a Firehose session. Everything below needs this first.",
                    requires_device=False, handler=self._act_load_programmer,
                ),
                ActionSpec(
                    "firehose_configure", "Configure Device",
                    "Sends <configure>: negotiates the payload size and, optionally, the storage "
                    "type. Required before any read or write.",
                    requires_device=False, handler=self._act_configure,
                ),
                ActionSpec(
                    "firehose_storage", "Get Storage Info",
                    "Sends <getstorageinfo>: total blocks and block size for one LUN.",
                    requires_device=False, handler=self._act_storage_info,
                ),
                ActionSpec(
                    "read_gpt", "Read GPT",
                    "Reads LBA 1 and the partition array through Firehose and fills the table "
                    "below with the partitions of the chosen LUN.",
                    requires_device=False, handler=self._act_read_gpt,
                ),
                ActionSpec(
                    "flash_firmware", "Flash Firmware…",
                    "Replays a rawprogram0.xml (and its patch0.xml) exactly as the package "
                    "shipped it. This writes to the device.",
                    requires_device=False, danger=True, handler=self._act_flash_firmware,
                ),
                ActionSpec(
                    "read_partition", "Read Partition…",
                    "Dumps a sector range of a partition to a file, for backup or inspection.",
                    icon="read", requires_device=False, handler=self._act_read_partition,
                ),
                ActionSpec(
                    "erase_partition", "Erase Partition…",
                    "Erases a sector range. The data is gone; there is no undo.",
                    icon="erase", requires_device=False, danger=True, handler=self._act_erase_partition,
                ),
                ActionSpec(
                    "apply_patch", "Apply Patch (patch0.xml)…",
                    "Writes the values a patch0.xml describes into partitions that are already "
                    "programmed. Run it after a flash, never before.",
                    requires_device=False, handler=self._act_apply_patch,
                ),
                ActionSpec(
                    "firehose_reset", "Reset Device",
                    "Sends <power value=\"reset\">: restarts the device out of EDL and closes "
                    "the session.",
                    requires_device=False, danger=True, handler=self._act_reset,
                ),
                ActionSpec(
                    "release_session", "Close Firehose Session",
                    "Drops the USB handle without resetting the device. The programmer stays "
                    "loaded but nobody is talking to it; the next action needs a fresh upload.",
                    requires_device=False, handler=self._act_release,
                ),
            ),
            body=self._build_body(),
            parent=parent,
        )

        service.job_progress_changed.connect(safe_slot(self._on_job_progress))
        service.job_finished.connect(safe_slot(self._on_job_finished))

    def _build_body(self) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(6)

        caption = QLabel(
            "Partition table — read it with <b>Read GPT</b>. Select a row to aim "
            "<b>Read Partition</b> or <b>Erase Partition</b> at it.",
            body,
        )
        caption.setObjectName("PanelSubtitle")
        caption.setWordWrap(True)

        layout.addWidget(caption)
        layout.addWidget(self._table, 1)
        layout.addWidget(self._progress)
        return body

    # -- actions -----------------------------------------------------------

    def _act_status(self, _device: Device | None) -> None:
        self._service.submit("edl.status", edl_verify_toolchain)

    def _act_read_info(self, _device: Device | None) -> None:
        self._service.submit("edl.identity", read_device_info)

    def _act_load_programmer(self, _device: Device | None) -> None:
        path, _ = filedialog.open_file_name(
            self,
            "Select a Firehose programmer",
            str(Path.home()),
            "Programmers (*.elf *.mbn *.bin);;All files (*)",
        )
        if not path:
            self._service.log_message("info", "programmer selection cancelled")
            return
        # A new programmer replaces the old session, so the old table no longer
        # describes the device in front of us.
        self._table.set_table(None)
        self._service.submit(
            "edl.load-programmer", load_programmer, path, on_success=self._on_programmer_loaded
        )

    def _act_configure(self, _device: Device | None) -> None:
        # The storage name is not verified against a primary source, so it is an
        # explicit, editable choice rather than a hidden default, and leaving it
        # empty - letting the programmer pick - is offered first and is the
        # default. See MEMORY_NAME_CHOICES for what is known about the spellings.
        name, accepted = QInputDialog.getItem(
            self,
            "Configure device",
            "Storage type to declare (MemoryName).\n"
            "The spelling is device-specific and unverified in this build;\n"
            "leave it empty to let the programmer use its own default:",
            list(memory_name_choices()),
            0,
            True,
        )
        if not accepted:
            self._service.log_message("info", "configure cancelled")
            return
        self._service.submit("edl.configure", edl_configure, name.strip())

    def _act_storage_info(self, _device: Device | None) -> None:
        lun, accepted = QInputDialog.getInt(self, "Storage info", "LUN:", 0, 0, 5)
        if not accepted:
            self._service.log_message("info", "storage info cancelled")
            return
        self._service.submit(
            "edl.storage-info", storage_info, lun, on_success=self._on_geometry
        )

    def _act_read_gpt(self, _device: Device | None) -> None:
        lun, accepted = QInputDialog.getInt(
            self,
            "Read GPT",
            "LUN (0 is the primary storage on every target this tool supports):",
            0, 0, 5,
        )
        if not accepted:
            self._service.log_message("info", "GPT read cancelled")
            return
        self._progress.begin(f"Reading the partition table of LUN {lun}…")
        self._service.submit(
            "edl.read-gpt", edl_read_gpt, lun, 0, on_success=self._on_gpt, on_failure=self._on_gpt_failed
        )

    def _act_flash_firmware(self, _device: Device | None) -> None:
        path, _ = filedialog.open_file_name(
            self,
            "Select the rawprogram file of the package",
            str(Path.home()),
            "Firehose packages (rawprogram*.xml patch*.xml);;XML files (*.xml);;All files (*)",
        )
        if not path:
            self._service.log_message("info", "firmware selection cancelled")
            return

        xml_path = Path(path)
        try:
            # Parsed on the UI thread on purpose: it is a pure function over a
            # small text file, and it lets the confirmation show the real plan -
            # which partitions, from which sectors, from which files.
            steps = describe_rawprogram(xml_path)
        except Exception as exc:  # noqa: BLE001 - a bad package is reported, not raised
            self._service.log_message("error", f"cannot read {xml_path.name}: {exc}")
            return

        missing = [step for step in steps if not step.exists]
        plan = "<br>".join(
            f"&nbsp;&nbsp;• <b>{step.label or step.filename}</b> — "
            f"{_human_size(step.size_bytes)} at sector {step.start_sector}"
            + ("" if step.exists else " <span style='color:#ff6b6b'>(file missing)</span>")
            for step in steps[:20]
        )
        if len(steps) > 20:
            plan += f"<br>&nbsp;&nbsp;… and {len(steps) - 20} more"

        patch_path = self._sibling_patch_file(xml_path)
        patch_note = (
            f"<br><br>Patch file: <b>{patch_path.name}</b> will be applied afterwards."
            if patch_path
            else "<br><br>No patch file was found next to it; none will be applied."
        )
        if not confirm_destructive(
            self,
            title="Flash firmware",
            message=(
                f"Write <b>{len(steps)} partition(s)</b> from "
                f"<b>{xml_path.name}</b> to the device?<br><br>{plan}{patch_note}"
                + (
                    f"<br><br><span style='color:#ff6b6b'>{len(missing)} image file(s) are "
                    "missing and the run will be refused.</span>"
                    if missing
                    else ""
                )
            ),
            command=f"Firehose replay of {xml_path.name}",
            confirm_label="Flash",
        ):
            self._service.log_message("info", "firmware flash cancelled")
            return

        self._progress.begin(f"Flashing {xml_path.name}…")
        self._service.submit(
            "edl.flash-firmware",
            edl_flash_firmware,
            str(xml_path),
            str(patch_path) if patch_path else "",
            cancellable=True,
            on_failure=self._on_transfer_failed,
        )

    def _act_read_partition(self, _device: Device | None) -> None:
        target = self._resolve_range("Read partition")
        if target is None:
            return
        start, sectors, label = target

        suggested = f"{label or 'partition'}-{start}.img"
        destination, _ = filedialog.save_file_name(
            self, "Save the dump as", str(Path.home() / suggested), "Disk images (*.img);;All files (*)"
        )
        if not destination:
            self._service.log_message("info", "read cancelled")
            return

        self._progress.begin(f"Reading {label or 'sectors'}…")
        self._service.submit(
            "edl.read-partition",
            edl_read_partition,
            destination,
            start,
            sectors,
            cancellable=True,
            on_failure=self._on_transfer_failed,
        )

    def _act_erase_partition(self, _device: Device | None) -> None:
        target = self._resolve_range("Erase partition")
        if target is None:
            return
        start, sectors, label = target

        if not confirm_destructive(
            self,
            title="Erase partition",
            message=(
                f"Erase <b>{label or f'{sectors} sectors'}</b> starting at sector "
                f"<b>{start}</b>?<br><br>The contents are gone and the device will not boot "
                "unless the partition is written again."
            ),
            command=f'<erase start_sector="{start}" num_partition_sectors="{sectors}"/>',
            confirm_label="Erase",
        ):
            self._service.log_message("info", "erase cancelled")
            return

        self._service.submit(
            "edl.erase-partition",
            edl_erase_partition,
            start,
            sectors,
            label=label or "",
        )

    def _act_apply_patch(self, _device: Device | None) -> None:
        path, _ = filedialog.open_file_name(
            self,
            "Select a patch file",
            str(Path.home()),
            "Patch files (patch*.xml);;XML files (*.xml);;All files (*)",
        )
        if not path:
            self._service.log_message("info", "patch selection cancelled")
            return
        if not confirm_destructive(
            self,
            title="Apply patch",
            message=(
                f"Write the values in <b>{Path(path).name}</b> into the partitions they "
                "name?<br><br>This modifies images that are already on the flash."
            ),
            command=f"Firehose replay of {Path(path).name}",
            confirm_label="Apply",
        ):
            self._service.log_message("info", "patch cancelled")
            return
        self._service.submit("edl.apply-patch", edl_apply_patch, path)

    def _act_reset(self, _device: Device | None) -> None:
        if not confirm_destructive(
            self,
            title="Reset device",
            message=(
                "Send <b>&lt;power value=\"reset\"&gt;</b> to the device in EDL mode?<br><br>"
                "The device restarts. Any programmer that was uploaded is lost, so Firehose "
                "commands will need a fresh upload afterwards."
            ),
            command='fastboot-style Firehose: <power value="reset" DelayInSeconds="10"/>',
            confirm_label="Reset",
        ):
            self._service.log_message("info", "reset cancelled")
            return
        self._progress.reset()
        self._service.submit("edl.power-reset", power_reset, on_success=lambda _r: self._clear_table())

    def _act_release(self, _device: Device | None) -> None:
        self._progress.reset()
        self._clear_table()
        self._service.submit("edl.release-session", edl_release_session)

    # -- helpers -----------------------------------------------------------

    def _sibling_patch_file(self, rawprogram: Path) -> Path | None:
        """The patch file that belongs to a rawprogram, if it is next to it.

        Packages name them inconsistently (patch0.xml, patch0.1.xml, even
        patch.xml), so the sibling scan accepts the numbered and unnumbered
        spellings rather than insisting on one.
        """
        for candidate in ("patch0.xml", "patch0.1.xml", "patch.xml", "patch1.xml"):
            sibling = rawprogram.parent / candidate
            if sibling.is_file():
                return sibling
        return None

    def _resolve_range(self, title: str) -> tuple[int, int, str] | None:
        """The sector range to act on: the table selection, or typed in.

        Returns (start_sector, sector_count, label) or None when the operator
        backed out.
        """
        record = self._table.selected_partition()
        if record is not None:
            return record.first_sector, record.sector_count, record.display_name

        self._service.log_message(
            "info",
            f"{title.lower()}: no partition is selected in the table, so the range has to be "
            "typed in. Select a row to use the GPT's own numbers.",
        )
        start, accepted = QInputDialog.getInt(self, title, "Start sector:", 0, 0, 2**31 - 1)
        if not accepted:
            self._service.log_message("info", f"{title.lower()} cancelled")
            return None
        sectors, accepted = QInputDialog.getInt(self, title, "Number of sectors:", 1, 1, 2**31 - 1)
        if not accepted:
            self._service.log_message("info", f"{title.lower()} cancelled")
            return None
        return start, sectors, ""

    def _clear_table(self) -> None:
        self._table.set_table(None)

    # -- job results -------------------------------------------------------

    def _on_programmer_loaded(self, _identity: object) -> None:
        self._progress.finish("Programmer loaded — configure the device next.")

    def _on_geometry(self, geometry: object) -> None:
        self._geometry = geometry
        if getattr(geometry, "is_known", False):
            self._service.log_message(
                "ok",
                f"storage geometry cached: {geometry.block_size}-byte blocks, "
                f"{geometry.total_blocks} of them",
            )

    def _on_gpt(self, table: object) -> None:
        if table is None:
            return
        self._table.set_table(table)
        used = len(table.used) if hasattr(table, "used") else 0
        self._progress.finish(
            f"{used} partition(s) in use, {table.entry_slots} slots, "
            f"{table.sector_size}-byte sectors (disk {table.disk_guid})"
        )

    def _on_gpt_failed(self, _kind: str, _tb: str) -> None:
        self._table.set_table(None)

    def _on_transfer_failed(self, _kind: str, _tb: str) -> None:
        self._progress.finish("The operation did not complete — see the log.")

    def _on_job_progress(self, job_name: str, percent: int, message: str) -> None:
        if job_name in self._PROGRESS_JOBS:
            self._progress.set_progress(percent, message)

    def _on_job_finished(self, job_name: str) -> None:
        # A cancelled or failed transfer never reaches its own success handler, so
        # the bar is settled here rather than left mid-flight forever.
        if job_name in self._PROGRESS_JOBS and self._progress.isVisible():
            self._progress.finish()


class MediaTekPanel(VendorPanel):
    status_state = "partial"
    """MediaTek BROM, download agent injection, and flashing through the agent.

    The panel follows the protocol's own order rather than presenting the commands
    as a flat list: the bootrom can only load an agent and jump to it, and every
    storage command belongs to the agent that upload left running. The buttons say
    which half they are, and the ones that need an agent refuse with that reason
    rather than sending bytes to a bootrom that cannot read them.
    """

    tab_name = "MediaTek"
    tab_icon = "chip"
    phase = "Phase 6 (BROM / DA injection)"

    #: Jobs whose progress drives the bar and the detail line.
    _PROGRESS_JOBS = frozenset({"mtk.flash", "mtk.readback", "mtk.format"})

    def __init__(self, service: BackendService, parent: QWidget | None = None) -> None:
        self._table = ScatterTableWidget()
        self._table.setMinimumHeight(180)
        self._progress = JobProgressBar()
        self._summary: object | None = None

        super().__init__(
            service,
            title="MediaTek (BROM)",
            subtitle=(
                "Boot ROM and preloader modes, USB 0E8D. The bootrom exposes no storage "
                "commands, so nothing can be read or written until a download agent has been "
                "uploaded into SRAM and started. On Windows the interface needs a "
                "WinUSB-class driver bound (Zadig); the stock VCOM driver will not work."
            ),
            banner=(
                "◐  Partly implemented — handshake, chip identification, agent upload and the "
                "agent's read/write/erase commands are wired, including scatter replay. None of "
                "it has been run against real hardware in this build."
            ),
            actions=(
                ActionSpec(
                    "mtk_status", "Check Device",
                    "Reports whether a device in BROM or preloader mode is attached, and which.",
                    requires_device=False, handler=self._act_status,
                ),
                ActionSpec(
                    "mtk_handshake_info", "BROM Handshake + Chip Info",
                    "Runs the complemented-echo handshake, then reads the hardware code, "
                    "version block and boot ROM version.",
                    requires_device=False, handler=self._act_chip_info,
                ),
                ActionSpec(
                    "mtk_target_config", "Read Target Config",
                    "Reports the bootrom's security configuration: secure boot, SLA, memory "
                    "read/write permissions.",
                    requires_device=False, handler=self._act_target_config,
                ),
                ActionSpec(
                    "mtk_load_da", "Load Download Agent…",
                    "Uploads a download agent into SRAM (SEND_DA) and starts it (JUMP_DA). "
                    "Until this succeeds the bootrom cannot read or write anything.",
                    requires_device=False, handler=self._act_load_da,
                ),
                ActionSpec(
                    "mtk_flash_info", "Read Flash Info",
                    "Asks the running agent for its version, the storage geometry and the "
                    "transfer sizes it wants. Needs a running agent.",
                    requires_device=False, handler=self._act_flash_info,
                ),
                ActionSpec(
                    "mtk_scatter", "Load Scatter File…",
                    "Parses an MTK scatter file and lists its partitions below. Nothing is "
                    "touched on the device; this is the plan, shown before it is carried out.",
                    requires_device=False, handler=self._act_load_scatter,
                ),
                ActionSpec(
                    "mtk_flash", "Flash Firmware",
                    "Writes every partition the scatter file names, in file order. This writes "
                    "to the device.",
                    requires_device=False, danger=True, handler=self._act_flash,
                ),
                ActionSpec(
                    "mtk_readback", "Readback Partition",
                    "Reads a selected partition out to a file, through the running agent.",
                    requires_device=False, handler=self._act_readback,
                ),
                ActionSpec(
                    "mtk_format", "Format / Erase Flash",
                    "Erases the partitions the scatter file lists with no image. The contents "
                    "are gone and there is no undo.",
                    requires_device=False, danger=True, handler=self._act_format,
                ),
                ActionSpec(
                    "mtk_shutdown", "Shut Down Device",
                    "Tells the agent to shut the device down. It re-enumerates and the session "
                    "ends with it.",
                    requires_device=False, danger=True, handler=self._act_shutdown,
                ),
                ActionSpec(
                    "mtk_release", "Close Session",
                    "Drops the USB handle without touching the device. The agent stays loaded "
                    "but nobody is talking to it; the next action needs a fresh upload.",
                    requires_device=False, handler=self._act_release,
                ),
            ),
            body=self._build_body(),
            parent=parent,
        )

        service.job_progress_changed.connect(safe_slot(self._on_job_progress))
        service.job_finished.connect(safe_slot(self._on_job_finished))

    def _build_body(self) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(6)

        caption = QLabel(
            "Scatter plan — load a scatter file with <b>Load Scatter File…</b>. Each row says "
            "what will happen to that partition: <b>write</b> from an image, or <b>erase</b> "
            "because the package lists it with none.",
            body,
        )
        caption.setObjectName("PanelSubtitle")
        caption.setWordWrap(True)

        layout.addWidget(caption)
        layout.addWidget(self._table, 1)
        layout.addWidget(self._progress)
        return body

    # -- bootrom actions ---------------------------------------------------

    def _act_status(self, _device: Device | None) -> None:
        self._service.submit("mtk.status", mtk_verify_toolchain)

    def _act_chip_info(self, _device: Device | None) -> None:
        self._service.submit("mtk.chip-info", mtk_read_chip_info)

    def _act_target_config(self, _device: Device | None) -> None:
        self._service.submit("mtk.target-config", mtk_read_target_config)

    def _act_load_da(self, _device: Device | None) -> None:
        path, _ = filedialog.open_file_name(
            self,
            "Select a download agent",
            str(Path.home()),
            "Download agents (*.bin *.img);;All files (*)",
        )
        if not path:
            self._service.log_message("info", "download agent selection cancelled")
            return

        # The load address is not a documented constant across chip generations,
        # so it is shown and editable rather than hidden behind a default.
        address, accepted = QInputDialog.getText(
            self,
            "Download agent load address",
            "SRAM address to load the agent at (hex):",
            text=f"0x{DEFAULT_DA_ADDRESS:08x}",
        )
        if not accepted:
            self._service.log_message("info", "download agent upload cancelled")
            return
        try:
            load_address = int(address.strip(), 16)
        except ValueError:
            self._service.log_message("error", f"'{address}' is not a hexadecimal address")
            return

        if not confirm_destructive(
            self,
            title="Upload download agent",
            message=(
                f"Upload <b>{Path(path).name}</b> to SRAM at "
                f"<b>0x{load_address:08x}</b> and start it?<br><br>"
                "A download agent that does not match this chipset can hang or crash the "
                "bootrom. Power-cycle the device afterwards if it stops responding."
            ),
            command=f"SEND_DA 0x{load_address:08x} + JUMP_DA",
            confirm_label="Upload and start",
        ):
            self._service.log_message("info", "download agent upload cancelled")
            return

        self._service.submit("mtk.load-da", mtk_load_download_agent, path, load_address)

    # -- agent actions -----------------------------------------------------

    def _act_flash_info(self, _device: Device | None) -> None:
        self._service.submit(
            "mtk.flash-info", mtk_read_flash_info, on_success=self._on_flash_info
        )

    def _act_load_scatter(self, _device: Device | None) -> None:
        path, _ = filedialog.open_file_name(
            self,
            "Select an MTK scatter file",
            str(Path.home()),
            "Scatter files (*scatter*.txt *.txt);;All files (*)",
        )
        if not path:
            self._service.log_message("info", "scatter file selection cancelled")
            return
        # Pure host work, but it belongs on the worker like everything else so a
        # large package cannot stall the UI thread while it is read.
        self._service.submit("mtk.scatter", mtk_load_scatter, path, on_success=self._on_scatter)

    def _act_flash(self, _device: Device | None) -> None:
        if self._summary is None:
            self._service.log_message(
                "warn",
                "load a scatter file first: the package it names is what decides which "
                "partitions are written and where.",
            )
            return

        selected = [record.name for record in self._table.selected_partitions()]
        downloads = list(self._summary.downloads)
        if selected:
            wanted = [entry for entry in downloads if entry.name in selected]
            if not wanted:
                self._service.log_message(
                    "warn",
                    "none of the selected rows is a partition this package writes. Select a "
                    "row whose action is 'write', or clear the selection to write them all.",
                )
                return
        else:
            wanted = downloads

        if not wanted:
            self._service.log_message("warn", "the scatter file names nothing to write")
            return

        plan = "<br>".join(
            f"&nbsp;&nbsp;• <b>{entry.display_name}</b> — {_human_size(entry.size)} at "
            f"0x{entry.start_address:x} ({entry.file_name})"
            for entry in wanted[:20]
        )
        if len(wanted) > 20:
            plan += f"<br>&nbsp;&nbsp;… and {len(wanted) - 20} more"
        total = sum(entry.size for entry in wanted)

        if not confirm_destructive(
            self,
            title="Flash firmware",
            message=(
                f"Write <b>{len(wanted)} partition(s)</b> "
                f"({_human_size(total)}) to the device?<br><br>{plan}"
                "<br><br>Image files are looked up next to the scatter file. If any is "
                "missing the run is refused before anything is written."
            ),
            command=f"scatter replay of {Path(self._summary.path).name}",
            confirm_label="Flash",
        ):
            self._service.log_message("info", "firmware flash cancelled")
            return

        self._progress.begin(f"Flashing {Path(self._summary.path).name}…")
        self._service.submit(
            "mtk.flash",
            mtk_flash_firmware,
            self._summary.path,
            "",
            only=selected or None,
            cancellable=True,
            on_failure=self._on_transfer_failed,
        )

    def _act_readback(self, _device: Device | None) -> None:
        if self._summary is None:
            self._service.log_message(
                "warn", "load a scatter file first: it says how big each partition is"
            )
            return
        record = self._table.selected_partition()
        if record is None:
            self._service.log_message("warn", "select a partition row to read back")
            return

        suggested = f"{record.display_name}-{record.start_address:x}.img"
        destination, _ = filedialog.save_file_name(
            self, "Save the dump as", str(Path.home() / suggested),
            "Disk images (*.img *.bin);;All files (*)",
        )
        if not destination:
            self._service.log_message("info", "readback cancelled")
            return

        self._progress.begin(f"Reading {record.display_name}…")
        self._service.submit(
            "mtk.readback",
            mtk_readback_partition,
            self._summary.path,
            record.name,
            destination,
            cancellable=True,
            on_failure=self._on_transfer_failed,
        )

    def _act_format(self, _device: Device | None) -> None:
        if self._summary is None:
            self._service.log_message(
                "warn",
                "load a scatter file first. Formatting without one would mean guessing which "
                "regions to erase, and this tool does not guess at that.",
            )
            return

        targets = [entry for entry in self._summary.partitions if entry.is_empty_region]
        if not targets:
            self._service.log_message(
                "warn",
                "this scatter file lists no partition without an image, so there is nothing "
                "to erase. Every entry either has an image or no size.",
            )
            return

        listing = "<br>".join(
            f"&nbsp;&nbsp;• <b>{entry.display_name}</b> — {_human_size(entry.size)} at "
            f"0x{entry.start_address:x}"
            for entry in targets[:20]
        )
        total = sum(entry.size for entry in targets)
        if not confirm_destructive(
            self,
            title="Erase partitions",
            message=(
                f"Erase <b>{len(targets)} partition(s)</b> ({_human_size(total)}) that "
                f"{Path(self._summary.path).name} lists with no image?<br><br>{listing}"
                "<br><br>Their contents are gone and the device will not boot until the "
                "partitions are written again."
            ),
            command="agent FORMAT over the listed regions",
            confirm_label="Erase",
        ):
            self._service.log_message("info", "erase cancelled")
            return

        self._progress.begin("Erasing…")
        self._service.submit(
            "mtk.format",
            mtk_format_flash,
            self._summary.path,
            cancellable=True,
            on_failure=self._on_transfer_failed,
        )

    def _act_shutdown(self, _device: Device | None) -> None:
        if not confirm_destructive(
            self,
            title="Shut down the device",
            message=(
                "Tell the running download agent to shut the device down?<br><br>"
                "It re-enumerates, so the session ends with it and anything loaded into "
                "SRAM is lost."
            ),
            command="agent SHUTDOWN",
            confirm_label="Shut down",
        ):
            self._service.log_message("info", "shutdown cancelled")
            return
        self._progress.reset()
        self._service.submit(
            "mtk.shutdown", mtk_shutdown_device, on_success=lambda _result: self._clear()
        )

    def _act_release(self, _device: Device | None) -> None:
        if not mtk_session_open():
            self._service.log_message("info", "no MediaTek session is open")
            return
        self._progress.reset()
        self._clear()
        self._service.submit("mtk.release", mtk_release_session)

    # -- helpers -----------------------------------------------------------

    def _clear(self) -> None:
        self._summary = None
        self._table.set_scatter(None)

    # -- job results -------------------------------------------------------

    def _on_flash_info(self, summary: object) -> None:
        if summary is None:
            return
        self._service.log_message(
            "ok",
            f"agent reports {summary.storage_label} {_human_size(summary.total_size)}"
            + (f", {summary.block_size}-byte blocks" if summary.block_size else ""),
        )

    def _on_scatter(self, info: object) -> None:
        if info is None:
            return
        self._summary = info
        self._table.set_scatter(info)
        writes = len(info.downloads)
        erases = sum(1 for entry in info.partitions if entry.is_empty_region)
        self._progress.finish(
            f"{Path(info.path).name}: {writes} to write, {erases} to erase "
            f"({_human_size(info.total_download_bytes)})"
        )

    def _on_transfer_failed(self, _kind: str, _tb: str) -> None:
        self._progress.finish("The operation did not complete — see the log.")

    def _on_job_progress(self, job_name: str, percent: int, message: str) -> None:
        if job_name in self._PROGRESS_JOBS:
            self._progress.set_progress(percent, message)

    def _on_job_finished(self, job_name: str) -> None:
        # A cancelled or failed run never reaches its own success handler, so the
        # bar is settled here rather than left mid-flight.
        if job_name in self._PROGRESS_JOBS and self._progress.isVisible():
            self._progress.finish()


class SpdPanel(VendorPanel):
    """Unisoc / Spreadtrum Research Download mode.

    What works here: reading a .pac and showing its contents. The container
    parser is complete and tested, and exposing it means an operator can check
    they have the right package, with the right loaders, at the right size -
    before any of it goes near a device.

    What does not: talking to one. The research-download protocol is implemented
    and tested in C++ but has no USB transport and is not exposed, so the buttons
    that would write say so rather than pretending.
    """

    tab_name = "SPD / Unisoc"
    tab_icon = "chip"
    status_state = "partial"
    phase = "a later phase"

    def __init__(self, service: BackendService, parent: QWidget | None = None) -> None:
        self._pac_view = tabviews.PacContentsView()

        super().__init__(
            service,
            title="Unisoc / SPD",
            subtitle="Unisoc / Spreadtrum Research Download mode.",
            description=(
                "Research Download mode (USB 1782 \u00b7 unverified PID). Firmware ships as a "
                ".pac container holding a partition table, the FDL loaders and one image per "
                "partition. Loading a package reads its table and shows what is in it; it "
                "writes nothing."
            ),
            banner=(
                "\u25d0  Reads a device and its flash; does not replay a package yet \u2014 the "
                "Research Download link works (handshake, device identity, read-back, reset, "
                "power-off) and .pac inspection works. Flashing a package is not offered: the "
                "FDL1-to-FDL2 handover needs a primary source this project does not have. See "
                "docs/spd-status.md."
            ),
            state="partial",
            body=self._pac_view,
            info=(
                "A .pac is the whole firmware: loaders, partition table and images in one "
                "file. Reading it costs a few kilobytes \u2014 the payload is skipped \u2014 so the "
                "summary line is cheap even for a two-gigabyte package. The payload CRC "
                "cannot be checked by a listing, and the summary says so rather than "
                "implying a check that did not happen."
            ),
            actions=(
                ActionSpec(
                    "spd_status", "Check Device",
                    "Reports whether a device with the Unisoc vendor ID (1782) is attached. "
                    "Real, and says nothing about whether it is in Research Download mode.",
                    icon="usb", requires_device=False, handler=self._act_status,
                ),
                ActionSpec(
                    "load_pac", "Load PAC File\u2026",
                    "Reads a .pac and shows its header, entry table and loaders. Nothing is "
                    "written to a device.",
                    icon="file", requires_device=False, handler=self._act_load_pac,
                ),
                ActionSpec(
                    "handshake", "Research Download Handshake",
                    "Opens the device and runs the BSL hello, then reads what it says about "
                    "itself. The device must be in Research Download mode with the right USB "
                    "driver bound; without that the link is silently dead.",
                    icon="power", requires_device=False, handler=self._act_handshake,
                ),
                ActionSpec(
                    "read_info", "Read Device Info",
                    "Re-reads chip type, flash type, sector size and chip UID from the open "
                    "link. A device may decline any of them, which the log says rather than "
                    "showing a zero.",
                    icon="info", requires_device=False, handler=self._act_read_info,
                ),
                ActionSpec(
                    "read_back", "Read Back Entry…",
                    "Reads the selected package entry's address range off the device into a "
                    "file. Read-back is the check that does not depend on trusting the tool "
                    "that wrote the flash.",
                    icon="read", requires_device=False, handler=self._act_read_back,
                ),
                ActionSpec(
                    "flash_pac", "Flash PAC Firmware",
                    "Not available, and deliberately not attempted: replaying a package needs "
                    "the FDL1-to-FDL2 handover, which needs a primary source this project "
                    "does not have. A guessed sequence against a phone is worse than no "
                    "sequence at all.",
                    icon="flash", requires_device=False, danger=True,
                    handler=self._act_unavailable, implemented=False,
                ),
                ActionSpec(
                    "reset", "Reset Device",
                    "Restarts the device out of download mode and closes the session - the "
                    "usual last step after a read-back.",
                    icon="refresh", requires_device=False, handler=self._act_reset,
                ),
                ActionSpec(
                    "power_off", "Power Off",
                    "BSL_CMD_POWER_OFF, for a device whose download mode has no reset path.",
                    icon="stop", requires_device=False, handler=self._act_power_off,
                ),
            ),
            parent=parent,
        )

    # -- actions -----------------------------------------------------------

    def _act_status(self, _device: Device | None) -> None:
        self._service.submit("spd.status", spd_verify_toolchain)

    def _act_handshake(self, _device: Device | None) -> None:
        # No panel argument: `bsl_connect` returns what it learned and the log
        # already carried it, so there is nothing for the panel to display. The
        # panels that pass `self` do so because their job calls back into them.
        self._service.submit("spd.handshake", spd_connect)

    def _act_read_info(self, _device: Device | None) -> None:
        self._service.submit("spd.info", spd_read_device_info)

    def _act_read_back(self, _device: Device | None) -> None:
        """Reads the selected package entry's range off the device.

        The address and the length come from the package the operator loaded, which is
        what makes this a button rather than a form: the entry IS the range, and reading
        back what the package claims is the check worth doing.
        """
        entry = self._pac_view.selected_entry()
        if entry is None:
            self._service.log_message(
                "warn", "select a package entry first - its address and size are the "
                        "range that gets read")
            return

        name = entry.file_name or entry.file_id
        destination = filedialog.save_file_name(
            self, "Save the read-back as", str(Path.home() / f"{name}.bin"),
            "Binary images (*.bin *.img);;All files (*)",
        )
        if not destination:
            self._service.log_message("info", "read-back cancelled")
            return
        self._service.submit("spd.read_back", _read_back_entry, entry, Path(destination), self)

    def _act_reset(self, _device: Device | None) -> None:
        self._service.submit("spd.reset", spd_reset)

    def _act_power_off(self, _device: Device | None) -> None:
        self._service.submit("spd.power_off", spd_power_off)

    def _act_load_pac(self, _device: Device | None) -> None:
        path_str, _ = filedialog.open_file_name(
            self, "Select a .pac package", str(Path.home()),
            "Unisoc package (*.pac);;All files (*)",
        )
        if not path_str:
            self._service.log_message("info", "PAC: selection cancelled")
            return
        self.load_pac_file(Path(path_str))

    def load_pac_file(self, path: Path) -> None:
        """Loads a PAC from a path, with no dialog.

        Split out from the button handler so a dropped file and a chosen file
        take exactly the same route. The alternative is a second code path that
        only drag-and-drop exercises, which is where the difference between the
        two would hide.
        """
        self._service.submit("spd.load_pac", _read_pac, path, self)

    def _act_unavailable(self, _device: Device | None) -> None:
        self._service.log_message("warn", PENDING_REASON)

    # -- results (UI thread) -----------------------------------------------

    def set_package(self, package: object, path: Path) -> None:
        """Shows a parsed package. Called on the UI thread when the job finishes."""
        self._pac_view.setPackage(package)
        fdl1 = package.fdl1()
        fdl2 = package.fdl2()
        missing = []
        if fdl1 is None:
            # A package with no loaders cannot be used to recover a device, which
            # is the whole reason an operator would reach for one.
            missing.append("FDL1")
        if fdl2 is None:
            missing.append("FDL2")
        if missing:
            self.set_status(
                f"\u26a0  {path.name} loaded, but it has no {' or '.join(missing)} loader. "
                "A package without them cannot recover a device.",
                "unavailable",
            )
        else:
            self.set_status(
                f"\u25d0  {path.name} loaded \u2014 {len(package.entries)} entries, "
                f"{package.header.version_string or 'unknown version'}. Flashing is still "
                "not available.",
                "partial",
            )


def _read_back_entry(ctx, entry, destination: Path, panel) -> Path:
    """Reads a package entry's range off the device, on the worker thread.

    The range comes from the entry, so this is the one read operation that needs
    no form: the package already says where the data should be, and comparing what
    comes back against the package is the check worth doing.

    A short read is treated as a failure rather than as a smaller answer - see
    `bsl_read_region`, which refuses to write a truncated file.
    """
    written = spd_read_back(ctx, int(entry.address), int(entry.size), destination)
    name = entry.file_name or entry.file_id
    QTimer.singleShot(0, lambda: panel.set_status(
        f"✔  Read {int(entry.size):,} bytes of {name} back into {written.name}. "
        "Compare it against the package before trusting what is on the device.",
        "ok",
    ))
    return written


def _read_pac(ctx, path: Path, panel) -> object:
    """Reads a PAC's header and table on the worker thread, then shows it.

    `read_pac_header` rather than `load_pac`: a package is hundreds of megabytes
    to a few gigabytes, and a listing needs its first few kilobytes. The payload
    CRC is therefore left unchecked, and the view says so.
    """
    ctx.log(f"reading {path.name} ({path.stat().st_size / (1024 * 1024):.1f} MiB)")
    ctx.progress(-1, "reading the package header")
    package = parsers.read_pac(path)
    ctx.log(package.summary(), "ok" if package.crc_ok else "warn")

    fdl1 = package.fdl1()
    if fdl1 is not None:
        ctx.log(f"FDL1: {fdl1.file_name} ({fdl1.size} bytes)")
    else:
        ctx.log("this package carries no FDL1 loader", "warn")

    # The panel is a Qt object, so the update is queued onto its own thread.
    QTimer.singleShot(0, lambda: panel.set_package(package, path))
    return package


class SamsungPanel(VendorPanel):
    """Samsung Download mode: Odin, PIT and .tar.md5 packages.

    What works here: opening a PIT and a firmware package and showing what is in
    them - the partitions, the members, and which member belongs to which
    partition. Both parsers are complete and tested, and both read files rather
    than devices.

    What does not: talking to a device. The Odin protocol and the PIT/PIT-write
    paths are implemented and covered by 107 native checks, but there is no USB
    transport and nothing is exposed through pybind11, so the buttons that write
    say so.
    """

    tab_name = "Samsung"
    tab_icon = "phone"
    status_state = "partial"
    phase = "a later phase"

    def __init__(self, service: BackendService, parent: QWidget | None = None) -> None:
        self._pit_view = tabviews.PitTableView()
        self._package_view = tabviews.FirmwarePackageView()

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, tokens.METRICS.gap_sm, 0, 0)
        body_layout.setSpacing(tokens.METRICS.gap)
        # A splitter rather than two stacked widgets: an operator comparing a
        # package's members against the partition table wants both visible, and
        # how much of each is up to them.
        splitter = QSplitter(Qt.Orientation.Vertical, body)
        splitter.addWidget(self._pit_view)
        splitter.addWidget(self._package_view)
        splitter.setSizes([320, 200])
        splitter.setChildrenCollapsible(False)
        body_layout.addWidget(splitter)

        # The PIT decides the mapping column, so a new PIT re-maps a package that
        # was loaded before it.
        self._pit_view.table.itemSelectionChanged.connect(
            safe_slot(self._on_pit_selection)
        )

        super().__init__(
            service,
            title="Samsung (Download Mode)",
            subtitle="Samsung Download mode: Odin protocol with PIT and .tar.md5 handling.",
            description=(
                "Download mode (USB 04e8). Flashing uses two files: a PIT describing the "
                "device's partitions, and a .tar.md5 package holding one image per "
                "partition with its digest appended. Loading either one reads it and shows "
                "what is in it; nothing is written."
            ),
            banner=(
                "\u25d0  File inspection works; flashing does not \u2014 the PIT parser, the "
                "Odin session and the .tar.md5 reader are implemented and covered by 107 "
                "native checks, but there is no USB transport and nothing is exposed for "
                "writing, so those buttons report that instead."
            ),
            state="partial",
            body=body,
            info=(
                "The PIT is the one file here that can leave a device unable to boot: it "
                "decides what the partitions are. Repartitioning is not available in this "
                "build, and the button says so rather than offering a step that cannot be "
                "undone. Loading a package is safe and is worth doing first \u2014 it is how a "
                "truncated download gets caught."
            ),
            actions=(
                ActionSpec(
                    "check_download", "Check Download Mode",
                    "Reports whether a device with the Samsung vendor ID is attached. Real, "
                    "and says nothing about whether it is in Download mode.",
                    icon="usb", requires_device=False, handler=self._act_check,
                ),
                ActionSpec(
                    "load_pit", "Load PIT File\u2026",
                    "Opens a .pit and lists its partitions. Nothing is written anywhere.",
                    icon="file", requires_device=False, handler=self._act_load_pit,
                ),
                ActionSpec(
                    "read_pit", "Read PIT From Device",
                    "Not available: needs the Odin transport, which this build does not have.",
                    icon="read", requires_device=False, handler=self._act_unavailable, implemented=False,
                ),
                ActionSpec(
                    "load_package", "Load Firmware Package\u2026",
                    "Opens a .tar.md5 and lists its members, so a truncated download is "
                    "caught before anything is flashed.",
                    icon="folder", requires_device=False, handler=self._act_load_package,
                ),
                ActionSpec(
                    "flash_odin", "Flash (Odin)",
                    "Not available: writing needs the transport.",
                    icon="flash", requires_device=False, danger=True,
                    handler=self._act_unavailable, implemented=False,
                ),
                ActionSpec(
                    "repartition", "Repartition (PIT)",
                    "Not available. This is the most destructive action in the tool: the PIT "
                    "decides the partition table, and a wrong one leaves a device unable to "
                    "boot. It would also need the transport.",
                    icon="warning", requires_device=False, danger=True,
                    handler=self._act_unavailable, implemented=False,
                ),
            ),
            parent=parent,
        )

    # -- actions -----------------------------------------------------------

    def _act_check(self, _device: Device | None) -> None:
        self._service.submit("samsung.check", samsung_verify_toolchain)

    def _act_load_pit(self, _device: Device | None) -> None:
        path_str, _ = filedialog.open_file_name(
            self, "Select a PIT file", str(Path.home()),
            "Samsung PIT (*.pit);;All files (*)",
        )
        if not path_str:
            self._service.log_message("info", "PIT: selection cancelled")
            return
        self.load_pit_file(Path(path_str))

    def load_pit_file(self, path: Path) -> None:
        """Loads a PIT from a path, with no dialog. See `load_pac_file`."""
        self._service.submit("samsung.load_pit", _read_pit, path, self)

    def _act_load_package(self, _device: Device | None) -> None:
        path_str, _ = filedialog.open_file_name(
            self, "Select a firmware package", str(Path.home()),
            "Firmware package (*.tar.md5 *.tar);;All files (*)",
        )
        if not path_str:
            self._service.log_message("info", "package: selection cancelled")
            return
        self.load_package_file(Path(path_str))

    def load_package_file(self, path: Path) -> None:
        """Loads a Samsung package from a path, with no dialog."""
        self._service.submit("samsung.load_package", _read_package, path, self)

    def _act_unavailable(self, _device: Device | None) -> None:
        self._service.log_message(
            "warn",
            "Samsung writing is not available in this build: the Odin protocol is "
            "implemented and tested, but there is no USB transport and nothing is "
            "exposed for writing. Reading a PIT or a package still works.",
        )

    # -- results (UI thread) -----------------------------------------------

    def set_pit(self, pit: object, path: Path) -> None:
        self._pit_view.setPit(pit, path.name)
        self._package_view.setPartitionNames(self._pit_view.partition_names())
        check = getattr(pit, "sanity_problem", None)
        problem = check() if callable(check) else None
        if problem:
            # A PIT that does not look like a partition table is the one thing
            # worth shouting about here, because acting on it is unrecoverable.
            self.set_status(f"\u26a0  {path.name} loaded but looks wrong: {problem}",
                            "error")
        else:
            self.set_status(
                f"\u25d0  {path.name} loaded \u2014 {len(pit.entries)} partitions. "
                "Repartitioning is still not available.",
                "partial",
            )

    def set_package(self, archive: object, path: Path) -> None:
        self._package_view.setPackage(path, archive)
        self._package_view.setPartitionNames(self._pit_view.partition_names())
        unmapped = [
            entry.base_name
            for entry in archive.files()
            if self._package_view._partition_for(entry.base_name) == "\u2014"
        ]
        if not self._pit_view.partition_names():
            self.set_status(
                f"\u25d0  {path.name} loaded \u2014 {len(archive.files())} members. Load a PIT "
                "to see which partition each one belongs to.",
                "partial",
            )
        elif unmapped:
            self.set_status(
                f"\u25d0  {path.name} loaded \u2014 {len(archive.files())} members, "
                f"{len(unmapped)} not matching any partition in the PIT.",
                "warn",
            )
        else:
            self.set_status(
                f"\u25d0  {path.name} loaded \u2014 every member maps to a partition. "
                "Flashing is still not available.",
                "partial",
            )

    def _on_pit_selection(self) -> None:
        entry = self._pit_view.selected_partition()
        if entry is not None:
            self._service.log_message(
                "info",
                f"PIT: {entry.partition_name} - {entry.block_count:,} blocks of "
                f"{entry.block_size_or_offset} bytes",
            )


def _read_pit(ctx, path: Path, panel) -> object:
    """Parses a PIT on the worker thread."""
    ctx.log(f"reading the partition table from {path.name}")
    pit = parsers.read_pit(path)
    ctx.log(f"{len(pit.entries)} partitions", "ok")
    problem = getattr(pit, "sanity_problem", None)
    if callable(problem) and problem():
        ctx.log(f"this PIT looks wrong: {problem()}", "error")
    QTimer.singleShot(0, lambda: panel.set_pit(pit, path))
    return pit


def _read_package(ctx, path: Path, panel) -> object:
    """Lists a package's members on the worker thread.

    Streamed: a firmware package is two to six gigabytes and listing it needs the
    member names, not the images. Without that this would need more memory than
    the machine has, which is why the native reader skips past the data.
    """
    size_mb = path.stat().st_size / (1024 * 1024)
    ctx.log(f"reading {path.name} ({size_mb:.1f} MiB) - this reads the headers only")
    ctx.progress(-1, "listing the package")
    archive = parsers.read_package(path)
    files = archive.files()
    ctx.log(f"{len(files)} members, {archive.total_file_bytes / (1024 * 1024):.1f} MiB of payload",
            "ok")
    ctx.log(
        "the .md5 digest is appended to the archive as raw bytes; verifying it means "
        "reading the whole file and is not done by a listing",
        "info",
    )
    QTimer.singleShot(0, lambda: panel.set_package(archive, path))
    return archive

