# DAClaw Windows Installer Prerequisites

The current Windows installer is the first packaging milestone. It already gives users the normal installation shape:

- choose an install folder
- install DAClaw into that folder
- create a DAClaw-only Python virtual environment
- create Desktop and Start Menu shortcuts
- uninstall from Windows Settings

## Current Requirement

This installer currently requires Python 3.11 or newer to be installed before setup runs.

Python is used only to create and run DAClaw's private virtual environment:

```text
<install-folder>\venv
```

It does not install DAClaw packages into the user's base Python environment.

## Not Required For Users

The packaged installer should include `daclaw_cli/web_dist`, so users do not need:

- Node.js
- npm
- Git
- the source checkout

Those are build-time tools only.

## Future Fully Self-Contained Installer

The next packaging milestone should remove the Python prerequisite by bundling or downloading a Python runtime. The intended user experience is:

```text
Download DAClaw-Setup.exe
Choose an install folder
Launch DAClaw
```

No terminal, Git, Node.js, or system Python setup should be needed in that final path.

## Private Default API Configuration

Installers can seed a default provider without committing secrets to the repository.

For Volcengine Ark private builds:

```text
base_url: https://ark.cn-beijing.volces.com/api/v3
model: glm-5.1
key env: ARK_API_KEY
```

Use `DACLAW_DEFAULT_ARK_API_KEY` at build time. The generated installer payload contains the private `.env`; the source tree does not.
