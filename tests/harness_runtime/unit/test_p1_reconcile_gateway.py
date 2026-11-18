"""R3 reconciliation determines outcomes without invoking external writes."""

import asyncio

import pytest

from harness.contracts import (
    Action, ActionStatus, Event, EventType, HarnessError, ReconcileResult, TaskOutcome, TaskStatus,
    ToolStatus, Usage, UsageKind, WaitCondition,
)
from harness.storage.memory import InMemoryRepositories
from harness.tools import ToolGateway, WhitelistPolicy
from harness.tools.gateway import reconciliation_history

from ..fakes import ACTOR, Clock, FakeExternalConnector, MemoryArtifactStore, claimed_task

pytestmark = pytest.mark.asyncio


async def setup(result, *, reserve=True):
    repos = InMemoryRepositories()
    clock = Clock()
    store = MemoryArtifactStore(repos)
    task, execution = await claimed_task(repos)
    connector = FakeExternalConnector(result)
    gateway = ToolGateway(repos, store, WhitelistPolicy(roles={}, users={}, capabilities={}),
                          [connector], profiles={}, clock=clock)
    action = Action(task_id=task.id, attempt_id=execution.lease.attempt_id, logical_key="step-1.0",
                    tool=connector.spec.name, request_hash="request", read_only=False,
                    status=ActionStatus.UNKNOWN, created_at=clock(), updated_at=clock())
    await repos.actions.register(action, reserve=Usage(tool_calls=1) if reserve else None)
    task = await repos.tasks.commit(execution.lease, TaskOutcome(status=TaskStatus.WAITING,
                                   wait_condition=WaitCondition.RECONCILIATION), clock())
    return repos, gateway, connector, store, clock, task, action


async def test_reconcile_succeeded_settles(tmp_path):
    repos, gateway, connector, store, clock, task, action = await setup(
        ReconcileResult(status="succeeded", receipt_ref="external:receipt", detail="observed remote result")
    )
    assert await gateway.reconcile(task, action, 1) is ActionStatus.SUCCEEDED
    actual = await repos.actions.get(task.id, action.logical_key)
    assert actual.status is ActionStatus.SUCCEEDED and actual.receipt_ref == "external:receipt"
    assert (await repos.tasks.get(task.scope_id, task.id)).usage.tool_calls == 1
    assert await repos.usage.open_reservations(task.id) == []
    (event,) = await reconciliation_history(repos.events, task)
    assert event.payload == {"action_id": action.id, "status": "succeeded", "attempt": 1, "actor": None}
    assert connector.reconciled_actions[0].action_id == action.id
    assert connector.reconciled_actions[0].request_hash == action.request_hash
    assert connector.reconcile_calls == 1 and connector.execute_calls == 0


async def test_reconcile_failed_marks_failed():
    repos, gateway, connector, store, clock, task, action = await setup(ReconcileResult(status="failed"))
    assert await gateway.reconcile(task, action, 1) is ActionStatus.FAILED
    assert (await repos.actions.get(task.id, action.logical_key)).status is ActionStatus.FAILED
    assert await repos.usage.open_reservations(task.id) == []
    assert (await repos.tasks.get(task.scope_id, task.id)).usage.tool_calls == 0
    assert connector.execute_calls == 0


@pytest.mark.parametrize("error_type", [HarnessError, OSError])
async def test_reconcile_named_error_is_recorded(error_type):
    repos, gateway, connector, store, clock, task, action = await setup(ReconcileResult(status="unknown"))

    async def fail(reference):
        raise error_type("remote unavailable")

    connector.reconcile = fail
    assert await gateway.reconcile(task, action, 1) is ActionStatus.UNKNOWN
    (event,) = await reconciliation_history(repos.events, task)
    assert event.payload["status"] == "unknown"
    assert event.payload["error"] == {"type": error_type.__name__, "message": "remote unavailable"}
    assert len(await repos.usage.open_reservations(task.id)) == 1
    assert connector.execute_calls == 0


@pytest.mark.parametrize("status", ["unknown", "unsupported"])
async def test_reconcile_unsettled_keeps_reservation(status):
    repos, gateway, connector, store, clock, task, action = await setup(ReconcileResult(status=status))
    assert await gateway.reconcile(task, action, 1) is ActionStatus.UNKNOWN
    assert (await repos.actions.get(task.id, action.logical_key)).status is ActionStatus.UNKNOWN
    assert len(await repos.usage.open_reservations(task.id)) == 1
    (event,) = await reconciliation_history(repos.events, task)
    assert event.payload["status"] == status and event.payload["attempt"] == 1
    assert connector.reconcile_calls == 1 and connector.execute_calls == 0


@pytest.mark.parametrize("happened", [True, False], ids=["happened", "not_happened"])
async def test_resolve_records_actor_and_usage(happened):
    repos, gateway, connector, store, clock, task, action = await setup(ReconcileResult(status="unknown"))
    expected = ActionStatus.SUCCEEDED if happened else ActionStatus.FAILED
    assert await gateway.resolve(task, action, happened, ACTOR, 1) is expected
    actual = await repos.actions.get(task.id, action.logical_key)
    assert actual.status is expected
    if happened:
        assert actual.receipt_ref
        receipt = store.json(actual.receipt_ref)
        assert receipt["actor"] == ACTOR.actor_id and receipt["outcome"] == "happened"
    assert await repos.usage.open_reservations(task.id) == []
    assert (await repos.tasks.get(task.scope_id, task.id)).usage.tool_calls == int(happened)
    (event,) = await reconciliation_history(repos.events, task)
    assert event.payload["actor"] == ACTOR.actor_id and event.payload["status"] == expected.value
    assert connector.reconcile_calls == connector.execute_calls == 0


@pytest.mark.parametrize("status", ["succeeded", "failed"])
async def test_reconcile_finishes_retry_reservation_only(status):
    repos, gateway, connector, store, clock, task, action = await setup(ReconcileResult(status=status))
    await repos.usage.release(action.id, clock())
    retry_id = action.id + ".retry-r_test"
    await repos.usage.reserve(task.id, action.attempt_id, retry_id, UsageKind.TOOL, Usage(tool_calls=1), clock())
    await repos.usage.reserve(task.id, action.attempt_id, "other-action", UsageKind.TOOL, Usage(tool_calls=1), clock())
    await gateway.reconcile(task, action, 1)
    assert [r.call_id for r in await repos.usage.open_reservations(task.id)] == ["other-action"]
    assert (await repos.tasks.get(task.scope_id, task.id)).usage.tool_calls == int(status == "succeeded")
    assert connector.execute_calls == 0


async def test_reconcile_no_reservation_is_valid():
    repos, gateway, connector, store, clock, task, action = await setup(ReconcileResult(status="succeeded"), reserve=False)
    assert await gateway.reconcile(task, action, 1) is ActionStatus.SUCCEEDED
    assert (await repos.tasks.get(task.scope_id, task.id)).usage.tool_calls == 0
    assert connector.execute_calls == 0


async def test_history_counts_past_first_page_per_action():
    repos, gateway, connector, store, clock, task, action = await setup(ReconcileResult(status="unknown"))
    for _ in range(505):
        await repos.events.append(Event(scope_id=task.scope_id, task_id=task.id,
                                      type=EventType.TOOL_ACTION, payload={}, occurred_at=clock()))
    await gateway.reconcile(task, action, 1)
    await gateway.reconcile(task, action, 2)
    history = await reconciliation_history(repos.events, task)
    assert [event.payload["attempt"] for event in history] == [1, 2]
    assert connector.execute_calls == 0


@pytest.mark.parametrize("status", ["succeeded", "failed", "unknown", "unsupported"])
async def test_late_automatic_result_has_no_effect(status):
    repos, gateway, connector, store, clock, task, action = await setup(ReconcileResult(status=status))
    await gateway.resolve(task, action, True, ACTOR, 1)
    winner = await repos.actions.get(task.id, action.logical_key)
    assert await gateway.reconcile(task, action, 1) is None
    assert await repos.actions.get(task.id, action.logical_key) == winner
    assert (await repos.tasks.get(task.scope_id, task.id)).usage.tool_calls == 1
    history = await reconciliation_history(repos.events, task)
    assert len(history) == 1 and history[0].payload["actor"] == ACTOR.actor_id
    assert connector.execute_calls == 0


@pytest.mark.parametrize("behavior", ["success", "accepted_timeout", "reject", "invalid"])
async def test_fake_external_execute_behaviors(behavior):
    connector = FakeExternalConnector(ReconcileResult(status="unknown"), execute_behavior=behavior)
    if behavior == "accepted_timeout":
        with pytest.raises(asyncio.TimeoutError):
            await connector.execute(None)
        assert connector.accepted_calls == 1
    elif behavior == "invalid":
        with pytest.raises(ValueError):
            await connector.execute(None)
    else:
        result = await connector.execute(None)
        assert result.status is (ToolStatus.SUCCEEDED if behavior == "success" else ToolStatus.FAILED)
    assert connector.execute_calls == 1
