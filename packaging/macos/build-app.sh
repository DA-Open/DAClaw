#!/usr/bin/env bash
set -euo pipefail

VERSION="0.1.0"
SOURCE_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
BUILD_ROOT="$(cd "$(dirname "$0")" && pwd)/build"
DIST_DIR="$(cd "$(dirname "$0")" && pwd)/dist"
SKIP_WEB_BUILD=0
INCLUDE_VOLCENGINE_ARK_DEFAULTS=0
ARK_API_KEY="${DACLAW_DEFAULT_ARK_API_KEY:-}"
ARK_BASE_URL="${DACLAW_DEFAULT_ARK_BASE_URL:-https://ark.cn-beijing.volces.com/api/v3}"
ARK_ENDPOINT_ID="${DACLAW_DEFAULT_ARK_ENDPOINT_ID:-}"
ARK_MODEL="${DACLAW_DEFAULT_ARK_MODEL:-${DACLAW_DEFAULT_ARK_ENDPOINT_ID:-}}"
NO_DMG=0
NO_ZIP=0
AD_HOC_SIGN=1
BUNDLE_PYTHON=1
PYTHON_STANDALONE_VERSION="${DACLAW_BUNDLED_PYTHON_VERSION:-3.11}"
PYTHON_STANDALONE_ARCHIVE_URL="${DACLAW_BUNDLED_PYTHON_URL:-}"

usage() {
  cat <<'EOF'
Build DAClaw.app and a macOS dmg.

Usage:
  packaging/macos/build-app.sh [options]

Options:
  --version VERSION                    Release version, default 0.1.0
  --source-dir PATH                    Repository root
  --build-root PATH                    Temporary build directory
  --skip-web-build                     Use existing daclaw_cli/web_dist
  --include-volcengine-ark-defaults    Generate private Volcengine Ark defaults
  --ark-api-key KEY                    Private Ark key (or DACLAW_DEFAULT_ARK_API_KEY)
  --ark-base-url URL                   Ark OpenAI-compatible base URL
  --ark-model MODEL                    Ark model parameter, e.g. doubao-seed-2-0-pro-260215 or ep-...
  --ark-endpoint-id ID                 Alias for --ark-model, kept for endpoint-based Ark deployments
  --no-dmg                             Do not create dmg
  --no-zip                             Do not create zip
  --no-bundle-python                   Do not bundle the arm64 Python runtime
  --python-version VERSION             Bundled Python minor version, default 3.11
  --python-standalone-url URL          Override python-build-standalone archive URL
  --no-sign                            Skip ad-hoc codesign
  -h, --help                           Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --source-dir) SOURCE_DIR="$(cd "$2" && pwd)"; shift 2 ;;
    --build-root) BUILD_ROOT="$2"; shift 2 ;;
    --skip-web-build) SKIP_WEB_BUILD=1; shift ;;
    --include-volcengine-ark-defaults) INCLUDE_VOLCENGINE_ARK_DEFAULTS=1; shift ;;
    --ark-api-key) ARK_API_KEY="$2"; shift 2 ;;
    --ark-base-url) ARK_BASE_URL="$2"; shift 2 ;;
    --ark-endpoint-id) ARK_ENDPOINT_ID="$2"; ARK_MODEL="$2"; shift 2 ;;
    --ark-model) ARK_MODEL="$2"; shift 2 ;;
    --no-dmg) NO_DMG=1; shift ;;
    --no-zip) NO_ZIP=1; shift ;;
    --no-bundle-python) BUNDLE_PYTHON=0; shift ;;
    --python-version) PYTHON_STANDALONE_VERSION="$2"; shift 2 ;;
    --python-standalone-url) PYTHON_STANDALONE_ARCHIVE_URL="$2"; shift 2 ;;
    --no-sign) AD_HOC_SIGN=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_ROOT="$(mkdir -p "$BUILD_ROOT" && cd "$BUILD_ROOT" && pwd)"
DIST_DIR="$(mkdir -p "$DIST_DIR" && cd "$DIST_DIR" && pwd)"
PAYLOAD_DIR="$BUILD_ROOT/payload"
APP_BUNDLE="$BUILD_ROOT/DAClaw.app"
APP_PAYLOAD="$APP_BUNDLE/Contents/Resources/payload"
APP_PYTHON="$APP_BUNDLE/Contents/Resources/python"
APP_ARCH_REPORT="$APP_BUNDLE/Contents/Resources/build-architecture-report.txt"

copy_dir() {
  local name="$1"
  if [[ -d "$SOURCE_DIR/$name" ]]; then
    mkdir -p "$(dirname "$PAYLOAD_DIR/$name")"
    /usr/bin/rsync -a \
      --exclude '.git' \
      --exclude '.pytest_cache' \
      --exclude '.vscode' \
      --exclude '__pycache__' \
      --exclude 'node_modules' \
      --exclude 'venv' \
      --exclude '.venv' \
      --exclude 'dist' \
      --exclude 'build' \
      --exclude 'daclaw_agent.egg-info' \
      "$SOURCE_DIR/$name/" "$PAYLOAD_DIR/$name/"
  fi
}

copy_file() {
  local name="$1"
  if [[ -f "$SOURCE_DIR/$name" ]]; then
    mkdir -p "$(dirname "$PAYLOAD_DIR/$name")"
    cp "$SOURCE_DIR/$name" "$PAYLOAD_DIR/$name"
  fi
}

download_bundled_python() {
  local download_dir="$BUILD_ROOT/python-standalone"
  local archive="$download_dir/python-standalone.tar.gz"
  local extract_dir="$download_dir/extract"

  rm -rf "$download_dir" "$APP_PYTHON"
  mkdir -p "$download_dir" "$extract_dir" "$APP_BUNDLE/Contents/Resources"

  local url="$PYTHON_STANDALONE_ARCHIVE_URL"
  if [[ -z "$url" ]]; then
    if ! command -v python3 >/dev/null 2>&1; then
      echo "python3 is required during release build to resolve python-build-standalone assets." >&2
      exit 1
    fi
    url="$(PYTHON_STANDALONE_VERSION="$PYTHON_STANDALONE_VERSION" python3 - <<'PY'
import json
import os
import re
import sys
import urllib.request

minor = os.environ.get("PYTHON_STANDALONE_VERSION", "3.11")
api = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
headers = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "daclaw-macos-packager",
}
token = os.environ.get("GITHUB_TOKEN", "").strip()
if token:
    headers["Authorization"] = f"Bearer {token}"

request = urllib.request.Request(api, headers=headers)
with urllib.request.urlopen(request, timeout=30) as response:
    release = json.load(response)

pattern = re.compile(
    rf"^cpython-{re.escape(minor)}\.\d+\+\d+-aarch64-apple-darwin-install_only\.tar\.gz$"
)
matches = [
    asset["browser_download_url"]
    for asset in release.get("assets", [])
    if pattern.match(asset.get("name", ""))
]
if not matches:
    print(f"No aarch64 macOS python-build-standalone asset found for {minor}", file=sys.stderr)
    raise SystemExit(1)
print(matches[0])
PY
)"
  fi

  echo "Downloading bundled arm64 Python runtime:"
  echo "  $url"
  /usr/bin/curl -L --fail --retry 3 --output "$archive" "$url"
  /usr/bin/tar -xzf "$archive" -C "$extract_dir"

  if [[ ! -d "$extract_dir/python" ]]; then
    echo "Unexpected python-build-standalone archive layout; missing python/ directory." >&2
    exit 1
  fi

  /usr/bin/rsync -a "$extract_dir/python/" "$APP_PYTHON/"
  printf '%s\n' "$PYTHON_STANDALONE_VERSION" > "$APP_PYTHON/.daclaw-python-version"

  local bundled_python="$APP_PYTHON/bin/python3"
  if [[ ! -x "$bundled_python" ]]; then
    bundled_python="$APP_PYTHON/bin/python3.11"
  fi
  if [[ ! -x "$bundled_python" ]]; then
    echo "Bundled Python executable not found under $APP_PYTHON/bin." >&2
    exit 1
  fi
  "$bundled_python" - <<'PY'
import platform
import sys

if platform.machine() != "arm64":
    raise SystemExit(f"Bundled Python is {platform.machine()}, expected arm64")
if sys.version_info < (3, 11):
    raise SystemExit(f"Bundled Python is {sys.version}, expected 3.11+")
print("Bundled Python:", sys.version.split()[0], platform.machine())
PY
}

verify_macho_architectures() {
  local app="$1"
  local report="$2"
  local failures=0

  : > "$report"
  echo "DAClaw macOS build architecture report" >> "$report"
  echo "version=$VERSION" >> "$report"
  echo "built_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >> "$report"
  echo "runner_arch=$(/usr/bin/uname -m)" >> "$report"
  echo >> "$report"

  if [[ "$(/usr/bin/uname -m)" != "arm64" ]]; then
    echo "Build host is not arm64. Refusing to produce Apple Silicon release." | tee -a "$report" >&2
    return 1
  fi

  local executable="$app/Contents/MacOS/DAClaw"
  if [[ ! -x "$executable" ]]; then
    echo "Missing executable: $executable" | tee -a "$report" >&2
    return 1
  fi

  local executable_info
  executable_info="$(/usr/bin/file "$executable")"
  echo "$executable_info" >> "$report"
  if [[ "$executable_info" != *"Mach-O"* || "$executable_info" != *"arm64"* ]]; then
    echo "CFBundleExecutable is not an arm64 Mach-O binary: $executable_info" | tee -a "$report" >&2
    return 1
  fi

  while IFS= read -r -d '' file_path; do
    local info
    info="$(/usr/bin/file "$file_path")"
    if [[ "$info" == *"Mach-O"* ]]; then
      echo "$info" >> "$report"
      if [[ "$info" != *"arm64"* ]]; then
        echo "Non-arm64 Mach-O binary found: $info" | tee -a "$report" >&2
        failures=$((failures + 1))
      fi
    fi
  done < <(/usr/bin/find "$app" -type f \( -perm -111 -o -name '*.dylib' -o -name '*.so' \) -print0)

  if [[ "$failures" -gt 0 ]]; then
    echo "Architecture verification failed with $failures non-arm64 Mach-O file(s)." | tee -a "$report" >&2
    return 1
  fi

  echo >> "$report"
  echo "architecture_verification=passed" >> "$report"
  echo "Architecture verification passed: all Mach-O executables have arm64 slices."
}

generate_app_icon() {
  local iconset="$BUILD_ROOT/AppIcon.iconset"
  local icon_path="$APP_BUNDLE/Contents/Resources/AppIcon.icns"

  if ! command -v swift >/dev/null 2>&1; then
    echo "swift is required to generate the macOS app icon." >&2
    exit 1
  fi
  if [[ ! -x /usr/bin/iconutil ]]; then
    echo "iconutil is required to generate the macOS app icon." >&2
    exit 1
  fi

  rm -rf "$iconset" "$icon_path"
  swift "$SCRIPT_DIR/generate-app-icon.swift" "$iconset"
  /usr/bin/iconutil -c icns "$iconset" -o "$icon_path"
  test -f "$icon_path"
  echo "App icon: $icon_path"
}

echo "Building DAClaw macOS app"
echo "Version: $VERSION"
echo "Source:  $SOURCE_DIR"
echo "Build:   $BUILD_ROOT"

if [[ "$SKIP_WEB_BUILD" -eq 0 ]]; then
  if ! command -v npm >/dev/null 2>&1; then
    echo "npm was not found. Install Node.js 20 LTS or pass --skip-web-build." >&2
    exit 1
  fi
  echo "Building Web UI before packaging..."
  (cd "$SOURCE_DIR/web" && npm install && npm run build)
fi

if [[ ! -f "$SOURCE_DIR/daclaw_cli/web_dist/index.html" ]]; then
  echo "Built Web UI missing: $SOURCE_DIR/daclaw_cli/web_dist/index.html" >&2
  exit 1
fi

rm -rf "$PAYLOAD_DIR" "$APP_BUNDLE"
mkdir -p "$PAYLOAD_DIR"

runtime_dirs=(
  acp_adapter
  acp_registry
  agent
  assets
  cron
  daclaw_cli
  gateway
  hermes_cli
  packaging/common
  packaging/defaults
  packaging/macos
  plugins
  skills
  tools
  web
)

runtime_files=(
  .env.example
  LICENSE
  MANIFEST.in
  README.md
  batch_runner.py
  cli.py
  daclaw
  daclaw_constants.py
  daclaw_logging.py
  daclaw_state.py
  daclaw_time.py
  mcp_serve.py
  model_tools.py
  pyproject.toml
  requirements.txt
  rl_cli.py
  run_agent.py
  toolset_distributions.py
  toolsets.py
  trajectory_compressor.py
  utils.py
)

for dir in "${runtime_dirs[@]}"; do
  copy_dir "$dir"
done
for file in "${runtime_files[@]}"; do
  copy_file "$file"
done

printf '%s\n' "$VERSION" > "$PAYLOAD_DIR/.daclaw-payload-version"

if [[ "$INCLUDE_VOLCENGINE_ARK_DEFAULTS" -eq 1 ]]; then
  if [[ -z "$ARK_API_KEY" ]]; then
    echo "Set DACLAW_DEFAULT_ARK_API_KEY or pass --ark-api-key when using --include-volcengine-ark-defaults." >&2
    exit 1
  fi
  if [[ -z "$ARK_MODEL" ]]; then
    echo "Set DACLAW_DEFAULT_ARK_MODEL or pass --ark-model when using --include-volcengine-ark-defaults." >&2
    echo "Use the Ark model value your account can access, e.g. doubao-seed-2-0-pro-260215 or an ep-... endpoint ID." >&2
    exit 1
  fi
  mkdir -p "$PAYLOAD_DIR/packaging/defaults"
  cat > "$PAYLOAD_DIR/packaging/defaults/config.yaml" <<EOF
model:
  provider: custom:volcengine-ark
  default: $ARK_MODEL
  base_url: $ARK_BASE_URL

custom_providers:
  - name: volcengine-ark
    base_url: $ARK_BASE_URL
    key_env: ARK_API_KEY
    api_mode: chat_completions
    model: $ARK_MODEL
EOF
  printf 'ARK_API_KEY=%s\n' "$ARK_API_KEY" > "$PAYLOAD_DIR/packaging/defaults/.env"
  echo "Included private Volcengine Ark defaults in payload."
fi

mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources"
sed "s/__APP_VERSION__/$VERSION/g" "$SCRIPT_DIR/Info.plist" > "$APP_BUNDLE/Contents/Info.plist"
generate_app_icon
if ! command -v clang >/dev/null 2>&1; then
  echo "clang is required to build the native macOS launcher." >&2
  exit 1
fi
clang -arch arm64 -mmacosx-version-min=12.0 "$SCRIPT_DIR/launcher.c" -o "$APP_BUNDLE/Contents/MacOS/DAClaw"
cp "$SCRIPT_DIR/launch-daclaw.sh" "$APP_BUNDLE/Contents/Resources/launch-daclaw.sh"
chmod +x "$APP_BUNDLE/Contents/Resources/launch-daclaw.sh"
/usr/bin/rsync -a "$PAYLOAD_DIR/" "$APP_PAYLOAD/"

if [[ "$BUNDLE_PYTHON" -eq 1 ]]; then
  download_bundled_python
fi

verify_macho_architectures "$APP_BUNDLE" "$APP_ARCH_REPORT"

if [[ "$AD_HOC_SIGN" -eq 1 ]] && command -v codesign >/dev/null 2>&1; then
  echo "Applying ad-hoc codesign..."
  codesign --force --deep --sign - "$APP_BUNDLE"
fi

if [[ "$NO_ZIP" -eq 0 ]]; then
  ZIP_PATH="$DIST_DIR/DAClaw-macOS-$VERSION.zip"
  rm -f "$ZIP_PATH"
  (cd "$BUILD_ROOT" && /usr/bin/ditto -c -k --keepParent "DAClaw.app" "$ZIP_PATH")
  echo "ZIP: $ZIP_PATH"
fi

if [[ "$NO_DMG" -eq 0 ]]; then
  DMG_ROOT="$BUILD_ROOT/dmg-root"
  DMG_PATH="$DIST_DIR/DAClaw-macOS-$VERSION.dmg"
  rm -rf "$DMG_ROOT" "$DMG_PATH"
  mkdir -p "$DMG_ROOT"
  cp -R "$APP_BUNDLE" "$DMG_ROOT/DAClaw.app"
  ln -s /Applications "$DMG_ROOT/Applications"
  /usr/bin/hdiutil create -volname "DAClaw" -srcfolder "$DMG_ROOT" -ov -format UDZO "$DMG_PATH"
  echo "DMG: $DMG_PATH"
fi
