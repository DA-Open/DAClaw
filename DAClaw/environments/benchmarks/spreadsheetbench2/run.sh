#!/usr/bin/env bash
set -euo pipefail
adapter_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
daclaw_dir="$(cd -- "$adapter_dir/../../.." && pwd)"
if [[ ! -x "$daclaw_dir/venv/bin/python" ]]; then
  echo "Missing DAClaw/venv; follow $adapter_dir/README.md" >&2
  exit 1
fi
source "$daclaw_dir/venv/bin/activate"
exec python "$adapter_dir/runner.py" "$@"
