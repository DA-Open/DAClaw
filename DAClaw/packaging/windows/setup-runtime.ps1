param(
    [string]$AppDir = "",
    [int]$Port = 9119,
    [string]$PythonExe = "",
    [switch]$SkipWebBuild,
    [switch]$NoShortcut
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($AppDir)) {
    $AppDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
} else {
    $AppDir = (Resolve-Path $AppDir).Path
}

function Find-Python {
    if (-not [string]::IsNullOrWhiteSpace($PythonExe)) {
        if (Test-Path $PythonExe) {
            return (Resolve-Path $PythonExe).Path
        }
        throw "Python executable not found: $PythonExe"
    }

    $candidates = @(
        @{ File = "py"; Args = @("-3.11", "-c", "import sys; print(sys.executable)") },
        @{ File = "py"; Args = @("-3", "-c", "import sys; print(sys.executable)") },
        @{ File = "python"; Args = @("-c", "import sys; print(sys.executable)") }
    )

    foreach ($candidate in $candidates) {
        try {
            $output = & $candidate.File @($candidate.Args) 2>$null
            if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($output)) {
                return ($output | Select-Object -First 1).Trim()
            }
        } catch {
        }
    }

    throw "Python 3.11+ was not found. Install Python from https://www.python.org/downloads/ and re-run this script."
}

function Ensure-Command {
    param([string]$Name, [string]$InstallHint)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw "$Name was not found. $InstallHint"
    }
    return $cmd.Source
}

function New-Shortcut {
    param(
        [string]$Path,
        [string]$TargetPath,
        [string]$Arguments,
        [string]$WorkingDirectory,
        [string]$IconLocation
    )

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($Path)
    $shortcut.TargetPath = $TargetPath
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = $WorkingDirectory
    if (Test-Path $IconLocation) {
        $shortcut.IconLocation = $IconLocation
    }
    $shortcut.Save()
}

Set-Location $AppDir

Write-Host "DAClaw runtime setup"
Write-Host "App directory: $AppDir"

$BasePython = Find-Python
$VenvDir = Join-Path $AppDir "venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    $VenvPython = Join-Path $VenvDir "bin\python"
}

if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating Python virtual environment..."
    & $BasePython -m venv $VenvDir
    $VenvPython = Join-Path $VenvDir "Scripts\python.exe"
    if (-not (Test-Path $VenvPython)) {
        $VenvPython = Join-Path $VenvDir "bin\python"
    }
}

Write-Host "Installing Python dependencies..."
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e ".[web]"

$DefaultSettings = Join-Path $AppDir "packaging\common\apply-default-settings.py"
if (Test-Path $DefaultSettings) {
    Write-Host "Applying DAClaw installer defaults..."
    & $VenvPython $DefaultSettings
}

$WebDir = Join-Path $AppDir "web"
$WebDistIndex = Join-Path $AppDir "daclaw_cli\web_dist\index.html"
if ((Test-Path (Join-Path $WebDir "package.json")) -and -not $SkipWebBuild) {
    $npm = Ensure-Command "npm" "Install Node.js 20 LTS from https://nodejs.org/ and re-run this script."
    Write-Host "Building DAClaw Web UI..."
    Push-Location $WebDir
    try {
        & $npm install
        & $npm run build
    } finally {
        Pop-Location
    }
} elseif (-not (Test-Path $WebDistIndex)) {
    throw "Built Web UI was not found at $WebDistIndex. Re-run without -SkipWebBuild after installing Node.js."
}

if (-not $NoShortcut) {
    $PowerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $Launcher = Join-Path $AppDir "packaging\windows\launch-daclaw.ps1"
    $Icon = Join-Path $AppDir "web\public\favicon.ico"
    $LauncherArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$Launcher`" -AppDir `"$AppDir`" -Port $Port"

    $Desktop = [Environment]::GetFolderPath("Desktop")
    $DesktopShortcut = Join-Path $Desktop "DAClaw.lnk"
    New-Shortcut -Path $DesktopShortcut -TargetPath $PowerShellExe -Arguments $LauncherArgs -WorkingDirectory $AppDir -IconLocation $Icon

    $StartMenuDir = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\DAClaw"
    New-Item -ItemType Directory -Force -Path $StartMenuDir | Out-Null
    $StartMenuShortcut = Join-Path $StartMenuDir "DAClaw.lnk"
    New-Shortcut -Path $StartMenuShortcut -TargetPath $PowerShellExe -Arguments $LauncherArgs -WorkingDirectory $AppDir -IconLocation $Icon

    Write-Host "Created shortcut: $DesktopShortcut"
    Write-Host "Created shortcut: $StartMenuShortcut"
}

Write-Host "DAClaw setup complete."
Write-Host "Launch URL: http://127.0.0.1:$Port/"
