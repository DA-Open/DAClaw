#!/bin/bash

# GDPval Evaluation
#
# Run from repo root:
#   bash environments/benchmarks/gdpval/run_eval.sh
#
# Smoke test (3 tasks):
#   bash environments/benchmarks/gdpval/run_eval.sh --env.limit 3
#
# Single sector:
#   bash environments/benchmarks/gdpval/run_eval.sh --env.sector_filter "Professional, Scientific"
#
# Specific tasks:
#   bash environments/benchmarks/gdpval/run_eval.sh --env.task_filter "83d10b06,0a1b2c3d"
#
# Override the harness model:
#   bash environments/benchmarks/gdpval/run_eval.sh --openai.model_name deepseek-v4-pro
#
# Requires: pip install "daclaw-agent[rl]"  (atroposlib drives the evaluate CLI),
#           pandas + openpyxl + pypdf for the grader.

set -euo pipefail

mkdir -p logs environments/benchmarks/gdpval/evals
LOG_FILE="logs/gdpval_$$(date +%Y%m%d_%H%M%S).log"

echo "GDPval Evaluation"
echo "Log file: $LOG_FILE"
echo ""

export PYTHONUNBUFFERED=1
# The gpt2 tokenizer is cached; skip the Hub round-trip that can stall offline.
export HF_HUB_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1
export LOGLEVEL="${LOGLEVEL:-INFO}"

python environments/benchmarks/gdpval/gdpval_env.py evaluate \
  --config environments/benchmarks/gdpval/default.yaml \
  "$@" \
  2>&1 | tee "$LOG_FILE"

echo ""
echo "Log saved to: $LOG_FILE"
echo "Results: environments/benchmarks/gdpval/evals/"
