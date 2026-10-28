<#
.SYNOPSIS
  Builds AICorp.exe (the Windows desktop app) and installs it for the current user.
.DESCRIPTION
  Needs Python 3.10+ for Windows (python.org) and an internet connection for the build tools. Copies the few source
  files to a temporary folder (so it also works when the repository lives inside WSL), builds one .exe with PyInstaller
  and copies it to -Out. The .exe is not code-signed: Windows SmartScreen may ask once ("More info" -> "Run anyway").
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File build.ps1 -Shortcut
#>
param(
    [string]$Out = (Join-Path $env:LOCALAPPDATA "AICorp"),
    [switch]$Shortcut
)
$ErrorActionPreference = "Stop"
$desktop = Split-Path -Parent $MyInvocation.MyCommand.Path
$project = Split-Path -Parent $desktop
$work = Join-Path $env:TEMP "aicorp-desktop-build"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Python for Windows was not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again." -ForegroundColor Yellow
    exit 1
}
if (Test-Path $work) { Remove-Item -Recurse -Force $work }
New-Item -ItemType Directory -Force (Join-Path $work "pkg\aicorp") | Out-Null
Copy-Item (Join-Path $desktop "*.py") $work
Copy-Item (Join-Path $desktop "requirements.txt") $work
Copy-Item (Join-Path $project "src\aicorp\__init__.py") (Join-Path $work "pkg\aicorp")
Copy-Item (Join-Path $project "src\aicorp\doctor.py") (Join-Path $work "pkg\aicorp")

Push-Location $work
try {
    Write-Host "Preparing build tools..."
    & python -m venv venv
    & .\venv\Scripts\python -m pip install --quiet --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Installing the build tools failed." }
    Write-Host "Building AICorp.exe..."
    & .\venv\Scripts\pyinstaller --noconfirm --onefile --windowed --name AICorp --paths $work --paths (Join-Path $work "pkg") shell.py
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path "dist\AICorp.exe")) { throw "The build failed." }
    New-Item -ItemType Directory -Force $Out | Out-Null
    Copy-Item "dist\AICorp.exe" (Join-Path $Out "AICorp.exe") -Force
} finally {
    Pop-Location
}
$exe = Join-Path $Out "AICorp.exe"
if ($Shortcut) {
    $link = Join-Path ([Environment]::GetFolderPath("Desktop")) "AICorp.lnk"
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut($link)
    $s.TargetPath = $exe
    $s.WorkingDirectory = $Out
    $s.Save()
    Write-Host "Desktop shortcut created."
}
Write-Host "Done: $exe"
