#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONTENTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
PAYLOAD_DIR="$RESOURCES_DIR/payload"
BUNDLED_PYTHON_DIR="$RESOURCES_DIR/python"

APP_SUPPORT="$HOME/Library/Application Support/DAClaw"
RUNTIME_APP="$APP_SUPPORT/app"
RUNTIME_PYTHON_DIR="$APP_SUPPORT/runtime/python"
LOG_DIR="$APP_SUPPORT/logs"
LAUNCH_LOG="$LOG_DIR/launcher.log"
STDOUT_LOG="$LOG_DIR/dashboard.stdout.log"
STDERR_LOG="$LOG_DIR/dashboard.stderr.log"

HOST="127.0.0.1"
PORT_START="${DACLAW_DASHBOARD_PORT:-9119}"
PORT_SEARCH_COUNT="${DACLAW_DASHBOARD_PORT_SEARCH_COUNT:-20}"

mkdir -p "$APP_SUPPORT" "$LOG_DIR"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LAUNCH_LOG"
}

show_error() {
  local message="$1"
  log "ERROR: $message"
  /usr/bin/osascript -e "display dialog \"${message}\" buttons {\"Open Logs\", \"OK\"} default button \"OK\" with title \"DAClaw\"" >/tmp/daclaw-dialog.out 2>/dev/null || true
  if grep -q "Open Logs" /tmp/daclaw-dialog.out 2>/dev/null; then
    /usr/bin/open "$LOG_DIR"
  fi
}

find_python() {
  local bundled_python="$RUNTIME_PYTHON_DIR/bin/python3"
  if [[ ! -x "$bundled_python" ]]; then
    bundled_python="$RUNTIME_PYTHON_DIR/bin/python3.11"
  fi
  if [[ -x "$bundled_python" ]]; then
    log "Using bundled Python runtime: $bundled_python"
    printf '%s\n' "$bundled_python"
    return 0
  fi

  local host_arch
  host_arch="$(/usr/bin/uname -m)"
  if [[ "$host_arch" == "arm64" ]]; then
    log "Bundled Python runtime is unavailable; refusing system Python fallback on Apple Silicon"
    return 1
  fi

  local candidates=(
    "/opt/homebrew/bin/python3.12"
    "/opt/homebrew/bin/python3.11"
    "/usr/local/bin/python3.12"
    "/usr/local/bin/python3.11"
    "python3.12"
    "python3.11"
    "python3"
  )

  for candidate in "${candidates[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1; then
      local path
      path="$(command -v "$candidate")"
      if DACLAW_HOST_ARCH="$host_arch" "$path" - <<'PY' >/dev/null 2>&1
import os
import platform
import sys

if sys.version_info < (3, 11):
    raise SystemExit(1)

host_arch = os.environ.get("DACLAW_HOST_ARCH", "")
python_arch = platform.machine()
if host_arch == "arm64" and python_arch != "arm64":
    raise SystemExit(1)

raise SystemExit(0)
PY
      then
        local python_arch
        python_arch="$("$path" -c 'import platform; print(platform.machine())' 2>/dev/null || true)"
        log "Using Python: $path ($python_arch)"
        printf '%s\n' "$path"
        return 0
      else
        log "Skipping Python candidate: $path"
      fi
    fi
  done

  return 1
}

copy_python_runtime_if_needed() {
  if [[ ! -d "$BUNDLED_PYTHON_DIR" ]]; then
    log "No bundled Python runtime found in app bundle; falling back to system Python search"
    return 0
  fi

  local bundled_version="$BUNDLED_PYTHON_DIR/.daclaw-python-version"
  local runtime_version="$RUNTIME_PYTHON_DIR/.daclaw-python-version"
  local needs_copy=0

  if [[ ! -x "$RUNTIME_PYTHON_DIR/bin/python3" && ! -x "$RUNTIME_PYTHON_DIR/bin/python3.11" ]]; then
    needs_copy=1
  elif [[ -f "$bundled_version" ]]; then
    if [[ ! -f "$runtime_version" ]] || ! cmp -s "$bundled_version" "$runtime_version"; then
      needs_copy=1
    fi
  fi

  if [[ "$needs_copy" -eq 1 ]]; then
    log "Copying bundled Python runtime to $RUNTIME_PYTHON_DIR"
    rm -rf "$RUNTIME_PYTHON_DIR"
    mkdir -p "$RUNTIME_PYTHON_DIR"
    /usr/bin/rsync -a "$BUNDLED_PYTHON_DIR/" "$RUNTIME_PYTHON_DIR/"
  fi
}

dashboard_ready() {
  local port="$1"
  /usr/bin/curl -fsS --max-time 2 "http://$HOST:$port/api/status" >/dev/null 2>&1
}

port_available() {
  local port="$1"
  ! /usr/sbin/lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
}

copy_payload_if_needed() {
  if [[ ! -d "$PAYLOAD_DIR/daclaw_cli" ]]; then
    show_error "DAClaw payload is missing from the app bundle."
    exit 1
  fi

  local payload_version="$PAYLOAD_DIR/.daclaw-payload-version"
  local runtime_version="$RUNTIME_APP/.daclaw-payload-version"
  local needs_copy=0

  if [[ ! -d "$RUNTIME_APP/daclaw_cli" ]]; then
    needs_copy=1
  elif [[ -f "$payload_version" ]]; then
    if [[ ! -f "$runtime_version" ]] || ! cmp -s "$payload_version" "$runtime_version"; then
      needs_copy=1
    fi
  fi

  if [[ "$needs_copy" -eq 1 ]]; then
    log "Copying DAClaw payload to $RUNTIME_APP"
    mkdir -p "$RUNTIME_APP"
    /usr/bin/rsync -a --delete --exclude 'venv' "$PAYLOAD_DIR/" "$RUNTIME_APP/"
  fi
}

ensure_runtime() {
  local python="$1"
  local venv_python="$RUNTIME_APP/venv/bin/python"
  local payload_version="$RUNTIME_APP/.daclaw-payload-version"
  local runtime_version="$RUNTIME_APP/.daclaw-runtime-version"
  local python_source_version="$RUNTIME_PYTHON_DIR/.daclaw-python-version"
  local python_runtime_version="$RUNTIME_APP/.daclaw-runtime-python-version"
  local needs_install=0
  local needs_venv_recreate=0

  if [[ -f "$python_source_version" ]]; then
    if [[ ! -f "$python_runtime_version" ]] || ! cmp -s "$python_source_version" "$python_runtime_version"; then
      needs_venv_recreate=1
    fi
  fi

  if [[ "$needs_venv_recreate" -eq 1 && -d "$RUNTIME_APP/venv" ]]; then
    log "Recreating DAClaw virtual environment for bundled Python runtime"
    rm -rf "$RUNTIME_APP/venv"
  fi

  if [[ ! -x "$venv_python" ]]; then
    log "Creating DAClaw virtual environment"
    "$python" -m venv "$RUNTIME_APP/venv"
    needs_install=1
  elif [[ -f "$payload_version" ]]; then
    if [[ ! -f "$runtime_version" ]] || ! cmp -s "$payload_version" "$runtime_version"; then
      needs_install=1
    fi
  fi

  if [[ "$needs_install" -eq 1 ]]; then
    log "Installing DAClaw dependencies into private venv"
    "$venv_python" -m ensurepip --upgrade >> "$LAUNCH_LOG" 2>&1 || true
    "$venv_python" -m pip install --upgrade pip >> "$LAUNCH_LOG" 2>&1
    (cd "$RUNTIME_APP" && "$venv_python" -m pip install -e ".[web]" >> "$LAUNCH_LOG" 2>&1)
    if [[ -f "$payload_version" ]]; then
      cp "$payload_version" "$runtime_version"
    fi
    if [[ -f "$python_source_version" ]]; then
      cp "$python_source_version" "$python_runtime_version"
    fi
  fi

  local defaults_script="$RUNTIME_APP/packaging/common/apply-default-settings.py"
  if [[ -f "$defaults_script" ]]; then
    log "Applying installer defaults"
    (cd "$RUNTIME_APP" && "$venv_python" "$defaults_script" >> "$LAUNCH_LOG" 2>&1)
  fi
}

open_or_start_dashboard() {
  local selected_port=""

  for ((offset=0; offset<PORT_SEARCH_COUNT; offset++)); do
    local candidate=$((PORT_START + offset))
    if dashboard_ready "$candidate"; then
      log "Dashboard already running on port $candidate"
      /usr/bin/open "http://$HOST:$candidate/"
      return 0
    fi
  done

  for ((offset=0; offset<PORT_SEARCH_COUNT; offset++)); do
    local candidate=$((PORT_START + offset))
    if port_available "$candidate"; then
      selected_port="$candidate"
      break
    fi
  done

  if [[ -z "$selected_port" ]]; then
    show_error "No free DAClaw dashboard port found from $PORT_START."
    return 1
  fi

  log "Starting dashboard on http://$HOST:$selected_port/"
  (
    cd "$RUNTIME_APP"
    nohup "$RUNTIME_APP/venv/bin/python" -m daclaw_cli.main dashboard \
      --host "$HOST" \
      --port "$selected_port" \
      --no-open \
      >> "$STDOUT_LOG" \
      2>> "$STDERR_LOG" &
  )

  for _ in $(seq 1 60); do
    if dashboard_ready "$selected_port"; then
      /usr/bin/open "http://$HOST:$selected_port/"
      return 0
    fi
    sleep 0.75
  done

  show_error "DAClaw did not become ready. Logs are in $LOG_DIR."
  return 1
}

main() {
  log "Launching DAClaw"
  copy_payload_if_needed
  copy_python_runtime_if_needed

  local python
  if ! python="$(find_python)"; then
    if [[ "$(/usr/bin/uname -m)" == "arm64" ]]; then
      show_error "DAClaw could not find its bundled arm64 Python runtime. Reinstall DAClaw from the latest dmg."
    else
      show_error "DAClaw could not find Python 3.11 or newer. Reinstall DAClaw from the latest dmg."
    fi
    exit 1
  fi

  ensure_runtime "$python"
  open_or_start_dashboard
}

main "$@"
