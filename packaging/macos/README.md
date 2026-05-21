# DAClaw macOS App and DMG

This folder builds the familiar macOS distribution shape:

```text
DAClaw.app
DAClaw-macOS-<version>.dmg
```

Users open the `.dmg`, drag `DAClaw.app` into Applications, then launch DAClaw like a normal macOS app.

## Runtime Layout

The app bundle contains a read-only payload:

```text
DAClaw.app/Contents/Resources/payload
```

On first launch, the app copies that payload into:

```text
~/Library/Application Support/DAClaw/app
```

Then it creates a private virtual environment:

```text
~/Library/Application Support/DAClaw/app/venv
```

DAClaw dependencies are installed into that venv only. The user's base Python environment is not modified.

## Requirements For This Preview

- macOS 12 or newer
- Python 3.11 or newer
- Internet access during first launch

Node.js, npm, and Git are build-time tools only. Users do not need them.

## Build

From the repository root on macOS:

```bash
packaging/macos/build-app.sh --version 0.1.0
```

Outputs:

```text
packaging/macos/dist/DAClaw-macOS-0.1.0.zip
packaging/macos/dist/DAClaw-macOS-0.1.0.dmg
```

To use an existing `daclaw_cli/web_dist`:

```bash
packaging/macos/build-app.sh --version 0.1.0 --skip-web-build
```

## Private Volcengine Ark Defaults

To build a private app that seeds Volcengine Ark as the default provider:

```bash
export DACLAW_DEFAULT_ARK_API_KEY="<your-private-ark-key>"
packaging/macos/build-app.sh --version 0.1.0 --include-volcengine-ark-defaults
```

The preset uses:

```text
base_url: https://ark.cn-beijing.volces.com/api/v3
model: glm-5.1
key env: ARK_API_KEY
```

Do not commit or publicly upload a build that contains a shared private API key unless that is intentional.

## Uninstall

Remove the app:

```text
Drag DAClaw.app to Trash
```

Optional runtime cleanup:

```bash
rm -rf "$HOME/Library/Application Support/DAClaw"
```

Optional user data cleanup:

```bash
rm -rf "$HOME/.daclaw"
```

By default, user configuration and sessions are preserved.

## Signing

The build script applies ad-hoc signing when `codesign` is available. For public distribution, use Apple Developer ID signing and notarization.
