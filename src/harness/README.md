# DaraOS harness runtime

A general agent runtime written to the group spec "Harness 架构与开发说明"
(2026-11-10): Main and Proactive roles share one TaskService, AgentRunner,
ToolGateway, Verifier and DeliveryService. It is developed next to the existing
hermes loop (`run_agent.py`), which it does not change.

This README is the agreement between the people working on the runtime. If a
rule here and the code disagree, fix one of them in the same PR.

## Status

| Module (spec section 2) | Interface | Implementation | PR |
|---|---|---|---|
| `contracts.py` shared types, task state machine | — | done | PR-A |
| `config.py` (env `DARAOS_HARNESS_*`) | — | done | PR-A |
| `interfaces.py` module Protocols (spec 5.2) | done | — | PR-A |
| `storage/` repositories | done | in-memory done; PostgreSQL + migrations next | PR-A / PR-B |
| `tasks/` TaskService + worker | done | not started | PR-C |
| `runtime/` AgentRunner, ModelAdapter, budget | done | done | PR-D |
| `context/` ContextBuilder | done | minimal (no memory, no token cut) | PR-D |
| `tools/` ToolGateway, Policy, connectors | done | not started | PR-E |
| `results/` ArtifactStore, Verifier | done | not started | PR-F |
| `agents/`, `capabilities/` profiles, capability packs | done (AgentRegistry) | not started | PR-F |
| `api/` POST/GET/cancel tasks | — | not started | PR-G |
| P0 acceptance scenarios | — | not started | PR-H |
| `delivery/`, ProactiveController, subscriptions (P2) | not yet | not started | — |

## Who does what

Each row has exactly one owner. A module that is not the owner does not do it.

| Responsibility | Owner | Not |
|---|---|---|
| Change task status | `TaskService` via `TaskRepository` | Runner, Gateway, API |
| Attempt lifecycle (`worker_id`, lease, profile/capability versions, `ended_at`, `exit_reason`) | `TaskRepository.claim` / `commit` | Runner |
| Attempt run record (`model_version`, `context_ref`) | Runner, via `TaskRepository.record_run` (spec 3: "Runner 提交运行记录") | TaskService, ModelAdapter |
| Choose `agent_role`, `run_lane`, trigger | Trusted entry (API, ProactiveController) via `TaskOrigin` | `TaskRequest`, the model |
| Set `deadline` | `TaskRepository.claim`, on the first claim; later claims keep it | Runner, API |
| Assemble the model input | `ContextBuilder` | Runner |
| Reserve and settle **model** usage | Runner: `UsageRepository.reserve` before each call, `settle` after | ModelAdapter, `commit` |
| Reserve **tool** usage | ToolGateway: `ActionRepository.register(..., reserve=...)` in one step | Runner, Connector |
| Generate `logical_key` | Runner, from the durable step number (`step-<n>.<call>`, counted over the whole task, never the attempt ID) | the model, Gateway |
| Check grants and approvals | ToolGateway (`tools/policy.py`) | Runner, Connector |
| Call external services | Connector, only from an `AuthorizedCall` | anything else |
| Write `model_call` and `budget_reminder` events | Runner | ModelAdapter |
| Write `tool_action` events, update `actions` | ToolGateway | Runner |
| Write `task_created` / `task_status_changed` events | `TaskRepository`, in the same transaction as the status change | anyone else |
| Decide passed / failed / inconclusive | Verifier | the model, Runner |
| Store content | ArtifactStore (file first, then row); everyone else keeps refs | database rows, events |

## Rules the code follows

- Status changes follow `contracts.TASK_TRANSITIONS` (spec 4.3).
- A task becomes `succeeded` only with a passed verification of the same
  `result_ref`; `failed` keeps the artifacts made so far.
- `Limits` accumulate over the whole task; a new attempt does not reset usage
  or the deadline. Each charge is reserved then settled once per `call_id`.
- Every read by ID also checks `scope_id`.
- Model, tool and network calls never run inside a storage transaction.
- Secrets never appear in task arguments, events, logs or model context; only
  credential references do.

## How to add to this

**Where a new type goes**
- Used by one module only: put it in that module.
- Used across modules, or stored in a table: `contracts.py`.

**Changing shared files.** `contracts.py`, `interfaces.py` and
`storage/repositories.py` are what everyone builds on. A change to them goes
in its own small PR, separate from feature work, and is merged first; other
branches rebase onto it. Adding an optional field is fine; renaming or
removing one needs the other people working on the runtime to agree in that
PR.

**A new storage implementation** must pass `tests/harness_runtime/unit/test_repositories.py`:
add it to the `repos` fixture params.

**Tests** live in `tests/harness_runtime/{unit,integration,scenarios}`. Not
`tests/harness`: several older tests put `tests/` on `sys.path`, and a
`tests/harness` package would hide `src/harness`. Async test modules need
`pytestmark = pytest.mark.asyncio` (the repo runs pytest-asyncio in strict
mode). Database tests carry the `integration` mark so the default run skips
them.

## Where this differs from the spec

**Fields and types the spec does not have.** Added in the first version so the
tables do not change later; most come from review comments on the spec.

| Field / type | Why |
|---|---|
| `Action.read_only` | The Verifier can replay read-only calls to check numbers in a conclusion |
| `EventType.MODEL_CALL` + `ModelCallRecord` | Exact model input/output per call, for trajectory export and training data |
| `EventType.BUDGET_REMINDER` + `BudgetReminderRecord` | Reminders before the budget runs out are part of the trajectory |
| `ContextBundle.injected_materials` + `InjectedMaterial` | Short (~100 token) pointers to background results for Main (P2) |
| `AdmissionDecision` | Proactive admission decisions with policy version, input snapshot and later label (P2) |
| `UsageReservation` (`usage_records` table) | The spec asks for idempotent accounting per call ID but names no table |
| `Task.lease_epoch`, `Task.current_attempt_id` | Reject a stale worker by checking one row |
| `RemainingBudget.for_task` | One place that turns a task into its remaining budget, time included |

**Signatures that differ from spec 5.2** (the reason is in each docstring in
`interfaces.py`):

- `TaskService.create` takes `origin` as well, so the role comes from the caller.
- `TaskService.heartbeat` returns the task, so a worker sees a cancel request.
- `TaskService.get` and `request_cancel` are added for the API.
- `AgentRunner.run` takes `ExecutionContext` (lease + actor + grants) instead of the bare lease.
- `ArtifactStore.read` takes optional `offset` / `length` for paging.
- `AgentRunner.run` (implementation) takes an optional `lease_lost` callback:
  the Worker's heartbeat result, so the Runner stops when the lease is gone.
- `ModelAdapter.next_step` may take an optional `max_output_tokens`, so the
  Runner can keep the answer inside the remaining token budget. The Runner
  checks the signature; an adapter written to the spec's signature still works.
- `TaskRepository.record_run` / `get_attempt` are added so the Runner can
  write the attempt's `model_version` and `context_ref` (spec 3).

**Runner behaviour the spec does not spell out** (PR-D):

- The model ends its work by calling `submit_result` (or answering in plain
  text); the adapter turns that into a `candidate_result` step. It never
  reaches ToolGateway.
- Wrap-up turn: `BudgetReminderConfig.reserve_model_calls` (default 1) model
  calls are kept for returning the result. On that turn only `submit_result`
  is offered. Set it to 0 to turn this off.
- Step numbers (and so `logical_key`s and model usage `call_id`s) continue
  across attempts, counted from earlier `model_call` events. Replaying a
  step from a checkpoint with its original number is P1.
- `inconclusive` verification ends in `waiting` / `input` without
  `result_ref`; `failed` ends the task (no repair rounds, spec 6.3).
- A model call that got an HTTP error is released; one that got no answer
  (timeout) is charged at the reserved amount. Retryable failures are tried
  again up to `model_retries` times in a row, inside the same budget.

## Model adapters

`runtime/models/`. One adapter per wire protocol; a provider preset fills in
the endpoint, protocol and key variable. Any preset value can be overridden.

| Protocol | Adapter | Presets |
|---|---|---|
| `openai_chat` (Chat Completions) | `OpenAIChatAdapter` | gemini, deepseek, qwen, kimi, glm, doubao, xai, mistral, openrouter, siliconflow, ollama, vllm, custom |
| `openai_responses` | `OpenAIResponsesAdapter` | openai |
| `anthropic_messages` | `AnthropicMessagesAdapter` | anthropic, minimax |

```python
from harness.runtime.models import ModelConfig, create_adapter

create_adapter(ModelConfig(provider="deepseek", model="deepseek-chat"))
create_adapter(ModelConfig(provider="custom", model="my-model",
                           base_url="https://relay.example.com/v1", api_key_env="RELAY_API_KEY"))
create_adapter(ModelConfig(provider="custom", model="claude-x", protocol="anthropic_messages",
                           base_url="https://relay.example.com", api_key_env="RELAY_API_KEY"))
```

Or from the environment: `DARAOS_HARNESS_MODEL_{PROVIDER,NAME,PROTOCOL,BASE_URL,API_KEY_ENV,...}`
(see `ModelConfig.from_env`). The key is read from the variable named by
`api_key_env` when the adapter is built and is never stored or logged.

The transcript in `ContextBundle.messages` is provider-neutral
(`runtime/messages.py`), so a task can change model between attempts. What
a protocol needs sent back unchanged (Anthropic thinking signatures,
Responses reasoning items, DeepSeek/Kimi `reasoning_content`) is kept in the
message's `native` field and replayed only to the same protocol.

`ScriptedModelAdapter` is the fake model for tests. `scripts/harness_runner_smoke.py`
runs one real model through the Runner with fake tools, for a manual check.

## Open items

| Item | Decided | When |
|---|---|---|
| Expired leases go back to `queued` (crashed worker) | Scheduler does it; until then such a task stays `running` | P1 |
| `TaskService.checkpoint` implementation | Interface and repository method exist; nothing calls it yet | P1 |
| `grants` / `approvals` tables | P0 has no tables. ToolGateway checks two whitelists from config and allows only their intersection: a **role whitelist** (tools Main / Proactive may call; the floor) and a **user whitelist** (per `scope_id` and role, which resources; covers several users on one deployment). A user entry can only narrow its role list. The tables replace the user whitelist later | PR-E; tables P1 |
| ProactiveController, DeliveryService, subscriptions, outbox | Not started; their types come with them | P2 |
| Memory beyond the spec's minimal MemoryStore | Waiting on the stability question in the spec review | after P0 |
| Wrap-up turn (keep one model call for returning the result) | Implemented, on by default; needs the spec author's OK | PR-D review |
| ContextBuilder token cut, memory, resume from checkpoint | P0 builder lists goal, acceptance, inputs and artifacts only | P1 |
| Parallel tool calls in one step | Run one after another in P0 | later |
| PostgreSQL driver and migrations | `asyncpg` driver, SQLAlchemy 2.0 Core (async) for queries, tables defined once in `storage/schema.py`, Alembic (async template) for migrations; tests start a `postgres:16` container. SQLAlchemy stays on 2.0.x | PR-B |

## Tests

```bash
.venv/bin/python -m pytest tests/harness_runtime -q
```
