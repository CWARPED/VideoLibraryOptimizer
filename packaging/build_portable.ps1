<#
.SYNOPSIS
  Build a portable, zero-install Windows distribution of VideoLibraryOptimizer.

.DESCRIPTION
  Produces a folder containing an embeddable CPython runtime with the app's
  dependencies pre-installed, the app source (backend/ + frontend/), and a
  double-clickable launcher. ffmpeg is NOT bundled -- the launcher downloads it
  on first run (keeps the package light).

.EXAMPLE
  pwsh packaging/build_portable.ps1 -Zip
#>
[CmdletBinding()]
param(
    [string]$PyVersion = "3.12.8",
    [string]$OutDir = "dist/VideoLibraryOptimizer-portable",
    [switch]$Zip
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$out = Join-Path $repo $OutDir
$runtime = Join-Path $out "runtime"
$xy = ($PyVersion -split '\.')[0..1] -join ''   # "3.12.8" -> "312"

Write-Host "[build] Output: $out"
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory -Force -Path $runtime | Out-Null

# 1. Embeddable Python -------------------------------------------------------
$embedUrl = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip"
$embedZip = Join-Path $env:TEMP "py-embed-$PyVersion.zip"
Write-Host "[build] Downloading embeddable Python $PyVersion..."
Invoke-WebRequest -Uri $embedUrl -OutFile $embedZip
Expand-Archive -Path $embedZip -DestinationPath $runtime -Force

# 2. Enable site-packages in the ._pth ---------------------------------------
$pth = Join-Path $runtime "python$xy._pth"
if (-not (Test-Path $pth)) { throw "._pth introuvable: $pth" }
$lines = Get-Content $pth | ForEach-Object { $_ -replace '^\s*#\s*import site', 'import site' }
if ($lines -notcontains 'Lib\site-packages') { $lines += 'Lib\site-packages' }
# The embeddable Python IGNORES PYTHONPATH when a ._pth exists, so the app
# package dir must be listed here (relative to runtime\python.exe -> ..\backend).
if ($lines -notcontains '..\backend') { $lines += '..\backend' }
$lines | Set-Content $pth

# 3. Bootstrap pip -----------------------------------------------------------
$py = Join-Path $runtime "python.exe"
$getpip = Join-Path $env:TEMP "get-pip.py"
Write-Host "[build] Bootstrapping pip..."
Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getpip
& $py $getpip --no-warn-script-location

# 4. Install runtime dependencies (matching pyproject [project].dependencies) -
Write-Host "[build] Installing dependencies into the embeddable runtime..."
& $py -m pip install --no-warn-script-location `
    "fastapi>=0.110" "uvicorn[standard]>=0.27" "pydantic>=2.6" "pydantic-settings>=2.2" "psutil>=5.9"

# 5. Copy app source + launcher ----------------------------------------------
Write-Host "[build] Copying application files..."
Copy-Item -Recurse -Force (Join-Path $repo "backend")  (Join-Path $out "backend")
Copy-Item -Recurse -Force (Join-Path $repo "frontend") (Join-Path $out "frontend")
Copy-Item -Recurse -Force (Join-Path $repo "tools")    (Join-Path $out "tools")
Copy-Item -Force (Join-Path $repo "README.md")         (Join-Path $out "README.md")
Copy-Item -Force (Join-Path $repo "LICENSE")           (Join-Path $out "LICENSE")
Copy-Item -Force (Join-Path $PSScriptRoot "start-portable.bat") (Join-Path $out "start.bat")

# Drop __pycache__ to keep the package clean.
Get-ChildItem -Recurse -Directory -Filter "__pycache__" $out | Remove-Item -Recurse -Force

# 6. Optional zip ------------------------------------------------------------
if ($Zip) {
    $zipPath = "$out.zip"
    if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
    Write-Host "[build] Zipping -> $zipPath"
    Compress-Archive -Path "$out\*" -DestinationPath $zipPath
}

Write-Host "[build] Done. Test: double-click $out\start.bat"
