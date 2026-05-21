# DAClaw Windows MVP Installer

This folder contains the first local-installation loop for non-technical Windows users.

It is intentionally simple:

1. Copy the current DAClaw source tree into a user-writable install directory.
2. Create a Python virtual environment.
3. Install DAClaw with the `web` extra.
4. Build the React dashboard once.
5. Create Desktop and Start Menu shortcuts.
6. Let the shortcut start the local dashboard and open the browser.

## Requirements

- Windows 10 or newer
- Python 3.11 or newer
- PowerShell 5.1 or newer

Node.js is only needed by developers when building the release payload. End users should receive a package that already contains `daclaw_cli/web_dist`.

The long-term installer should bundle or bootstrap Python. This MVP keeps Python as a prerequisite so the install/uninstall loop can be validated before building a fully self-contained `.exe`.

## Install From This Checkout

From the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\windows\install-local.ps1
```

The default install target is:

```text
%LOCALAPPDATA%\DAClaw\app
```

To choose another location:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\windows\install-local.ps1 -InstallDir "D:\Apps\DAClaw"
```

After installation, launch DAClaw from the desktop shortcut or the Start Menu.

## Launch Manually

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\DAClaw\app\packaging\windows\launch-daclaw.ps1" -AppDir "$env:LOCALAPPDATA\DAClaw\app"
```

The launcher:

- Reuses an already-running DAClaw dashboard when possible.
- Searches ports starting at `9119`.
- Starts the dashboard with `--host 127.0.0.1`.
- Opens the browser to the selected local URL.
- Writes launch logs to `%LOCALAPPDATA%\DAClaw\logs\dashboard-launch.log`.

## Build The Setup EXE

Install Inno Setup 6, then run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\windows\build-installer.ps1 -Version 0.1.0
```

This creates a clean payload under:

```text
packaging\windows\build\payload
```

and, when `ISCC.exe` is available, compiles:

```text
packaging\windows\dist\DAClaw-Setup-0.1.0.exe
```

To build only the payload without compiling the installer:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\windows\build-installer.ps1 -Version 0.1.0 -NoCompile
```

To build a private installer that seeds Volcengine Ark as the default provider,
set the key in the shell environment and enable the preset:

```powershell
$env:DACLAW_DEFAULT_ARK_API_KEY = "<your-private-ark-key>"
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\windows\build-installer.ps1 -Version 0.1.0 -IncludeVolcengineArkDefaults
```

Do not commit the generated payload or installer if it contains a private API key.

## Installer Behavior

`DAClaw.iss` wraps the copied source and these scripts with Inno Setup:

- Inno Setup copies the release payload into `{localappdata}\DAClaw\app`.
- The post-install step runs `setup-runtime.ps1 -SkipWebBuild -NoShortcut`.
- Inno Setup creates Desktop and Start Menu shortcuts that call `launch-daclaw.ps1`.
- Windows Settings can uninstall DAClaw.
- The uninstaller removes the app files and generated `venv`.
- User configuration and sessions under `~/.daclaw` are preserved.

That keeps the actual install logic in version-controlled PowerShell scripts instead of hiding it inside installer metadata.
