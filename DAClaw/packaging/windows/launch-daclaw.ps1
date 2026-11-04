param(
    [string]$AppDir = "",
    [int]$Port = 9119,
    [string]$HostAddress = "127.0.0.1",
    [int]$PortSearchCount = 20
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($AppDir)) {
    $AppDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
} else {
    $AppDir = (Resolve-Path $AppDir).Path
}

$LogDir = Join-Path $env:LOCALAPPDATA "DAClaw\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir "dashboard-launch.log"
$StdoutLog = Join-Path $LogDir "dashboard-stdout.log"
$StderrLog = Join-Path $LogDir "dashboard-stderr.log"

function Write-LaunchLog {
    param([string]$Message)
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $LogFile -Value "[$stamp] $Message"
}

function Test-Dashboard {
    param([int]$TestPort)
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://$HostAddress`:$TestPort/api/status" -TimeoutSec 2
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500)
    } catch {
        return $false
    }
}

function Test-PortAvailable {
    param([int]$TestPort)
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse($HostAddress), $TestPort)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener) {
            $listener.Stop()
        }
    }
}

function Wait-Dashboard {
    param([int]$TestPort, [int]$TimeoutSeconds = 45)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Dashboard -TestPort $TestPort) {
            return $true
        }
        Start-Sleep -Milliseconds 750
    }
    return $false
}

try {
    Set-Location $AppDir
    Write-LaunchLog "Launching DAClaw from $AppDir"

    for ($offset = 0; $offset -lt $PortSearchCount; $offset++) {
        $candidate = $Port + $offset
        if (Test-Dashboard -TestPort $candidate) {
            Write-LaunchLog "Dashboard already running on port $candidate"
            Start-Process "http://$HostAddress`:$candidate/"
            exit 0
        }
    }

    $SelectedPort = $null
    for ($offset = 0; $offset -lt $PortSearchCount; $offset++) {
        $candidate = $Port + $offset
        if (Test-PortAvailable -TestPort $candidate) {
            $SelectedPort = $candidate
            break
        }
    }

    if (-not $SelectedPort) {
        throw "No free dashboard port found from $Port to $($Port + $PortSearchCount - 1)."
    }

    $PythonExe = Join-Path $AppDir "venv\Scripts\python.exe"
    if (-not (Test-Path $PythonExe)) {
        $PythonExe = Join-Path $AppDir "venv\bin\python"
    }
    if (-not (Test-Path $PythonExe)) {
        throw "Python virtual environment not found at $PythonExe. Run packaging\windows\setup-runtime.ps1 first."
    }

    $Arguments = @(
        "-m", "daclaw_cli.main",
        "dashboard",
        "--host", $HostAddress,
        "--port", [string]$SelectedPort,
        "--no-open"
    )

    Write-LaunchLog "Starting dashboard on http://$HostAddress`:$SelectedPort/"
    Start-Process -FilePath $PythonExe `
        -ArgumentList $Arguments `
        -WorkingDirectory $AppDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $StdoutLog `
        -RedirectStandardError $StderrLog

    if (Wait-Dashboard -TestPort $SelectedPort) {
        Start-Process "http://$HostAddress`:$SelectedPort/"
        exit 0
    }

    throw "Dashboard did not become ready on port $SelectedPort. See $LogDir."
} catch {
    Write-LaunchLog "ERROR: $($_.Exception.Message)"
    if (Test-Path $LogFile) {
        Start-Process notepad.exe $LogFile
    }
    throw
}
