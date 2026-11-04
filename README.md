# DAClaw benchmark rollout — yangkuntao

This independent branch packages the **DAClaw harness**, **PersonaMem-v3**,
**SpreadsheetBench-2**, the adapted runners, and six recorded example rollouts.
All three source projects are ordinary directories, not nested Git submodules.

```text
DAClaw/                    Harness and benchmark adapters
PersonaMem-v3/             Official benchmark source and license
SpreadsheetBench-2/        Official benchmark source
bench_output/              Selected data, recorded trajectories, Excel files and scores
BENCH_EVALUATION_GUIDE.md  Detailed setup and rollout instructions
rollout.py                 Portable entry point for both benchmarks
requirements-rollout.txt  Python installation requirements
```

## Quick start (Linux, Python 3.11+)

```bash
git clone --single-branch --branch yangkuntao https://github.com/DA-Open/DAClaw.git
cd DAClaw
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-rollout.txt
sudo apt-get update
sudo apt-get install -y libreoffice-calc
read -rs -p 'Model API key: ' BENCH_API_KEY; echo
export BENCH_API_KEY
python rollout.py --bench all --limit 3
unset BENCH_API_KEY
```

Default model: `gpt-5.4`. Default OpenAI-compatible API base:
`https://api2.aigcbest.top/v1`. Provide your own API key with access to that
endpoint, or override `--model`, `--base-url`, and `--api-key-env`.
No credentials are included in this repository.

New outputs appear in `bench_output/personamem_v3_rollout_<run-name>/` and
`bench_output/spreadsheetbench2_rollout_<run-name>/`. Published sample results
remain unchanged. Use `--bench persona` or `--bench spreadsheet` to run one.
`python rollout.py --dry-run` prints the actual commands without API calls.

See [BENCH_EVALUATION_GUIDE.md](BENCH_EVALUATION_GUIDE.md) for metrics and artifacts.

## Recorded sample results

| Benchmark subset | Generation/scoring completed | Metric |
|---|---:|---|
| PersonaMem-v3, persona 8, first 3 personalized recommendation instances | 3/3 | Mean tier concordance 0.4577 |
| SpreadsheetBench-2, first 3 Debugging instances | 3/3 | Official strict accuracy 0/3 |

These are six sample rollouts, not full-benchmark scores. The spreadsheet
evaluator was also checked with gold-output positive controls (3/3 pass).

## Data, licenses and execution

PersonaMem-v3 data derives from `facebook/gistbench` and is subject to
**CC-BY-NC-4.0** as documented by its upstream project; preserve attribution and
non-commercial restrictions. Source URLs and hashes are saved with each sample
run. Consult each bundled project's license and upstream documentation before
reuse. Upstream repositories retain their original notices.

The spreadsheet adapter executes model-generated terminal/Python commands with
your user permissions. Use a disposable environment for untrusted models.
PersonaMem's read-only tools enforce a time-masked snapshot boundary. Offline
fixture smoke tests validate adapter behavior, not model benchmark performance.

`PUBLICATION_MANIFEST.json` records original source revisions and exclusions.
Virtual environments, caches, the full redundant ZIP download, and machine-local
LibreOffice binaries are not committed. Selected data and full conversation
trajectories are committed. Historical traces preserve the original machine's
absolute paths as provenance; `rollout.py` resolves new paths from this checkout.
