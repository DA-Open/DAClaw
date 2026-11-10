#!/bin/bash
# PersistBench -- smoke or full run.
#
#   bash environments/benchmarks/persistbench/run_eval.sh --limit 6
#   bash environments/benchmarks/persistbench/run_eval.sh --subset beneficial --limit 3
#   bash environments/benchmarks/persistbench/run_eval.sh            # all 500
#
# Point at any OpenAI-compatible model under test:
#   bash .../run_eval.sh --sut-base-url https://api.example.com/v1 --sut-model my-model --sut-api-key sk-...
#
# Judge defaults to deepseek-v4-pro through the local model proxy. That is NOT
# the paper-calibrated judge (kimi-k2-thinking via OpenRouter), so the numbers
# are internal-only. Set PERSISTBENCH_JUDGE_MODEL to override.

set -euo pipefail

cd "$(dirname "$0")/../../.."

export PYTHONUNBUFFERED=1
if [ -z "${OPENAI_API_KEY:-}" ] && [ -f "$HOME/.daclaw/.env" ]; then
  export OPENAI_API_KEY="$(grep -m1 '^OPENAI_API_KEY=' "$HOME/.daclaw/.env" | cut -d= -f2-)"
fi

# Prefer the project venv -- the system python here is a 3.10 anaconda base
# whose pandas is incompatible with the host libstdc++.
if [ -x "$PWD/venv/bin/python" ]; then
  PYTHON="$PWD/venv/bin/python"
else
  PYTHON="python3"
fi

"$PYTHON" environments/benchmarks/persistbench/runner.py "$@"
