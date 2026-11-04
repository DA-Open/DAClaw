# GDPval

Eval-only environment for [GDPval](https://huggingface.co/datasets/openai/gdpval) --
220 real-world knowledge-work tasks across 44 occupations and 9 sectors. Each task
gives the agent a prompt plus reference files, and is scored against a task-specific
rubric of 14-137 criteria.

This follows the eval-only pattern documented in `environments/README.md`:
inherit from `DAClawAgentBaseEnv`, stub the training methods, implement
`rollout_and_score_eval()` and `evaluate()`, drive it with the `evaluate` subcommand.

## Requirements

- `pip install "daclaw-agent[rl]"` -- atroposlib drives the evaluate CLI
- The local dataset release:
  `/home/yangxingchang/DA-Research/bench/gdpval` (2.2 GB, 552 files).
  Override with `--env.dataset_path` if it lives elsewhere.
- `pandas`, `openpyxl`, `pypdf` (all in the `[data]` extra) for parsing deliverables.
- An OpenAI-compatible endpoint for the harness model and, separately, the judge.

## Run

```bash
# full 220-task run
bash environments/benchmarks/gdpval/run_eval.sh

# smoke test -- 3 tasks
bash environments/benchmarks/gdpval/run_eval.sh --env.limit 3

# one sector, 4 at a time
bash environments/benchmarks/gdpval/run_eval.sh \
    --env.sector_filter "Professional, Scientific" \
    --env.max_concurrent_tasks 4

# specific tasks
bash environments/benchmarks/gdpval/run_eval.sh --env.task_filter "83d10b06,0a1b2c3d"
```

## How a task is run

1. `_materialize()` creates `work/<task_id>/` and symlinks the task's
   reference files in under their original names (never copied -- the release is
   1.6 GB and tasks share files).
2. `register_task_env_overrides(rollout_id, {"cwd": workspace})` points the
   terminal tool at that directory for this rollout only.
3. `DAClawAgentLoop` runs with the configured toolsets.
4. Every file the agent created (i.e. not one of the seeded references) is treated
   as a deliverable and rendered to text: xlsx sheets and cells, docx/pptx XML text,
   pdf text, csv/md/ipynb verbatim, binary types by name and size.
5. `gdpval_grader.grade_task()` scores it against the rubric.
6. The record is appended to the streaming JSONL (crash-safe) and written to
   `evals/tasks/<task_id>.json`; the workspace is removed unless
   `--env.keep_workspaces` is set.

## Scoring

Per rubric item the deliverable is either satisfied (award the item's points) or not.
The final score is `sum(earned) / sum(possible) * 100`.

Criteria that name an exact filename, extension, or worksheet are resolved by rule
(`rule_check()`) -- deterministic and free. Everything else goes to the judge in
batches of `judge_batch_size` items, which must answer with
`{"verdicts": [{"id", "satisfied", "reason"}]}`.
A judge failure scores the item 0 with the error recorded, so a dead endpoint
degrades rather than crashes.

## Outputs

```
environments/benchmarks/gdpval/evals/
  samples_<ts>.jsonl        # one JSON line per finished task (crash-safe)
  tasks/<task_id>.json      # full record: conversation, per-item rubric verdicts
  summary_gdpval_<ts>.json  # aggregate metrics + per-task scores
logs/gdpval_<ts>.log        # stdout of the run
```

## Runtime notes (learned the hard way)

- **\`task_timeout\` must cover grading, not just the agent.** The judge runs inside
  the same per-task budget; with 30-40 rubric items on a slow reasoning model a
  graded task can take 4-6 extra minutes. 1800s is a sane default, 900 is not.
- **Keep toolsets to \`terminal\` + \`file\`.** The \`data\` toolset ships
  \`analysis_audit\`, which issues its own LLM call and was measured at ~360s per
  invocation -- it burned a third of a rollout before timing out.
- **\`data_dir_to_save_evals\` must be set** or atroposlib skips \`evaluate_log()\`
  entirely (only a warning is printed).
- **Tokenizer:** atroposlib constructs \`AutoTokenizer\` even in eval mode. This
  module forces \`HF_HUB_OFFLINE=1\`; make sure \`gpt2\` is in the local HF cache
  (one-time fetch: \`transformers.AutoTokenizer.from_pretrained("gpt2")\` with
  network access).
- **Local-backend cwd:** \`tools/environments/base.py:init_session()\` used to
  capture the *host process* cwd and overwrite the cwd the environment was
  constructed with, which made every per-task \`cwd\` override a no-op on
  \`TERMINAL_ENV=local\` (agents then hunted the filesystem for their inputs).
  Fixed by cd-ing into \`self.cwd\` before capturing \`pwd\`. Container backends are
  unaffected (\`cd\` there is a no-op or fails soft).

## Caveats

- `terminal_backend` must stay `local`: the workspace is built from host
  symlinks, and uploading the reference set per task would be prohibitive. Tasks
  therefore share the host filesystem -- run only trusted prompts, or isolate the
  whole run in a container/VM.
- The harness appends a short note to each prompt telling the agent that the
  referenced files are in its working directory and where to write the deliverable.
  This is a harness affordance, not part of the original task text.
- Judge scores are model-dependent. Pin `judge_model` when comparing runs.
