<#
.SYNOPSIS
    Configures and builds the Huaxin Tool native backend (huaxin_core).

.EXAMPLE
    .\scripts\build.ps1
    .\scripts\build.ps1 -BuildType Debug
    .\scripts\build.ps1 -Clean -FetchDeps
#>
[CmdletBinding()]
param(
    [ValidateSet('Debug', 'Release', 'RelWithDebInfo')]
    [string]$BuildType = 'Release',

    [string]$PythonExe = 'python',

    # Delete the build directory before configuring.
    [switch]$Clean,

    # Ignore a pip-installed pybind11 and let CMake FetchContent download it (requires git).
    [switch]$FetchDeps
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$buildDir = Join-Path $repoRoot 'build'

if ($Clean -and (Test-Path $buildDir)) {
    Write-Host "Removing $buildDir" -ForegroundColor Yellow
    Remove-Item -Recurse -Force $buildDir
}

$cmakeArgs = @('-S', $repoRoot, '-B', $buildDir, "-DPython3_EXECUTABLE=$PythonExe")

if (-not $FetchDeps) {
    # Prefer the pybind11 that pip installed for this exact interpreter, so the
    # extension is built against the Python that will import it.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $probe = & $PythonExe -c "import pybind11, pathlib; print(pybind11.get_cmake_dir())" 2>$null
    $probeOk = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $previous

    if ($probeOk -and $probe) {
        $pybind11Dir = ($probe | Select-Object -Last 1).ToString().Trim()
        Write-Host "pybind11 cmake dir: $pybind11Dir"
        $cmakeArgs += "-Dpybind11_DIR=$pybind11Dir"
    }
    else {
        Write-Warning "'$PythonExe' has no pybind11 installed; falling back to FetchContent (needs git)."
        Write-Warning "Install it with: $PythonExe -m pip install pybind11"
    }
}

Write-Host "`n== Configuring ==" -ForegroundColor Cyan
& cmake @cmakeArgs
if ($LASTEXITCODE -ne 0) { throw 'CMake configure failed.' }

Write-Host "`n== Building ($BuildType) ==" -ForegroundColor Cyan
& cmake --build $buildDir --config $BuildType --parallel
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }

Write-Host "`n== Done ==" -ForegroundColor Green
Write-Host "Verify the Python bridge with:  $PythonExe python/tools/test_bridge.py"
