#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Stop the DAClaw Data Agent demo servers started by start.sh
#   ./scripts/demo/stop.sh
# ---------------------------------------------------------------------------
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$SCRIPT_DIR/.run"
API_PORT="${DACLAW_API_PORT:-9119}"
WEB_PORT="${DACLAW_WEB_PORT:-5173}"

kill_pidfile() {
  local f="$1" name="$2"
  if [ -f "$f" ]; then
    local pid; pid="$(cat "$f" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      # kill the process group so child node/uvicorn workers die too
      kill "$pid" 2>/dev/null || true
      sleep 1
      kill -9 "$pid" 2>/dev/null || true
      echo "  stopped $name (pid $pid)"
    fi
    rm -f "$f"
  fi
}

echo "▸ Stopping DAClaw demo…"
kill_pidfile "$RUN_DIR/web.pid" "frontend"
kill_pidfile "$RUN_DIR/api.pid" "backend"

# belt-and-suspenders: free the ports even if pidfiles were stale
for port in "$WEB_PORT" "$API_PORT"; do
  pids="$(lsof -ti:"$port" 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    echo "  freeing port $port ($pids)"
    echo "$pids" | xargs kill -9 2>/dev/null || true
  fi
done

echo "✓ Stopped."
