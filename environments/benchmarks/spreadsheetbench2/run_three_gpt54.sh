#!/usr/bin/env bash
# Run the requested three official examples and publish the two-benchmark guide.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
PARENT_DIR="$(dirname -- "$PROJECT_DIR")"
cd "$PROJECT_DIR"
source venv/bin/activate

if [[ -z "${SSB2_API_KEY:-}" ]]; then
  read -rs -p '请输入之前提供的模型 API key: ' SSB2_API_KEY
  echo
  export SSB2_API_KEY
fi
if [[ -z "$SSB2_API_KEY" ]]; then
  echo 'API key 不能为空' >&2
  exit 1
fi

OUTPUT_DIR="$PARENT_DIR/bench_output/spreadsheetbench2_gpt54"
DATASET_DIR="$OUTPUT_DIR/data/Debugging"
if [[ -x "$OUTPUT_DIR/runtime/bin/libreoffice" ]]; then
  export PATH="$OUTPUT_DIR/runtime/bin:$PATH"
fi
if [[ ! -e "$PARENT_DIR/BENCH_EVALUATION_GUIDE.md" ]]; then
  cp environments/benchmarks/BENCH_EVALUATION_GUIDE.md "$PARENT_DIR/BENCH_EVALUATION_GUIDE.md"
fi
mkdir -p "$OUTPUT_DIR"

if [[ ! -f "$DATASET_DIR/dataset.json" ]]; then
  python -u "$SCRIPT_DIR/download_subset.py" --category Debugging --limit 3 \
    --output "$DATASET_DIR"
fi

python "$SCRIPT_DIR/runner.py" doctor --category Debugging \
  --dataset "$DATASET_DIR" --config "$SCRIPT_DIR/model.gpt54.json"
if [[ ! -f "$OUTPUT_DIR/dataset_recalculated.ok" ]]; then
  python "$SCRIPT_DIR/runner.py" recalculate-dataset --category Debugging \
    --dataset "$DATASET_DIR" --limit 3
  touch "$OUTPUT_DIR/dataset_recalculated.ok"
fi
python -u "$SCRIPT_DIR/runner.py" run --category Debugging \
  --dataset "$DATASET_DIR" --output "$OUTPUT_DIR" \
  --config "$SCRIPT_DIR/model.gpt54.json" --limit 3 --workers 1 --timeout 1800 --resume
python "$SCRIPT_DIR/runner.py" evaluate --category Debugging \
  --dataset "$DATASET_DIR" --output "$OUTPUT_DIR" --limit 3
echo "结果：$OUTPUT_DIR/evaluation.json"
echo "启动说明：$PARENT_DIR/BENCH_EVALUATION_GUIDE.md"
