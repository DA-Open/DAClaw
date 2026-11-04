# DAClaw + gpt-5.4: PersonaMem-v3 smoke run

3/3 official personalized_recommendation instances completed for persona 8.
This evaluates three instances of one task category, not the full benchmark.

| Query | Tier concordance | Recall@1 | Recall@3 |
|---|---:|---:|---:|
| 8:0035:recsys_2026-04-06_a0 | 0.5070 | 100% | 100% |
| 8:0036:recsys_2026-04-06_a1 | 0.4314 | 0% | 100% |
| 8:0042:recsys_2026-04-06_a2 | 0.4348 | 0% | 100% |

Mean tier concordance: 0.4577. Mean Recall@1: 33.33%. Mean Recall@3: 100%.

Official deterministic upstream scorer; no LLM judge.
All task trajectories, tool calls, tool results, final answers and token usage are in tasks/001..003/trajectory.json.
Per-task scores are in results.csv/results.json; aggregate scores are in summary.json.
Data sources and hashes are in data_provenance.json; validation evidence is in verification.json.
Transient model connection errors recovered through retries.
Credentials are not stored in artifacts.
