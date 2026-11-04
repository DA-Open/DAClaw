param(
    [string]$Version = "0.1.0",
    [string]$SourceDir = "",
    [string]$BuildRoot = "",
    [switch]$SkipWebBuild,
    [switch]$NoCompile,
    [switch]$IncludeVolcengineArkDefaults,
    [string]$VolcengineArkApiKey = $env:DACLAW_DEFAULT_ARK_API_KEY,
    [string]$VolcengineArkBaseUrl = "https://ark.cn-beijing.volces.com/api/v3",
    [string]$VolcengineArkEndpointId = $env:DACLAW_DEFAULT_ARK_ENDPOINT_ID,
    [string]$VolcengineArkModel = $(if ($env:DACLAW_DEFAULT_ARK_MODEL) { $env:DACLAW_DEFAULT_ARK_MODEL } else { $env:DACLAW_DEFAULT_ARK_ENDPOINT_ID })
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($SourceDir)) {
    $SourceDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
} else {
    $SourceDir = (Resolve-Path $SourceDir).Path
}

if ([string]::IsNullOrWhiteSpace($BuildRoot)) {
    $BuildRoot = Join-Path $PSScriptRoot "build"
}

$BuildRoot = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($BuildRoot)
$PayloadDir = Join-Path $BuildRoot "payload"
$DistDir = Join-Path $PSScriptRoot "dist"

function Ensure-Command {
    param([string]$Name, [string]$InstallHint)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw "$Name was not found. $InstallHint"
    }
    return $cmd.Source
}

function Invoke-Robocopy {
    param(
        [string]$From,
        [string]$To,
        [switch]$Mirror
    )

    $mode = if ($Mirror) { "/MIR" } else { "/E" }
    $robocopyArgs = @(
        $From,
        $To,
        $mode,
        "/NFL",
        "/NDL",
        "/NJH",
        "/NJS",
        "/NP"
    )

    $excludeDirs = @(
        ".git",
        ".pytest_cache",
        ".vscode",
        "__pycache__",
        "node_modules",
        "venv",
        ".venv",
        "dist",
        "build",
        "daclaw_agent.egg-info"
    )

    $excludeFiles = @(
        "*.pyc",
        "*.pyo",
        ".env",
        ".env.*",
        "cli-config.yaml"
    )

    $robocopyArgs += @("/XD") + $excludeDirs + @("/XF") + $excludeFiles

    & robocopy @robocopyArgs | Out-Null
    $code = $LASTEXITCODE
    if ($code -gt 7) {
        throw "robocopy failed with exit code $code"
    }
}

function Copy-Directory {
    param([string]$Name)
    $from = Join-Path $SourceDir $Name
    if (Test-Path $from) {
        $to = Join-Path $PayloadDir $Name
        New-Item -ItemType Directory -Force -Path (Split-Path $to -Parent) | Out-Null
        Invoke-Robocopy -From $from -To $to
    }
}

function Copy-File {
    param([string]$Name)
    $from = Join-Path $SourceDir $Name
    if (Test-Path $from) {
        $to = Join-Path $PayloadDir $Name
        New-Item -ItemType Directory -Force -Path (Split-Path $to -Parent) | Out-Null
        Copy-Item -LiteralPath $from -Destination $to -Force
    }
}

Write-Host "Building DAClaw Windows installer payload"
Write-Host "Version: $Version"
Write-Host "Source:  $SourceDir"
Write-Host "Payload: $PayloadDir"

New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null

if (-not $SkipWebBuild) {
    $npm = Ensure-Command "npm" "Install Node.js 20 LTS from https://nodejs.org/."
    Write-Host "Building Web UI before packaging..."
    Push-Location (Join-Path $SourceDir "web")
    try {
        & $npm install
        & $npm run build
    } finally {
        Pop-Location
    }
}

$WebDistIndex = Join-Path $SourceDir "daclaw_cli\web_dist\index.html"
if (-not (Test-Path $WebDistIndex)) {
    throw "Built Web UI was not found at $WebDistIndex. Run without -SkipWebBuild or build web/ first."
}

if (Test-Path $PayloadDir) {
    $empty = Join-Path $BuildRoot "empty"
    New-Item -ItemType Directory -Force -Path $empty | Out-Null
    Invoke-Robocopy -From $empty -To $PayloadDir -Mirror
    Remove-Item -LiteralPath $empty -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PayloadDir -Recurse -Force -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Force -Path $PayloadDir | Out-Null

$runtimeDirs = @(
    "acp_adapter",
    "acp_registry",
    "agent",
    "assets",
    "cron",
    "daclaw_cli",
    "gateway",
    "hermes_cli",
    "packaging\common",
    "packaging\defaults",
    "packaging\windows",
    "plugins",
    "skills",
    "tools",
    "web"
)

$runtimeFiles = @(
    ".env.example",
    "LICENSE",
    "MANIFEST.in",
    "README.md",
    "batch_runner.py",
    "cli.py",
    "daclaw",
    "daclaw_constants.py",
    "daclaw_logging.py",
    "daclaw_state.py",
    "daclaw_time.py",
    "mcp_serve.py",
    "model_tools.py",
    "pyproject.toml",
    "requirements.txt",
    "rl_cli.py",
    "run_agent.py",
    "toolset_distributions.py",
    "toolsets.py",
    "trajectory_compressor.py",
    "utils.py"
)

foreach ($dir in $runtimeDirs) {
    Copy-Directory -Name $dir
}
foreach ($file in $runtimeFiles) {
    Copy-File -Name $file
}

$payloadBuildDir = Join-Path $PayloadDir "packaging\windows\build"
$payloadDistDir = Join-Path $PayloadDir "packaging\windows\dist"
Remove-Item -LiteralPath $payloadBuildDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $payloadDistDir -Recurse -Force -ErrorAction SilentlyContinue

if ($IncludeVolcengineArkDefaults) {
    if ([string]::IsNullOrWhiteSpace($VolcengineArkApiKey)) {
        throw "Set DACLAW_DEFAULT_ARK_API_KEY or pass -VolcengineArkApiKey when using -IncludeVolcengineArkDefaults."
    }
    if ([string]::IsNullOrWhiteSpace($VolcengineArkModel)) {
        throw "Set DACLAW_DEFAULT_ARK_MODEL or pass -VolcengineArkModel when using -IncludeVolcengineArkDefaults. Use an Ark model value your account can access, e.g. doubao-seed-2-0-pro-260215 or an ep-... endpoint ID."
    }
    $defaultsDir = Join-Path $PayloadDir "packaging\defaults"
    New-Item -ItemType Directory -Force -Path $defaultsDir | Out-Null
    @"
model:
  provider: custom:volcengine-ark
  default: $VolcengineArkModel
  base_url: $VolcengineArkBaseUrl

custom_providers:
  - name: volcengine-ark
    base_url: $VolcengineArkBaseUrl
    key_env: ARK_API_KEY
    api_mode: chat_completions
    model: $VolcengineArkModel
"@ | Set-Content -Path (Join-Path $defaultsDir "config.yaml") -Encoding UTF8
    "ARK_API_KEY=$VolcengineArkApiKey" | Set-Content -Path (Join-Path $defaultsDir ".env") -Encoding UTF8
    Write-Host "Included private Volcengine Ark defaults in payload."
}

$PayloadWebDistIndex = Join-Path $PayloadDir "daclaw_cli\web_dist\index.html"
if (-not (Test-Path $PayloadWebDistIndex)) {
    throw "Payload is missing daclaw_cli\web_dist\index.html."
}

Write-Host "Payload ready: $PayloadDir"

if ($NoCompile) {
    Write-Host "Skipping Inno Setup compile because -NoCompile was provided."
    exit 0
}

$isccCommand = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
$isccPath = if ($isccCommand) { $isccCommand.Source } else { "" }
if ([string]::IsNullOrWhiteSpace($isccPath)) {
    $programFilesX86 = [Environment]::GetFolderPath("ProgramFilesX86")
    $candidate = Join-Path $programFilesX86 "Inno Setup 6\ISCC.exe"
    if (Test-Path $candidate) {
        $isccPath = $candidate
    }
}
if ([string]::IsNullOrWhiteSpace($isccPath)) {
    throw "ISCC.exe was not found. Install Inno Setup 6, or re-run with -NoCompile to only build the payload."
}

$iss = Join-Path $PSScriptRoot "DAClaw.iss"
Write-Host "Compiling installer with Inno Setup..."
& $isccPath "/DAppVersion=$Version" "/DSourceDir=$PayloadDir" "/DOutputDir=$DistDir" $iss

Write-Host "Installer output:"
Get-ChildItem -Path $DistDir -Filter "DAClaw-Setup-$Version.exe" | ForEach-Object {
    Write-Host "  $($_.FullName)"
}
