# =============================================================================
#  PyInstaller spec for Huaxin Tool.
#
#  Build from the repository root:
#
#      python -m pip install -r requirements.txt pyinstaller
#      ./scripts/build.ps1                     # build huaxin_core first
#      pyinstaller packaging/huaxin.spec --noconfirm --clean
#
#  Or without the spec file, with every option spelled out:
#
#      pyinstaller python/main.py ^
#          --name HuaxinTool ^
#          --distpath dist --workpath build/pyinstaller ^
#          --paths python ^
#          --add-binary "python/huaxin_core.cp314-win_amd64.pyd;." ^
#          --add-data "drivers;drivers" ^
#          --add-data "docs;docs" ^
#          --add-data "README.md;." ^
#          --add-data "LICENSE;." ^
#          --add-data "packaging/settings.template.json;." ^
#          --hidden-import huaxin_core ^
#          --exclude-module PyQt6.QtWebEngineCore ^
#          --exclude-module PyQt6.QtQuick ^
#          --exclude-module tkinter ^
#          --noconfirm --clean --windowed --noupx
#
#  On Linux and macOS the --add-binary/--add-data separator is ":" instead of
#  ";". The spec file avoids that difference entirely, which is the other reason
#  to prefer it.
#
#  Output: dist/HuaxinTool/ containing HuaxinTool.exe and everything it needs.
#
#  Two things this spec does NOT do, because they cannot be done here:
#
#    * USB drivers. WinUSB and libusbK are Windows kernel drivers. They can be
#      installed (Zadig, or an INF), but not bundled into a folder and used from
#      it. What *is* bundled is drivers/ - the instructions and the helper
#      script - so the operator has them next to the executable. See
#      drivers/README.md; the honest answer is that first-time driver binding is
#      a user step.
#    * adb / fastboot. They are bundled when present, but they are Google's
#      binaries and carry their own licence; shipping them is your call, not
#      something a build script should decide silently.
# =============================================================================

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

REPO_ROOT = Path(SPECPATH).resolve().parent
PYTHON_ROOT = REPO_ROOT / "python"

# --- what ships alongside the executable -------------------------------------
datas = []

# The compiled backend. Imported as the top-level module `huaxin_core`, so it
# goes in the bundle root, not inside the package directory.
native_modules = sorted(PYTHON_ROOT.glob("huaxin_core*"))
if not native_modules:
    raise SystemExit(
        "huaxin_core was not found in python/.\n"
        "Build the native backend first:  ./scripts/build.ps1"
    )
for module in native_modules:
    datas.append((str(module), "."))

# platform-tools, when the repository has them vendored. Absent is fine: the
# toolchain check reports where adb and fastboot were found at run time.
platform_tools = REPO_ROOT / "third_party" / "platform-tools"
if platform_tools.is_dir():
    datas.append((str(platform_tools), "platform-tools"))

# The driver instructions and helper script. The drivers themselves cannot be
# bundled (see the note at the top), but the guidance can - and it is what the
# operator needs at the moment a device shows a warning triangle.
drivers = REPO_ROOT / "drivers"
if drivers.is_dir():
    datas.append((str(drivers), "drivers"))

# The protocol documentation. Packaged because the honest account of what is and
# is not verified belongs with the build that has those limitations, not only in
# the source repository.
docs = REPO_ROOT / "docs"
if docs.is_dir():
    datas.append((str(docs), "docs"))

# The stylesheet template. A data file, so PyInstaller's import analysis never
# sees it - and without it the packaged application raises on its first line of
# styling and dies before it can write a log. The destination preserves the
# package layout, because the loader resolves it relative to its own module.
ui_styles = PYTHON_ROOT / "huaxin" / "ui" / "styles"
if ui_styles.is_dir():
    datas.append((str(ui_styles), "huaxin/ui/styles"))
else:
    raise SystemExit(
        "the stylesheet template is missing from " + str(ui_styles) + ".\n"
        "The packaged application cannot start without it."
    )

# The deployment settings template. Copying it next to the executable as
# settings.template.json is how an organisation ships the same timeouts and log
# level to every workstation; see huaxin/core/config.py.
template = REPO_ROOT / "packaging" / "settings.template.json"
if template.is_file():
    datas.append((str(template), "."))

# Optional example packages: a Firehose rawprogram pair, a scatter file, a PAC
# listing. Present only when the repository has them, and none of them is
# required to run - they are something to point the tool at while learning it.
samples = REPO_ROOT / "examples"
if samples.is_dir():
    datas.append((str(samples), "examples"))

for misc in ("README.md", "LICENSE"):
    candidate = REPO_ROOT / misc
    if candidate.is_file():
        datas.append((str(candidate), "."))

hidden = [
    # Imported lazily inside functions, so static analysis misses them.
    "huaxin_core",
    "huaxin.core.backend",
    "huaxin.core.config",
    "huaxin.core.devices",
    "huaxin.core.errors",
    "huaxin.core.qualcomm",
    "huaxin.core.mediatek",
    "huaxin.core.samsung",
    "huaxin.core.unisoc",
    "huaxin.core.adb_fastboot_wrapper",
]
hidden += collect_submodules("huaxin.ui")

# Qt modules the app never touches. Dropping them takes a meaningful bite out of
# the bundle; these are the safe ones, not a maximal list.
excludes = [
    "PyQt6.QtWebEngineCore",
    "PyQt6.QtWebEngineWidgets",
    "PyQt6.QtQuick",
    "PyQt6.QtQml",
    "PyQt6.Qt3DCore",
    "PyQt6.QtBluetooth",
    "PyQt6.QtMultimedia",
    "PyQt6.QtNetworkAuth",
    "PyQt6.QtPdf",
    "PyQt6.QtCharts",
    "PyQt6.QtDataVisualization",
    # Not used, and unittest drags in a surprising amount.
    "tkinter",
    "unittest",
    "pydoc_data",
    "pytest",
]

a = Analysis(
    [str(PYTHON_ROOT / "main.py")],
    pathex=[str(PYTHON_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HuaxinTool",
    debug=False,
    strip=False,
    upx=False,  # UPX and Qt do not always agree; size is not the constraint here
    console=False,  # a GUI tool: no console window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="HuaxinTool",
)
