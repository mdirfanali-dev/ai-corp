<#
.SYNOPSIS
  Builds AICorp.exe and then the installer (AICorp-Setup-<version>.exe).
.DESCRIPTION
  Needs Python for Windows and Inno Setup 6 (free: https://jrsoftware.org/isinfo.php; ISCC.exe on the PATH or in the default folders).
  The result is written to -Out (default: dist next to this script's parent). Unsigned until the project's SignPath setup is done.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File build-installer.ps1 -Version 0.1.0
#>
param(
    [string]$Version = "0.1.0",
    [string]$Out = (Join-Path $env:TEMP "aicorp-installer-out"),
    [string]$Iscc = ""
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = Split-Path -Parent $here

if (-not $Iscc) {
    $found = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    $candidates = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe")
    if ($found) { $Iscc = $found.Source } else { $Iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1 }
}
if (-not $Iscc) { Write-Host "Inno Setup 6 was not found. Install it from https://jrsoftware.org/isinfo.php and run this again." -ForegroundColor Yellow; exit 1 }

New-Item -ItemType Directory -Force $Out | Out-Null
& (Join-Path $desktop "build.ps1") -Out $Out
if ($LASTEXITCODE -ne 0) { throw "Building AICorp.exe failed." }

$work = Join-Path $env:TEMP "aicorp-installer-build"
if (Test-Path $work) { Remove-Item -Recurse -Force $work }
New-Item -ItemType Directory -Force (Join-Path $work "desktop\installer"), (Join-Path $work "desktop\assets"), (Join-Path $work "dist") | Out-Null
Copy-Item (Join-Path $here "aicorp.iss") (Join-Path $work "desktop\installer")
Copy-Item (Join-Path $desktop "assets\aicorp.ico") (Join-Path $work "desktop\assets")
Copy-Item (Join-Path $desktop "..\LICENSE") $work
Copy-Item (Join-Path $desktop "..\NOTICE") $work
Copy-Item (Join-Path $Out "AICorp.exe") (Join-Path $work "dist")

& $Iscc "/DAppVersion=$Version" "/DExePath=$(Join-Path $work 'dist\AICorp.exe')" "/O$Out" (Join-Path $work "desktop\installer\aicorp.iss")
if ($LASTEXITCODE -ne 0) { throw "Building the installer failed." }
Write-Host "Done: $(Join-Path $Out "AICorp-Setup-$Version.exe")"
