# GAIA2 on DAClaw harness ③

Runs GAIA2 scenarios (Meta Agents Research Environments, ARE) with
`environments/agent_loop.py::DAClawAgentLoop` as the agent and ARE's own
`GraphPerEventJudge` as the scorer. ARE's environment, events, notifications
and judge run in the same process as the agent.

## Files

| file | role |
|---|---|
| `are_bridge.py` | ARE import path, AppTool → `tools/registry.py` bridge, `AREChatServer` (server object for the loop) |
| `run_scenario.py` | one scenario: preprocess (oracle run + judge), start env, per-turn loop, validate, export |
| `bench.py` | `sample` / `run` / `summary` CLI |
| `default.yaml` | config (dataset, seed, model, judge, limits) |
| `rejudge.py` | offline re-judge of a finished run from its saved ARE traces (patch off and on) |

## Setup on 137 (done once)

ARE is **not** installed into `~/DAClaw/.venv`:

```bash
# ARE + only the packages missing from the DAClaw venv, no dependency resolution
uv pip install --python ~/DAClaw/.venv/bin/python --target ~/gaia2_bench/are_site \
    --no-deps meta-agents-research-environments==1.2.0 -r ~/gaia2_bench/scripts/extra_pins.txt
# FileSystem app data (hf-mirror rate-limits the lazy per-file loading, 429)
snapshot_download("meta-agents-research-environments/gaia2_filesystem", repo_type="dataset",
                  local_dir="~/gaia2_bench/gaia2_filesystem")
```

`are_site` is **appended** to `sys.path` at runtime, so every package that already
exists in the DAClaw venv keeps its pinned version (e.g. huggingface-hub 1.4.1,
openai 2.24.0, pydantic 2.12.5 instead of ARE's pins). `DEMO_FS_PATH` points to
the local filesystem copy. A separate `~/gaia2_bench/are_venv` holds a normal ARE
install used only for the official `are-benchmark` sanity run.

## Run

```bash
cd ~/DAClaw
.venv/bin/python -m environments.benchmarks.gaia2.bench sample
.venv/bin/python -m environments.benchmarks.gaia2.bench run --name <run_name>
```

The key is read from `DEEPSEEK_API_KEY` in `~/.daclaw/.env` and is never written
to results or logs. Output: `~/gaia2_bench/runs/<run_name>/` with `config.json`
(includes sampled ids), `summary.csv`, and per scenario `result.json`,
`messages.json`, `run.log`, `are_trace/`.

## How the bridge works

- **Tools**: every `AppTool` of the scenario (`scenario.get_tools()`, minus the 4
  AUI read tools ARE's default agent also removes) is registered under toolset
  `gaia2-are`; the handler calls the bound app method, so ARE's
  `@event_registered` logs it as an AGENT event for the judge. Schemas are built
  from ARE's arg type strings (`list[str] | None` → array, etc.). Tools are
  deregistered after the scenario. 101 tools per mini scenario, passed as-is.
- **Turns**: ARE scenarios can have several turns. The runner wakes the agent
  whenever ARE has a due user message **or environment notification** (e.g. a
  reply email arriving), like ARE's default agent, runs `DAClawAgentLoop` on the
  shared conversation, and ends the turn when the agent calls
  `AgentUserInterface__send_message_to_user` (same termination as ARE). ARE's
  turn trigger then judges the turn online and either releases the next turn's
  events or stops the env.
- **Plain-text final reply**: with function calling the model can end by
  answering in plain text instead of calling `send_message_to_user`; ARE's
  ReAct agent cannot (that is an invalid action and it is told to retry). The
  runner treats it the way ARE's agent treats an output without a valid
  action: it appends ARE's own error message (`InvalidActionAgentError`,
  category `AgentError`, rendered with base_agent's `"[OUTPUT OF STEP i]
  ERROR: ... Now let's retry ..."` template; "did not contain Action: or
  Thought:" becomes "did not contain a tool call") and the agent continues, up
  to `agent.text_reply_nudges` times (default 10 = ARE's
  `invalid_format_retries`, `base_agent.py:347`). The message says nothing
  about the answer. As in ARE, the failed call counts as an iteration
  (`execute_agent_loop` increments `iterations` in `finally`, so it counts
  towards the 80-call limit). Not reproducible: ARE first silently re-samples
  outputs that contain neither `Thought:` nor `Action:`; with function
  calling there is no such unparseable output. If the model still answers in plain text the turn
  ends without a message to the user, as in ARE. `agent.forward_text_reply:
  true` would instead deliver that text to the user; it is off by default
  because ARE's agent has no such fallback (it would score higher than ARE).
- **Status**: every `result.json` has `status`. `ok` = the agent ran and the
  judge scored it (`success` is the result). `error` = infrastructure or judge
  failure — agent LLM unreachable after 4 retries (`agent_api_error: ...`),
  empty response, judge exception, scenario load failure; `success` is `null`.
  `timeout` = `run.scenario_timeout` exceeded. `bench.py run` with the same
  `--name` reruns non-`ok` scenarios and skips `ok` ones; `summary.csv` and the
  printed pass rate count `ok` scenarios only. (`DAClawAgentLoop` itself
  swallows API errors and just returns, so this is tracked in `AREChatServer`.)
- **Notifications / time**: before every LLM call `AREChatServer` drains due
  messages from ARE's notification queue and appends them with ARE's own
  history templates ("User messages updates:" / "Environment notifications
  updates:"). The simulated clock is paused during generation and resumed with
  0 s offset, which is what the official run does with the LiteLLM engine (it
  reports no completion_duration). `SystemApp__wait_for_notification` jumps
  the clock to the next event, unchanged ARE code.
- **System prompt**: ARE's general + environment instructions, notification
  policy and date, with the ReAct JSON action-format section replaced by one
  line saying tools come via function calling.
- **Reasoning**: DeepSeek returns `reasoning_content`; the loop stores it on
  the assistant message and DeepSeek accepts it back (checked both with and
  without it).

## Differences from the official GAIA2 setup

1. **Agent harness**: DAClawAgentLoop (OpenAI function calling) instead of
   ARE's ReAct JSON agent. System prompt differs as described above.
2. **No `DAClawAgentBaseEnv`**: it imports `atroposlib`, which is not in the
   server venv (it is in the `rl` extra, pinned to `DA-Open/atropos`, not
   publicly fetchable). The loop is driven directly with a thin server adapter
   that is called the same way the base env calls it.
3. **Judge model**: deepseek-v4-pro instead of the default
   Llama-3.3-70B-Instruct. Scores are not comparable to the leaderboard.
4. **Scale**: 3 scenarios from `mini` (validation), seed 20261103, 1 run each
   (official: full configs, 3 runs).
5. **Limits**: 80 LLM calls per user turn (= ARE `max_iterations`), 3600 s
   wall clock per scenario (official 1860 s). Time-capability scenarios keep
   ARE's 420 s simulated duration cap. The wall-clock limit is an
   `asyncio.wait_for`; the blocking wait for the next turn's events
   (`run.turn_wait_timeout`, 900 s) cannot be interrupted by it, so a scenario
   can overrun the limit by up to that much.
6. **Dependencies**: DAClaw venv versions instead of ARE's pins (see Setup).
7. **Same model as agent and judge** (deepseek-v4-pro): possible self-preference
   bias.
8. **Tool results** longer than DAClawAgentLoop's 100k-character limit are
   truncated to a preview by the loop (can only lower scores).

## Known issue: ARE 1.2.0 judge + LiteLLM (case bug)

- `are/simulation/agents/llm/litellm/litellm_engine.py:125` rewrites
  `True`→`true` and `False`→`false` in **every** model output, including the
  judge's.
- `are/simulation/validation/utils/llm_utils.py` (`build_llm_checkers`, ~l.164–233):
  `signature_checker`, `sanity_checker` and `tone_checker` use
  `success_str="[[True]]"` / `failure_str="[[False]]"` and `LLMChecker.__call__`
  matches them case-sensitively, so with a LiteLLM judge they always return `None`.
  (content/cab/email/message/user_message/event checkers use `[[Success]]`/
  `[[Failure]]` and are unaffected.)
- `are/simulation/validation/tool_judge.py` `SoftToolJudge.compare()` does
  `if not checker_fn(...): return False`, so `None` = reject.
- Per `validation/constants.py` (`PER_TOOL_TO_SOFT_CHECKER_TYPES`, ~l.374–400)
  this rejects every non-identical `AgentUserInterface__send_message_to_user`
  (sanity) and every non-identical email send/reply / message (signature, tone).
- The official `are-benchmark` has the same behaviour whenever the judge runs
  through LiteLLM (provider `local`, `openai`-compatible, …). ARE's HF engine
  (`hf_engine.py:152`) does the same replacement, so the default official judge
  setup is very likely affected too; whether published scores were produced
  with the bug cannot be checked from here.

**Fix used here** (`are_bridge.patch_llm_checker`, judge-only monkeypatch, files
in `are_site` untouched): `LLMChecker.__call__` is replaced by the same voting
logic with case-insensitive matching of `success_str`/`failure_str`; agent-side
LiteLLM is not involved (the agent uses the OpenAI client). The patch also
records each checker's raw output (`judge_log`) and retries judge LLM calls on
network errors (LiteLLM reports connection failures as
`LLMEngineException: Auth error in litellm.`). Config switch:
`judge.fix_case_bug`, **default `false`** (ARE as shipped, the score comparable
to the official setup). Report the patched score next to it, never instead of
it; `rejudge.py` produces both from one run's saved traces without calling the
agent again.

## Results (mini validation, seed 20261103, deepseek-v4-pro agent + judge)

Interface check, **not a formal score**: 3 scenarios, 1 run each. The official
GAIA2 headline is the mean success rate over 3 runs per scenario (± SEM,
`benchmark/report_stats.py`; pass@k / pass^k are reported alongside), so these
numbers are not comparable to the leaderboard. Run 2026-11-04 on 137, all
three `status=ok`, no infrastructure errors.

| scenario | category | ARE judge as shipped (offline) | judge case fix (online) | judge case fix (offline) |
|---|---|---|---|---|
| scenario_universe_21_bdn1w8 | time, 2 turns | FAIL | PASS | PASS |
| scenario_universe_23_0wpjds | search | FAIL | PASS | PASS |
| scenario_universe_28_dlqvas | search | FAIL | FAIL | FAIL |
| **total** | | **0/3** | **2/3** | **2/3** |

- The first column is the setup comparable to the official one. With the
  unpatched judge every scenario fails on its first soft check
  (`signature_checker` / `sanity_checker` returns `None`), whatever the agent
  did: only answers identical to the oracle can pass.
- The run was executed with `judge.fix_case_bug: true` online so that the
  2-turn scenario could continue past turn 1 (the unpatched judge always
  rejects that turn and ARE then stops the environment). Both judge columns
  come from the same agent trajectories via `rejudge.py`; no agent output
  was regenerated.
- scenario_universe_21_bdn1w8 turn 2 was started by an environment
  notification (Amscan's reply email), i.e. the notification wake-up path.
- scenario_universe_28_dlqvas: the agent's answer named the wrong person
  (`user_message_checker` `[[Failure]]`).
- No invalid-action error message was needed in this run (0 in all turns).
- The soft checkers are LLM calls and not deterministic: in earlier runs the
  same `sanity_checker` input was accepted once and rejected once (it
  sometimes objects to the `content:` prefix that ARE's own judge prompt puts
  in front of the message).

Files: `results/` in this directory holds the small files of both runs
(`config.json`, `summary.csv`, `result.json`, rejudge JSON; absolute paths
shortened to `~`). `messages.json`, `run.log` and `are_trace/` stay in
`<bench_root>/runs/` on the machine that ran them.

- `final_mini_seed20261103/`: `config.json` (resolved config, sampled ids,
  notes), `summary.csv`, and per scenario `result.json` (status, pass/fail,
  rationale, per-turn info, `judge_log` with each checker's raw verdict,
  tokens, timings), `messages.json` (full agent conversation), `run.log`,
  `are_trace/` (ARE trace used by `rejudge.py`).
- `final_mini_seed20261103_rejudge/`: `summary.csv`, `rejudge_summary.json`,
  and per scenario `rejudge_orig.json` / `rejudge_fixed.json` with `judge_log`.
