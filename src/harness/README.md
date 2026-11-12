# DaraOS harness runtime

A general agent runtime written to the group spec "Harness 架构与开发说明"
(2026-11-10): Main and Proactive roles share one TaskService, AgentRunner,
ToolGateway, Verifier and DeliveryService. It is developed next to the existing
hermes loop (`run_agent.py`), which it does not change.

## Status

| Module (spec section 2) | State |
|---|---|
| `contracts.py` shared types, task state machine | done |
| `config.py` (env `DARAOS_HARNESS_*`) | done |
| `storage/repositories.py` interfaces | done |
| `storage/memory.py` in-memory implementation | done |
| `storage/` PostgreSQL implementation and migrations | next |
| `tasks/`, `context/`, `runtime/`, `tools/`, `results/`, `agents/`, `capabilities/`, `api/` | not started |
| `delivery/`, ProactiveController, subscriptions (P2) | not started |

## Rules the code follows

- Only trusted entry points set `agent_role`, `run_lane` and `trigger_type`
  (`TaskOrigin`); a `TaskRequest` cannot carry them.
- Task status changes go through `contracts.TASK_TRANSITIONS` and are made
  only by the task repository, together with a `task_status_changed` event.
- A task becomes `succeeded` only with a passed verification of the same
  `result_ref`.
- Budgets (`Limits`) accumulate over the whole task; a new attempt does not
  reset them. Every charge is reserved, then settled, once per `call_id`.
- Every read by ID also checks `scope_id`.

## Additions to the spec

These fields are not in the spec. They are in the first version so that the
tables do not have to change later.

| Field / type | Why |
|---|---|
| `Action.read_only` | The Verifier can replay read-only calls to check numbers in a conclusion |
| `EventType.MODEL_CALL` + `ModelCallRecord` | Exact model input/output per call, for trajectory export and training data |
| `EventType.BUDGET_REMINDER` + `BudgetReminderRecord` | Reminders before the budget runs out are recorded in the trajectory |
| `ContextBundle.injected_materials` + `InjectedMaterial` | Short (≈100 token) pointers to background results for Main (P2) |
| `AdmissionDecision` | Proactive admission decisions with policy version, input snapshot and later label (P2) |
| `UsageReservation` (`usage_records` table) | The spec asks for idempotent accounting per call ID but names no table |
| `Task.lease_epoch`, `Task.current_attempt_id` | Reject a stale worker by checking one row |

## Tests

```bash
.venv/bin/python -m pytest tests/harness_runtime -q
```
