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
| `runtime/` AgentRunner, ModelAdapter, budget | done | not started | PR-D |
| `context/` ContextBuilder | done | not started | PR-D |
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
| Generate `logical_key` | Runner, from the durable step ID | the model, Gateway |
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

## Open items

| Item | Decided | When |
|---|---|---|
| Expired leases go back to `queued` (crashed worker) | Scheduler does it; until then such a task stays `running` | P1 |
| `TaskService.checkpoint` implementation | Interface and repository method exist; nothing calls it yet | P1 |
| `grants` / `approvals` tables | P0 has no tables. ToolGateway checks two whitelists from config and allows only their intersection: a **role whitelist** (tools Main / Proactive may call; the floor) and a **user whitelist** (per `scope_id` and role, which resources; covers several users on one deployment). A user entry can only narrow its role list. The tables replace the user whitelist later | PR-E; tables P1 |
| ProactiveController, DeliveryService, subscriptions, outbox | Not started; their types come with them | P2 |
| Memory beyond the spec's minimal MemoryStore | Waiting on the stability question in the spec review | after P0 |
| PostgreSQL driver and migrations | `asyncpg` driver, SQLAlchemy 2.0 Core (async) for queries, tables defined once in `storage/schema.py`, Alembic (async template) for migrations; tests start a `postgres:16` container. SQLAlchemy stays on 2.0.x | PR-B |

## Tests

```bash
.venv/bin/python -m pytest tests/harness_runtime -q
```
