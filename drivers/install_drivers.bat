@echo off
REM ===========================================================================
REM  Huaxin Tool - USB driver helper
REM
REM  This script does not bundle any driver. It checks what is present, tells
REM  you what is missing, and opens the tools that do the binding. That is
REM  deliberate: the vendor drivers are proprietary and belong to their owners,
REM  and a script that silently installed four drivers would bind WinUSB to
REM  things that should not have it.
REM
REM  Every step asks first. Nothing here is destructive on its own.
REM ===========================================================================
setlocal EnableDelayedExpansion
title Huaxin Tool - USB drivers

set "ROOT=%~dp0"
set "ZADIG=%ROOT%zadig.exe"
set "FAILED=0"

cls
echo.
echo  ==========================================================
echo    HUAXIN TOOL - USB DRIVER HELPER
echo  ==========================================================
echo.
echo   A device with a warning triangle in Device Manager will
echo   never be flashed. This walks through each vendor.
echo.
echo   Nothing is installed without asking. If you are not sure,
echo   answer N and read drivers\README.md first.
echo.

REM -- Administrator check ---------------------------------------------------
REM  Binding a driver needs elevation. Rather than failing halfway through,
REM  check now and offer to relaunch.
net session >nul 2>&1
if errorlevel 1 (
    echo  This script needs administrator rights to bind drivers.
    echo.
    choice /c YN /n /m "  Relaunch elevated now? [Y/N] "
    if errorlevel 2 goto :skip_binding
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
    exit /b 0
)
set "ELEVATED=1"

:skip_binding
echo.
echo  ------------------------------------------------------------------
echo   1. Check what is bound right now
echo  ------------------------------------------------------------------
echo.
echo   Opening Device Manager. Put the device into its flash mode and
echo   look under "Ports (COM ^& LPT)" or "Other devices".
echo.
echo   A warning triangle means the driver needs binding.
echo   Nothing at all means the mode or the cable - see drivers\README.md.
echo.
start "" devmgmt.msc
echo.
pause

REM -- Per-vendor -------------------------------------------------------------
call :offer_vendor "Qualcomm EDL (9008)" "05C6" "qualcomm" ^
    "QDLoader / HS-USB 9008" "qualcomm\Qualcomm_USB_Driver\setup.exe" ^
    "Put the phone into EDL: power off, then hold Vol+ and Vol- while connecting."
call :offer_vendor "MediaTek (BROM / Preloader)" "0E8D" "mediatek" ^
    "MediaTek USB VCOM" "mediatek\InstallDriver.exe" ^
    "Power the device off completely; it enters BROM as it connects."
call :offer_vendor "Unisoc / Spreadtrum" "1782" "unisoc" ^
    "Spreadtrum USB driver" "unisoc\Spreadtrum_USB_Driver\setup.exe" ^
    "Power off, then hold the board's key combination while connecting."
call :offer_vendor "Samsung (Download mode)" "04E8" "samsung" ^
    "Samsung Android USB driver" "samsung\SAMSUNG_USB_Driver_for_Mobile_Phones.exe" ^
    "Power off, then hold Vol- and Vol+ (or Bixby) and connect."

REM -- Zadig fallback ---------------------------------------------------------
echo.
echo  ------------------------------------------------------------------
echo   5. Zadig - the fallback for any of the above
echo  ------------------------------------------------------------------
echo.
if exist "%ZADIG%" (
    echo   zadig.exe found.
    echo.
    echo   IMPORTANT, before you press Replace Driver:
    echo     * Options -^> List All Devices, or your device is hidden.
    echo     * Check the USB ID in the dropdown matches the vendor table
    echo       in drivers\README.md.
    echo     * Choose WinUSB as the target driver.
    echo.
    echo   Binding the wrong device - a keyboard, a mouse - stops that
    echo   device working until you uninstall the driver.
    echo.
    choice /c YN /n /m "  Open Zadig now? [Y/N] "
    if errorlevel 2 goto :zadig_done
    start "" "%ZADIG%"
) else (
    echo   zadig.exe is not in this folder.
    echo.
    echo   Download it from https://zadig.akeo.ie/ and put it here, or use
    echo   the vendor installer above. It is not bundled with this tool
    echo   because it is a third-party program with its own licence.
    echo.
    set "FAILED=1"
)
:zadig_done

REM -- Summary ----------------------------------------------------------------
echo.
echo  ==========================================================
if "%FAILED%"=="1" (
    echo    SOME STEPS WERE SKIPPED
    echo.
    echo   A step was skipped because its installer was not found.
    echo   drivers\README.md says where to get each one.
) else (
    echo    DONE
)
echo  ==========================================================
echo.
echo   After binding, unplug and replug the device, then run a
echo   scan in Huaxin Tool. If the device now appears under
echo   "Ports (COM ^& LPT)" with no warning icon, it is ready.
echo.
echo   To undo a binding: Device Manager, right-click the device,
echo   Uninstall device, tick "Delete the driver software", replug.
echo.
pause
exit /b 0

REM ===========================================================================
REM  :offer_vendor  - check, report, and optionally run one vendor installer
REM
REM  %1 label        %2 vendor id    %3 folder
REM  %4 driver name  %5 installer    %6 how to enter the mode
REM ===========================================================================
:offer_vendor
set "LABEL=%~1"
set "VID=%~2"
set "FOLDER=%~3"
set "DRIVER=%~4"
set "INSTALLER=%ROOT%%~5"
set "HOWTO=%~6"

echo.
echo  ------------------------------------------------------------------
echo   %LABEL%   [USB %VID%]
echo  ------------------------------------------------------------------
echo.
echo   Needs:  %DRIVER%
echo   How:    %HOWTO%
echo.

REM  Report what Windows currently sees for this vendor ID. This is the part
REM  that makes the script worth running even when nothing needs installing.
set "SEEN=0"
for /f "tokens=*" %%L in ('powershell -NoProfile -Command ^
    "Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object { $_.InstanceId -match 'VID_%VID%' } | Select-Object -First 3 -ExpandProperty FriendlyName" 2^>nul') do (
    echo   Present: %%L
    set "SEEN=1"
)
if "!SEEN!"=="0" echo   Present: nothing with USB ID %VID% right now.

if exist "%INSTALLER%" (
    echo.
    echo   Installer found:
    echo     %INSTALLER%
    echo.
    choice /c YN /n /m "  Run it now? [Y/N] "
    if errorlevel 2 (
        echo   Skipped.
    ) else (
        echo   Starting the driver installer...
        start "" /wait "%INSTALLER%"
        echo   Installer finished. Check Device Manager for a warning icon.
    )
) else (
    echo.
    echo   No installer found at:
    echo     %INSTALLER%
    echo.
    echo   Put the vendor's installer there, or bind WinUSB with Zadig
    echo   (step 5 below). Nothing was changed.
    set "FAILED=1"
)
exit /b 0
