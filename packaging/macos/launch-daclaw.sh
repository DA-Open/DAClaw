#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONTENTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
PAYLOAD_DIR="$RESOURCES_DIR/payload"

APP_SUPPORT="$HOME/Library/Application Support/DAClaw"
RUNTIME_APP="$APP_SUPPORT/app"
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
      if "$path" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
      then
        printf '%s\n' "$path"
        return 0
      fi
    fi
  done

  return 1
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
  local needs_install=0

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
    "$venv_python" -m pip install --upgrade pip >> "$LAUNCH_LOG" 2>&1
    (cd "$RUNTIME_APP" && "$venv_python" -m pip install -e ".[web]" >> "$LAUNCH_LOG" 2>&1)
    if [[ -f "$payload_version" ]]; then
      cp "$payload_version" "$runtime_version"
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

  local python
  if ! python="$(find_python)"; then
    show_error "Python 3.11 or newer is required for this DAClaw preview. Install Python, then open DAClaw again."
    exit 1
  fi

  ensure_runtime "$python"
  open_or_start_dashboard
}

main "$@"
