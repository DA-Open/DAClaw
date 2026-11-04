# PersonaMem-v3 → DAClaw

Directly runs `run_agent.AIAgent` against an OpenAI-compatible Chat Completions
endpoint. The endpoint can be either the base `/v1` URL or the full
`/v1/chat/completions` URL. No core harness changes are required.

This smoke adapter selects the first **3 individual
`personalized_recommendation` instances** for persona `8`, in the official
`test.json` order. This is not three task categories and not a full benchmark
evaluation. It uses upstream `materialize_snapshot`,
`personalized_recommendation_prompt`, and
`compute_personalized_recommendation_metrics` without modifying their scoring.
There is no LLM judge. The headline metric is `tier_concordance`; Recall@K,
NDCG@K, MRR, and negative-ranking violations are also saved.

```bash
source venv/bin/activate
read -rs -p 'Model API key: ' PM3_API_KEY
export PM3_API_KEY
python environments/benchmarks/personamem_v3/runner.py \
  --benchmark /home/yangkuntao/DaraOS/PersonaMem-v3 \
  --output /home/yangkuntao/DaraOS/bench_output/personamem_v3_gpt54 \
  --download --data-endpoint https://hf-mirror.com \
  --model gpt-5.4 --base-url https://api2.aigcbest.top/v1/chat/completions \
  --limit 3
unset PM3_API_KEY
```

Use a fresh output directory for each run. For already-downloaded data, omit
`--download` and set `--backend-dir /path/to/backend`. The mirror supplies the
official `bowen-upenn/PersonaMem-v3` files; source URLs and file SHA256 hashes are
saved in `data_provenance.json`.

Outputs:

- `run_config.json`, `selected_tasks.json`, `data_provenance.json`: configuration,
  exact task selection and source data hashes. Ground truth is evaluator-only.
- `tasks/001..003/history/`: upstream time-masked, leak-stripped history snapshots.
- `tasks/001..003/trajectory.json`: full DAClaw conversation, tool calls/results,
  final response, completion flags and token usage.
- `tasks/001..003/agent.log`, `request.json`, `result.json`: diagnostics and results.
- `results.json`, `results.csv`, `summary.json`: per-instance and mean scores.

The agent has only `read_file` and `search_files`. Actual tool dispatch validates
resolved paths against the snapshot, including symlink resolution. It cannot
read the test set, raw backend, grader metadata, API key, or other tasks. The
credential is removed from the worker environment before the agent starts.
Profiles and caches are task-local. Existing working tree changes are preserved.

```bash
source venv/bin/activate
python -m unittest discover -s environments/benchmarks/personamem_v3 -p 'test_*.py'
```
