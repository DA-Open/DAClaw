#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# DAClaw Data Agent demo — one-command dev launcher (hot-reload)
#
#   ./scripts/demo/start.sh
#
# What it does (idempotent, safe to re-run):
#   1. picks a Python 3.10+ interpreter (prefers conda `agent` env)
#   2. installs backend + frontend deps on first run
#   3. seeds the demo e-commerce dataset on first run
#   4. starts the backend API (:9119) and the Vite hot-reload dev server (:5173)
#   5. opens the browser at the Data Agent page
#
# Frontend edits (.tsx/.css) hot-reload automatically. Backend edits (.py)
# need a restart — just re-run this script (it restarts cleanly).
#
# Stop everything with:  ./scripts/demo/stop.sh
# ---------------------------------------------------------------------------
set -euo pipefail

# --- paths -----------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
WEB_DIR="$REPO_ROOT/web"
RUN_DIR="$SCRIPT_DIR/.run"          # pid + log files live here
mkdir -p "$RUN_DIR"

# --- config (override via env) ---------------------------------------------
API_PORT="${DACLAW_API_PORT:-9119}"
WEB_PORT="${DACLAW_WEB_PORT:-5173}"
# Use the real DAClaw home so the agent finds its inference credentials
# (~/.daclaw/.env's ARK_API_KEY and ~/.daclaw/auth.json's custom:volcengine-ark
# endpoint). Isolating DACLAW_HOME to ~/.daclaw_demo hid those creds and broke
# the agentic chat ("No inference provider configured"). Demo data lives in a
# subdir so nothing collides with normal use.
export DACLAW_HOME="${DACLAW_HOME:-$HOME/.daclaw}"
export DACLAW_WEB_DEV_TOKEN="${DACLAW_WEB_DEV_TOKEN:-dev-daclaw-token}"
DEMO_DATA_DIR="$DACLAW_HOME/demo_data"

# --- pick a Python 3.10+ interpreter ---------------------------------------
pick_python() {
  local candidates=(
    "${DACLAW_PYTHON:-}"
    "$HOME/miniconda3/envs/agent/bin/python"
    "$HOME/miniconda3/bin/python"
    "python3"
    "python"
  )
  for py in "${candidates[@]}"; do
    [ -z "$py" ] && continue
    if command -v "$py" >/dev/null 2>&1 || [ -x "$py" ]; then
      # require >= 3.10 (repo uses X | None syntax)
      if "$py" -c 'import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; then
        echo "$py"; return 0
      fi
    fi
  done
  return 1
}

PY="$(pick_python || true)"
if [ -z "$PY" ]; then
  echo "✗ No Python 3.10+ found. Set DACLAW_PYTHON=/path/to/python and retry." >&2
  exit 1
fi
echo "▸ Python: $PY ($("$PY" --version 2>&1))"

# --- helpers ---------------------------------------------------------------
free_port() {
  local port="$1"
  local pids
  pids="$(lsof -ti:"$port" 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    echo "  freeing port $port (killing: $pids)"
    echo "$pids" | xargs kill -9 2>/dev/null || true
    sleep 1
  fi
}

wait_for_http() {
  local url="$1" name="$2" tries=40
  for _ in $(seq 1 "$tries"); do
    if curl -s -o /dev/null "$url" 2>/dev/null; then return 0; fi
    sleep 0.5
  done
  echo "  ⚠ $name did not come up at $url (check logs in $RUN_DIR)"
  return 1
}

# --- 1. backend deps (first run) -------------------------------------------
echo "▸ Checking backend dependencies…"
if ! "$PY" -c "import duckdb, pandas, openpyxl, fastapi, uvicorn, multipart, yaml, formulas, sqlalchemy, pypdf" 2>/dev/null; then
  echo "  installing backend deps (data engine, documents, FastAPI, SQLAlchemy + common DB drivers)…"
  "$PY" -m pip install -q duckdb pandas openpyxl pyarrow fastapi "uvicorn[standard]" python-multipart pyyaml formulas sqlalchemy pypdf psycopg2-binary pymysql snowflake-sqlalchemy pyodbc || {
    echo "✗ backend dependency install failed" >&2; exit 1; }
else
  echo "  ok"
fi

# --- 2. frontend deps (first run) ------------------------------------------
echo "▸ Checking frontend dependencies…"
if [ ! -d "$WEB_DIR/node_modules" ]; then
  echo "  installing frontend deps (npm install)…"
  (cd "$WEB_DIR" && npm install) || { echo "✗ npm install failed" >&2; exit 1; }
else
  echo "  ok"
fi

# --- 3. seed demo data (first run) -----------------------------------------
if [ ! -f "$DEMO_DATA_DIR/ecommerce.sqlite" ]; then
  echo "▸ Seeding demo dataset into $DEMO_DATA_DIR …"
  (cd "$REPO_ROOT" && "$PY" -c "from agent.data_agent import seed; seed.write_dataset('$DEMO_DATA_DIR')") \
    && echo "  seeded (customers/products/orders/order_items/events)"
else
  echo "▸ Demo data already present ($DEMO_DATA_DIR)"
fi

# --- 4. (re)start servers --------------------------------------------------
echo "▸ Starting servers…"
free_port "$API_PORT"
free_port "$WEB_PORT"

# backend API
( cd "$REPO_ROOT" && \
  DACLAW_HOME="$DACLAW_HOME" DACLAW_WEB_DEV_TOKEN="$DACLAW_WEB_DEV_TOKEN" \
  "$PY" -c "import daclaw_cli.web_server as ws, uvicorn; uvicorn.run(ws.app, host='127.0.0.1', port=$API_PORT, log_level='warning')" \
) > "$RUN_DIR/api.log" 2>&1 &
echo $! > "$RUN_DIR/api.pid"
echo "  backend  → http://127.0.0.1:$API_PORT  (pid $(cat "$RUN_DIR/api.pid"), log: scripts/demo/.run/api.log)"

# frontend Vite (hot-reload)
( cd "$WEB_DIR" && \
  DACLAW_WEB_DEV_TOKEN="$DACLAW_WEB_DEV_TOKEN" \
  npm run dev -- --port "$WEB_PORT" --strictPort \
) > "$RUN_DIR/web.log" 2>&1 &
echo $! > "$RUN_DIR/web.pid"
echo "  frontend → http://localhost:$WEB_PORT   (pid $(cat "$RUN_DIR/web.pid"), log: scripts/demo/.run/web.log)"

# --- 5. wait + open browser ------------------------------------------------
wait_for_http "http://127.0.0.1:$API_PORT/api/status" "backend" || true
wait_for_http "http://localhost:$WEB_PORT/" "frontend" || true

URL="http://localhost:$WEB_PORT/data-agent"
echo ""
echo "✓ DAClaw Data Agent demo is up:"
echo "    Data Agent : $URL"
echo "    Leaderboard: http://localhost:$WEB_PORT/leaderboard"
echo ""
echo "  Edit .tsx/.css → hot-reloads automatically."
echo "  Edit .py       → re-run this script to restart the backend."
echo "  Stop all       → ./scripts/demo/stop.sh"

if command -v open >/dev/null 2>&1; then
  open "$URL" 2>/dev/null || true
fi
