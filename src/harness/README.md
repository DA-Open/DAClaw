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
| `tasks/` TaskService + worker | done | done (in-memory store until PostgreSQL lands; `python -m harness.worker`) | PR-C |
| `app.py` wiring, permissions file | — | done (`build_system`, `DARAOS_HARNESS_POLICY_FILE`) | PR-C |
| `trace.py` task timeline | — | done (`--trace`) | PR-C |
| `runtime/` AgentRunner, ModelAdapter, budget | done | done | PR-D |
| `context/` ContextBuilder | done | minimal (no memory, no token cut) | PR-D |
| `tools/` ToolGateway, Policy, connectors | done | done (P0: `files.read`, `artifacts.read`, `artifacts.write`) | PR-E |
| `results/` ArtifactStore, Verifier | done | done (`FileArtifactStore`, `CapabilityVerifier` with `artifact_readable`, `required_sections`) | PR-E / PR-F |
| `agents/`, `capabilities/` profiles, capability packs | done (AgentRegistry) | done (Main profile, `document_draft@1`) | PR-F |
| `api/` POST/GET/cancel tasks, SSE events, trace, artifacts | — | done (`python -m harness.api`; token auth; in-memory store until PostgreSQL is wired in) | PR-G |
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
| Write `model_call`, `context_built` and `budget_reminder` events | Runner | ModelAdapter |
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
| `EventType.CONTEXT_BUILT` + `ContextBuiltRecord` | Size of each part of every model input (system, task brief, history, tool results, reminders, tool specs), so a costly call can be explained from events alone |
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
  `result_ref` (decided 2026-11-12: spec 6.3 "待确认结果" means the user
  confirms); `failed` ends the task (no repair rounds, spec 6.3).
- A model call that got an HTTP error is released; one that got no answer
  (timeout) is charged at the reserved amount. Retryable failures are tried
  again up to `model_retries` times in a row, inside the same budget.

## Tool gateway

`tools/gateway.py` follows the five steps of spec 6.2 for every call:

1. **Same key seen before?** Same `logical_key` with other arguments raises
   `ActionConflict`; an earlier success is reused without running the tool
   (same `action_id`, stored summary and artifacts); an earlier `unknown`
   is answered `unknown` again, never re-run blindly.
2. **May it run?** Current lease (else `StaleLease`), no cancel request, not
   past the deadline, known tool, permission check, arguments valid against
   the tool's `input_schema`, file paths inside the allowed directories.
   Refusals here return `denied` (or `failed` for bad arguments) with no
   `action_id` and charge nothing.
3. **Register and reserve** one tool call in one step.
4. **Run the connector** with an `AuthorizedCall`. Just before running, the
   task is read again: a cancel that arrived after step 2 still stops the call
   (spec 4.4), releasing the reservation. The call is bounded by the task
   deadline. A crash or timeout in a read-only connector is `failed`; in any
   other connector it is `unknown` (a timeout does not mean it did not run).
5. **Settle and record**: usage is settled (an `unknown` keeps its
   reservation), the action gets its status, `args_ref` / `result_ref`, and a
   `tool_action` event is written.

**Permissions** (`tools/policy.py`, decided in the review of #12). Two
whitelists from config stand in for the grants table:

| Layer | Keyed by | Says |
|---|---|---|
| Role whitelist | `AgentProfile.tool_policy_ref` | which tools this kind of agent may call (the floor) |
| User whitelist | `scope_id` → role → `UserGrant` | which of those tools this user allows, and which directories `files.read` may read |
| Capability | `Task.capability_ref` | which tools the pack needs (narrows only) |
| Deployment | `disabled_tools` in the permissions file | tools the operator turned off for everyone (spec 7, the fourth layer) |

A call is allowed only by the intersection; a user entry can only narrow its
role list, and a scope without an entry gets nothing. Files named in a
task's `input_refs` get no exemption: whoever creates a task chooses its
`input_refs`, so they must sit in the user's directories like any other path
(review of #16). Paths are resolved first, so `../` and symlinks out of a
directory are refused. `PolicyToolCatalog` gives ContextBuilder exactly the
allowed tools, so the model never sees a tool it may not call.

**Connectors** (`tools/connectors/files.py`):

| Tool | Read-only | Notes |
|---|---|---|
| `files.read` | yes | text only, 5 MB cap, line paging; a page over 6,000 chars is stored as an artifact and returned as a preview plus its ID |
| `artifacts.read` | yes | this scope's artifacts, text / JSON only, line paging |
| `artifacts.write` | no | writes a named artifact of this task; no outside side effects |

None of them supports `reconcile` (they are local).

**ArtifactStore** (`results/artifacts.py`, `FileArtifactStore`): content goes
to `<root>/<scope_id>/<sha256[:2]>/<sha256>` with an atomic write (temp file +
rename), then the `artifacts` row is added. Identical content is one file but
each `put` is its own artifact. `read` checks the scope through the repository
first, then re-checks the content hash (a changed file raises
`ArtifactIntegrityError`). `root` comes from `HarnessConfig.artifact_root`;
object storage can replace it behind the same interface. Tool arguments and
results are stored as trajectory artifacts (`task_id` empty, name
`<task_id>/tool_args/<key>` and `<task_id>/tool_result/<key>`), like the
Runner's model inputs.

## Verification, capability packs, profiles

**Capability packs** (`capabilities/`). One YAML file per pack, fixed per task
by `capability_ref` (`document_draft@1`); `resolve` returns that exact
version. A pack lists the tools it needs (fed to
`WhitelistPolicy.capabilities` via `CapabilityRegistry.tool_map()`; it can
only narrow), the checkers it offers, an optional `result_schema` for the
`CandidateResult`, the name of its deliverable artifact (`result_artifact`),
and instructions that ContextBuilder adds to the system prompt. Adding a
scenario means adding a pack and, if needed, connectors (spec 7).

**Verifier** (`results/verifier.py`, `CapabilityVerifier`). Which checks run
comes from the task, never from the model:

| Check | Source | Fails when |
|---|---|---|
| `required_artifacts` | `acceptance.required_artifacts` (always) | a named artifact is missing from the result |
| `result_schema` | the pack (always, if it has one) | the result does not match the schema |
| `evidence_refs` | the result's `evidence_refs` and `required_action_receipts` (always, if any) | a reference is not a readable artifact of this scope or a succeeded action / receipt of this task |
| `required_actions` | `acceptance.required_actions` | never fails in P0; it is `inconclusive` (receipts cannot be checked yet) |
| `artifact_readable` | `acceptance.checks` | an artifact is missing, in another scope, not text, empty, or its file changed |
| `required_sections` | `acceptance.checks` with `sections` | a section is not a Markdown heading of the draft |

Overall status: any `failed` → `failed`; otherwise any `inconclusive`, an
unverified item, or a task with no acceptance conditions at all →
`inconclusive`; otherwise `passed`. A check the pack does not offer, a
checker that is missing or crashes, and an unknown pack are all
`inconclusive`, never `passed`. `required_sections` matches headings
outside code fences, ignoring leading numbering ("1.", "一、"), emphasis,
spaces and case; the heading must be the section name, not just contain it.
Every result is stored and a `verification_recorded` event is written;
`result_ref` is the pack's deliverable.

**Profiles** (`agents/`). `AgentRegistry` resolves `main@1` exactly;
`tool_policies()` feeds `ToolGateway(profiles=...)` and
`main_role_whitelist()` the role whitelist. `PackageInstructions` loads
`instructions_ref` paths relative to `src/harness` (refusing anything outside
it). Prompts live in `prompts/`; YAML, schema and prompts ship as package data.

Wiring: `app.build_system(config, model)` puts all real modules together
(repositories, `FileArtifactStore`, registries, `WhitelistPolicy`,
`ToolGateway`, `CapabilityVerifier`, `SimpleContextBuilder`, `AgentRunner`,
`TaskService`). The worker CLI, the API and the tests all use it, so there
is one place to change the wiring.

## Tasks and the worker

`TaskService` (`tasks/service.py`) is the only writer of task status. On
`create` it checks the limits against the deployment caps, that the
capability pack and the agent profile exist, and that every
`acceptance.checks[].id` is a verifier of that pack (`UnknownCheck`
otherwise, so a typo fails at submit time, not after the run). `checkpoint`
stores the checkpoint as a JSON artifact and points the task at it.

`Worker` (`tasks/worker.py`) runs one attempt per claim:

1. `claim` a queued task in its lanes (lease TTL `lease_ttl_sec`);
2. a background heartbeat every `heartbeat_interval_sec` extends the lease
   and notices a cancel request;
3. resolve the profile, build `ExecutionContext` and the context bundle,
   run `AgentRunner` with `lease_lost` wired to the heartbeat;
4. `commit` the outcome. If the lease was lost (`StaleLease` from a
   heartbeat or from the Runner) it commits nothing and reports
   `lease_lost`; the task stays `running` until the Scheduler (P1) requeues
   it. A missing profile or a context-build error is committed as `failed`
   (`internal`) so the task does not hang.

```bash
python -m harness.worker --once --submit task.json --model-config model.yaml --print-result
python -m harness.worker --once --submit task.json --scripted script.json   # no network
```

Until the PostgreSQL repositories land, the store is in memory, so
`--submit` creates the task in the same process (as the API would, role
`main`, trusted origin). `model.yaml` holds `ModelConfig` fields; the key
comes from the environment variable named by `api_key_env`. The command
prints one JSON line and exits 0 when the attempt was committed.

`--trace` prints the task's timeline to stderr (`--trace FILE.json` writes
it as JSON): each status change, context size, model call (tokens, latency,
stored input), tool action, refused tool call, verification and budget
reminder, then usage against the limits. It is built by `trace.py` from the
records already written and calls no model. Refused calls have no action by
design; the trace takes them from the tool message the model saw next, and
copies only the status and error line. With the in-memory store it covers
the current process only; with PostgreSQL the same `build_trace` works for
any past task.

**Permissions file** (`DARAOS_HARNESS_POLICY_FILE`, read by
`app.load_policy_file`; unknown keys or roles are refused at start):

```yaml
disabled_tools: []            # deployment policy: off for everyone
users:                        # user whitelist, per scope_id
  s1:
    main:
      paths: [/data/s1]       # directories files.read may read
      tools: [files.read, artifacts.read, artifacts.write]   # optional, narrows the role list
      ref: grant:s1:main      # optional, recorded on each action
```

With no file, no scope has any tool.

## HTTP API

`api/app.py`, run with `python -m harness.api` (spec 5.1; P0 subset):

| Route | Does |
|---|---|
| `POST /tasks` | `Idempotency-Key` header required; 202 with `task_id`, `agent_role`, `run_lane`, `status`, `events_url` |
| `GET /tasks/{id}` | status, wait reason, usage, remaining budget, `result_ref`, error |
| `GET /tasks/{id}/events` | SSE; `Last-Event-ID` resumes; one `final` event after the task stops (succeeded, failed, cancelled or waiting); a dropped client does not cancel the task |
| `POST /tasks/{id}/cancel` | idempotent; body `{"reason": "..."}` optional |
| `GET /tasks/{id}/trace` | `trace.build_trace` JSON |
| `GET /artifacts/{id}` | content, `offset` / `length` byte range (5 MB max per call) |

Not yet: resume, approvals, `POST /events`, subscriptions (P1/P2).

**Identity** comes only from the bearer token. The permissions file lists
callers by the token's sha256 (the token itself is never stored):

```yaml
tokens:
  - sha256: <printf %s "$TOKEN" | sha256sum>
    scope_id: s1
    actor_id: u1
```

With no tokens every request is 401, so the API is closed by default. The
token is checked before the body is read. The body may not carry
`scope_id`, `actor_id`, `agent_role` or `run_lane` (400): the role is always
`main`, and `prefer_background` only picks the lane. Another scope's task or
artifact is 404, the same answer as a missing one. Files in `input_refs`
must sit in the caller's whitelisted directories (400 at submit time).

**Errors** all have `{error_code, message, retryable, request_id}` and an
`X-Request-ID` header: 400 bad body, unknown capability or profile, unknown
check, limits over the caps; 401 token; 404 not found or not yours; 409
same key with other content; 500 `internal` only, details in the log.

**Running it.** The API never runs model or tool calls in a request. Until
PostgreSQL is wired in (item 2b) tasks live in the process's memory, so a
worker runs in the same process:

```bash
python -m harness.api --port 8090 --with-worker --model-config model.yaml
```

It binds 127.0.0.1 by default.

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
| Who calls `TaskService.checkpoint` | Implemented (JSON artifact + `checkpoint_ref`); the Runner does not write checkpoints yet, and resume reads none | P1 |
| `grants` / `approvals` tables | P0 has no tables; `tools/policy.py` uses the two whitelists described under "Tool gateway". Tools with outside side effects are refused with `needs_approval` until approvals exist | P1 |
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
