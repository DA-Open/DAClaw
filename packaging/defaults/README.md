# Installer Defaults

This directory is used for optional first-run defaults that are copied into a private release payload.

Do not commit real API keys here.

Supported private files:

```text
packaging/defaults/config.yaml
packaging/defaults/.env
```

During installation, `packaging/common/apply-default-settings.py` seeds:

- `{DACLAW_HOME}/config.yaml`
- `{DACLAW_HOME}/.env`

Existing user values are preserved.

## Volcengine Ark Preset

The Windows and macOS build scripts can generate a private Volcengine Ark preset in the payload:

```powershell
$env:DACLAW_DEFAULT_ARK_API_KEY = "<your-private-key>"
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\windows\build-installer.ps1 -Version 0.1.0 -IncludeVolcengineArkDefaults
```

```bash
export DACLAW_DEFAULT_ARK_API_KEY="<your-private-key>"
packaging/macos/build-app.sh --version 0.1.0 --include-volcengine-ark-defaults
```

The generated payload defaults use:

```yaml
model:
  provider: custom:volcengine-ark
  default: glm-5.1
```

and store the API key in `.env` as `ARK_API_KEY`.
