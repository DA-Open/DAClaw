param(
    [string]$SourceDir = "",
    [string]$InstallDir = "",
    [int]$Port = 9119,
    [switch]$SkipWebBuild,
    [switch]$NoShortcut
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($SourceDir)) {
    $SourceDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
} else {
    $SourceDir = (Resolve-Path $SourceDir).Path
}

if ([string]::IsNullOrWhiteSpace($InstallDir)) {
    $InstallDir = Join-Path $env:LOCALAPPDATA "DAClaw\app"
}

$InstallDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($InstallDir)
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

Write-Host "Installing DAClaw"
Write-Host "Source: $SourceDir"
Write-Host "Target: $InstallDir"

$excludeDirs = @(
    ".git",
    ".pytest_cache",
    ".vscode",
    "__pycache__",
    "node_modules",
    "venv",
    "dist",
    "daclaw_agent.egg-info"
)

$excludeFiles = @(
    "*.pyc",
    "*.pyo",
    ".env"
)

$robocopyArgs = @(
    $SourceDir,
    $InstallDir,
    "/E",
    "/NFL",
    "/NDL",
    "/NJH",
    "/NJS",
    "/NP",
    "/XD"
) + $excludeDirs + @("/XF") + $excludeFiles

& robocopy @robocopyArgs | Out-Null
$code = $LASTEXITCODE
if ($code -gt 7) {
    throw "robocopy failed with exit code $code"
}

$setup = Join-Path $InstallDir "packaging\windows\setup-runtime.ps1"
$setupArgs = @("-AppDir", $InstallDir, "-Port", [string]$Port)
if ($SkipWebBuild) {
    $setupArgs += "-SkipWebBuild"
}
if ($NoShortcut) {
    $setupArgs += "-NoShortcut"
}

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $setup @setupArgs

Write-Host ""
Write-Host "Installed DAClaw to $InstallDir"
Write-Host "Run the desktop shortcut, or launch manually:"
Write-Host "powershell -NoProfile -ExecutionPolicy Bypass -File `"$InstallDir\packaging\windows\launch-daclaw.ps1`" -AppDir `"$InstallDir`" -Port $Port"
