import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from harness.api import TokenAuth, create_app, token_digest
from harness.app import build_system
from harness.config import HarnessConfig
from harness.contracts import (
    Action, ActionStatus, Event, EventType, ReconcileResult, Task, TaskOrigin, TaskOutcome,
    TaskRequest, TaskStatus, Usage, WaitCondition,
)
from harness.runtime.models import ScriptedModelAdapter
from harness.tasks.recovery import Recovery

from ..fakes import ACTOR, Clock, FakeExternalConnector, claimed_task
from ..unit.test_repositories import repos

pytestmark = pytest.mark.asyncio
AUTH = {"Authorization": "Bearer owner"}


@pytest_asyncio.fixture
async def case(repos, tmp_path):
    clock = Clock()
    system = build_system(HarnessConfig(artifact_root=tmp_path), ScriptedModelAdapter([]), repos=repos, clock=clock)
    connector = FakeExternalConnector(ReconcileResult(status="unknown"))
    system.gateway._connectors[connector.spec.name] = connector
    task, execution = await claimed_task(repos)
    action = Action(
        task_id=task.id, attempt_id=execution.lease.attempt_id, logical_key="step-1.0",
        tool=connector.spec.name, request_hash="request", read_only=False, status=ActionStatus.UNKNOWN,
        created_at=clock(), updated_at=clock(),
    )
    await repos.actions.register(action, reserve=Usage(tool_calls=1))
    task = await repos.tasks.commit(execution.lease, TaskOutcome(
        status=TaskStatus.WAITING, wait_condition=WaitCondition.RECONCILIATION,
    ), clock())
    auth = TokenAuth([
        {"sha256": token_digest("owner"), "scope_id": "s1", "actor_id": "u1"},
        {"sha256": token_digest("owner-web"), "scope_id": "s1", "actor_id": "u1"},
        {"sha256": token_digest("other"), "scope_id": "s1", "actor_id": "u2"},
        {"sha256": token_digest("foreign"), "scope_id": "s2", "actor_id": "u1"},
    ])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(system, auth)), base_url="http://harness") as client:
        value = SimpleNamespace(
            system=system, repos=repos, clock=clock, connector=connector, task=task, action=action, client=client,
            recovery=Recovery(repos.tasks, system.config, clock, gateway=system.gateway, actions=repos.actions, events=repos.events),
            url=f"/tasks/{task.id}", resolve=f"/tasks/{task.id}/actions/{action.id}/resolve",
        )
        yield value
        assert connector.execute_calls == 0


async def stored_action(case):
    return await case.repos.actions.get(case.task.id, case.action.logical_key)


async def test_reconcile_succeeded_settles_and_requeues(case):
    case.clock.advance(60)
    case.connector.reconcile_result = ReconcileResult(status="succeeded", receipt_ref="receipt:external")
    await case.recovery.tick(case.clock())
    action = await stored_action(case)
    task = await case.repos.tasks.get("s1", case.task.id)
    assert action.status is ActionStatus.SUCCEEDED and action.receipt_ref == "receipt:external"
    assert task.status is TaskStatus.QUEUED
    assert task.usage.tool_calls == 1
    assert await case.repos.usage.open_reservations(task.id) == []
    assert case.connector.execute_calls == 0


async def test_reconcile_failed_marks_failed(case):
    case.clock.advance(60)
    case.connector.reconcile_result = ReconcileResult(status="failed")
    await case.recovery.tick(case.clock())
    assert (await stored_action(case)).status is ActionStatus.FAILED
    assert await case.repos.usage.open_reservations(case.task.id) == []
    assert (await case.repos.tasks.get("s1", case.task.id)).usage.tool_calls == 0
    assert case.connector.execute_calls == 0


async def test_reconcile_unknown_backs_off(case):
    case.clock.advance(60)
    await case.recovery.tick(case.clock())
    case.clock.advance(59)
    await case.recovery.tick(case.clock())
    assert case.connector.reconcile_calls == 1
    case.clock.advance(1)
    await case.recovery.tick(case.clock())
    assert case.connector.reconcile_calls == 2


async def test_reconcile_gives_up_after_six(case):
    case.clock.advance(60)
    await case.recovery.tick(case.clock())
    for seconds in (60, 120, 240, 480, 960):
        case.clock.advance(seconds)
        await case.recovery.tick(case.clock())
    assert case.connector.reconcile_calls == 6
    case.clock.advance(10000)
    await case.recovery.tick(case.clock())
    assert case.connector.reconcile_calls == 6
    response = await case.client.get(case.url, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["status"] == "waiting"
    assert response.json()["unsettled_actions"][0]["needs_manual_confirmation"] is True


async def test_unsupported_needs_manual(case):
    case.clock.advance(60)
    case.connector.reconcile_result = ReconcileResult(status="unsupported")
    await case.recovery.tick(case.clock())
    case.clock.advance(10000)
    await case.recovery.tick(case.clock())
    assert case.connector.reconcile_calls == 1
    response = await case.client.get(case.url, headers=AUTH)
    assert response.json()["unsettled_actions"][0]["needs_manual_confirmation"] is True


@pytest.mark.parametrize("outcome,expected", [("happened", ActionStatus.SUCCEEDED), ("not_happened", ActionStatus.FAILED)])
async def test_resolve_happened_or_not_happened(case, outcome, expected):
    response = await case.client.post(case.resolve, headers={"Authorization": "Bearer owner-web"}, json={"outcome": outcome})
    assert response.status_code == 200, response.text
    action = await stored_action(case)
    assert action.status is expected
    assert action.receipt_ref
    receipt = await case.client.get(f"/artifacts/{action.receipt_ref}", headers=AUTH)
    assert receipt.status_code == 200
    assert receipt.json() == {"actor": ACTOR.actor_id, "outcome": outcome}
    assert (await case.repos.tasks.get("s1", case.task.id)).status is TaskStatus.QUEUED
    events = await case.repos.events.list("s1", task_id=case.task.id)
    event = [e for e in events if e.type is EventType.TOOL_RECONCILE][-1]
    assert event.payload["actor"] == ACTOR.actor_id
    assert event.payload["status"] == expected.value
    assert case.connector.reconcile_calls == 0


async def test_resolve_other_user_403(case):
    response = await case.client.post(case.resolve, headers={"Authorization": "Bearer other"}, json={"outcome": "happened"})
    assert response.status_code == 403
    assert (await stored_action(case)).status is ActionStatus.UNKNOWN


async def test_resolve_readonly_action_409(case):
    action = case.action.model_copy(update={"id": "act_readonly", "logical_key": "step-1.1", "read_only": True})
    await case.repos.actions.register(action)
    response = await case.client.post(f"{case.url}/actions/{action.id}/resolve", headers=AUTH, json={"outcome": "happened"})
    assert response.status_code == 409
    assert (await case.repos.actions.get(case.task.id, action.logical_key)).status is ActionStatus.UNKNOWN


async def test_resolve_settled_action_409(case):
    await case.repos.actions.update(case.action.id, ActionStatus.SUCCEEDED, now=case.clock())
    response = await case.client.post(case.resolve, headers=AUTH, json={"outcome": "not_happened"})
    assert response.status_code == 409
    assert (await stored_action(case)).status is ActionStatus.SUCCEEDED


async def test_resolve_scope_missing_and_invalid_body(case):
    response = await case.client.post(case.resolve, headers={"Authorization": "Bearer foreign"}, json={"outcome": "happened"})
    assert response.status_code == 404
    response = await case.client.post(f"{case.url}/actions/act_missing/resolve", headers=AUTH, json={"outcome": "happened"})
    assert response.status_code == 404
    response = await case.client.post(case.resolve, headers=AUTH, json={"outcome": "maybe"})
    assert response.status_code == 400
    assert (await stored_action(case)).status is ActionStatus.UNKNOWN


async def test_cancelled_task_cleanup_pending_then_done(case):
    case.clock.advance(60)
    await case.repos.tasks.request_cancel("s1", case.task.id, "stop", case.clock())
    response = await case.client.get(case.url, headers=AUTH)
    assert response.json()["cleanup"] == "pending_reconciliation"
    assert response.json()["unsettled_actions"][0]["action_id"] == case.action.id
    case.connector.reconcile_result = ReconcileResult(status="succeeded", receipt_ref="receipt:cancelled")
    await case.recovery.tick(case.clock())
    response = await case.client.get(case.url, headers=AUTH)
    assert response.json()["cleanup"] == "done"
    assert response.json()["status"] == "cancelled"
    assert response.json()["unsettled_actions"] == []
    events = await case.repos.events.list("s1", task_id=case.task.id)
    assert not any(e.payload.get("from") == "cancelled" and e.payload.get("to") == "queued" for e in events)


async def test_reconcile_history_after_many_events(case):
    case.clock.advance(60)
    for _ in range(105):
        await case.repos.events.append(Event(scope_id="s1", task_id=case.task.id, type=EventType.BUDGET_REMINDER))
    case.connector.reconcile_result = ReconcileResult(status="unsupported")
    await case.recovery.tick(case.clock())
    case.clock.advance(600)
    await case.recovery.tick(case.clock())
    assert case.connector.reconcile_calls == 1
    response = await case.client.get(case.url, headers=AUTH)
    assert response.json()["unsettled_actions"][0]["needs_manual_confirmation"] is True


@pytest.mark.parametrize("status", ["succeeded", "failed", "unknown", "unsupported"])
async def test_reconcile_never_executes(case, status):
    case.clock.advance(60)
    case.connector.reconcile_result = ReconcileResult(status=status)
    await case.recovery.tick(case.clock())
    assert case.connector.execute_calls == 0


async def test_late_reconciliation_cannot_overwrite_manual_decision(case, monkeypatch):
    case.clock.advance(60)
    entered, finish = asyncio.Event(), asyncio.Event()
    reconcile = case.connector.reconcile
    case.connector.reconcile_result = ReconcileResult(status="failed")

    async def delayed(action):
        entered.set()
        await finish.wait()
        return await reconcile(action)

    monkeypatch.setattr(case.connector, "reconcile", delayed)
    automatic = asyncio.create_task(case.recovery.tick(case.clock()))
    await entered.wait()
    try:
        response = await case.client.post(case.resolve, headers=AUTH, json={"outcome": "happened"})
        assert response.status_code == 200, response.text
        assert (await stored_action(case)).status is ActionStatus.SUCCEEDED
        assert (await case.repos.tasks.get("s1", case.task.id)).status is TaskStatus.QUEUED
    finally:
        finish.set()
        await automatic
    assert (await stored_action(case)).status is ActionStatus.SUCCEEDED
    assert (await case.repos.tasks.get("s1", case.task.id)).status is TaskStatus.QUEUED
    assert case.connector.execute_calls == 0


async def test_manual_after_automatic_409(case):
    case.clock.advance(60)
    case.connector.reconcile_result = ReconcileResult(status="succeeded", receipt_ref="receipt:automatic")
    await case.recovery.tick(case.clock())
    response = await case.client.post(case.resolve, headers=AUTH, json={"outcome": "not_happened"})
    assert response.status_code == 409
    assert response.json()["current_status"] == "succeeded"
    assert (await stored_action(case)).status is ActionStatus.SUCCEEDED
    events = await case.repos.events.list("s1", task_id=case.task.id)
    assert len([event for event in events if event.type is EventType.TOOL_RECONCILE]) == 1


async def test_manual_loses_race_after_api_validation(case, monkeypatch):
    case.clock.advance(60)
    entered, finish = asyncio.Event(), asyncio.Event()
    resolve = case.system.gateway.resolve

    async def delayed(task, action, happened, actor, expected_attempt):
        entered.set()
        await finish.wait()
        return await resolve(task, action, happened, actor, expected_attempt)

    monkeypatch.setattr(case.system.gateway, "resolve", delayed)
    manual = asyncio.create_task(case.client.post(case.resolve, headers=AUTH, json={"outcome": "not_happened"}))
    await entered.wait()
    try:
        case.connector.reconcile_result = ReconcileResult(status="succeeded", receipt_ref="receipt:automatic")
        await case.recovery.tick(case.clock())
    finally:
        finish.set()
        response = await manual
    assert response.status_code == 409
    assert response.json()["current_status"] == "succeeded"
    action = await stored_action(case)
    assert action.status is ActionStatus.SUCCEEDED and action.receipt_ref == "receipt:automatic"
    task = await case.repos.tasks.get("s1", case.task.id)
    assert task.status is TaskStatus.QUEUED and task.usage.tool_calls == 1
    events = await case.repos.events.list("s1", task_id=case.task.id)
    assert len([event for event in events if event.type is EventType.TOOL_RECONCILE]) == 1


async def test_recovery_releases_waiting_task_without_unsettled_actions(case):
    await case.repos.actions.update(case.action.id, ActionStatus.FAILED, now=case.clock())
    assert await case.repos.actions.list_unsettled(case.task.id) == []
    assert await case.recovery.tick(case.clock()) == 0
    assert (await case.repos.tasks.get("s1", case.task.id)).status is TaskStatus.QUEUED
    assert case.connector.reconcile_calls == case.connector.execute_calls == 0


async def extra_task(case, *, tool, wait_condition, lease_ttl=60):
    request = TaskRequest(request_key="second", goal="Second task", capability_ref="document_draft@1")
    origin = TaskOrigin(**case.task.model_dump(include={
        "agent_role", "agent_profile_ref", "run_lane", "trigger_type",
    }))
    new = Task.new(request, ACTOR, origin, now=case.clock()).model_copy(update={"id": "zzz_second_task"})
    await case.repos.tasks.create(new)
    lease = await case.repos.tasks.claim("w2", [new.run_lane], lease_ttl, case.clock())
    action = case.action.model_copy(update={
        "id": "act_second", "task_id": new.id, "attempt_id": lease.attempt_id,
        "tool": tool, "status": ActionStatus.RUNNING,
    })
    await case.repos.actions.register(action, reserve=Usage(tool_calls=1))
    if wait_condition is not None:
        await case.repos.tasks.commit(lease, TaskOutcome(
            status=TaskStatus.WAITING, wait_condition=wait_condition,
        ), case.clock())
    return new, action


async def test_resolve_rejected_while_task_running(case):
    task, action = await extra_task(case, tool=case.connector.spec.name, wait_condition=None)
    before = await case.repos.usage.open_reservations(task.id)
    response = await case.client.post(
        f"/tasks/{task.id}/actions/{action.id}/resolve", headers=AUTH, json={"outcome": "not_happened"},
    )
    assert response.status_code == 409
    assert response.json()["task_status"] == "running"
    assert await case.repos.actions.get(task.id, action.logical_key) == action
    assert await case.repos.usage.open_reservations(task.id) == before


async def test_resolve_rejected_while_waiting_for_input(case):
    task, action = await extra_task(case, tool=case.connector.spec.name, wait_condition=WaitCondition.INPUT)
    response = await case.client.post(
        f"/tasks/{task.id}/actions/{action.id}/resolve", headers=AUTH, json={"outcome": "happened"},
    )
    assert response.status_code == 409
    assert response.json()["task_status"] == "waiting"
    assert await case.repos.actions.get(task.id, action.logical_key) == action


async def test_first_reconcile_waits_for_grace(case):
    await case.repos.actions.update(case.action.id, ActionStatus.FAILED, now=case.clock())
    task, action = await extra_task(case, tool=case.connector.spec.name, wait_condition=None, lease_ttl=10)
    case.clock.advance(10)
    assert await case.recovery.tick(case.clock()) == 1
    assert (await case.repos.tasks.get("s1", task.id)).wait_condition is WaitCondition.RECONCILIATION
    assert case.connector.reconcile_calls == 0
    case.clock.advance(49)
    await case.recovery.tick(case.clock())
    assert case.connector.reconcile_calls == 0
    case.clock.advance(1)
    await case.recovery.tick(case.clock())
    assert case.connector.reconcile_calls == 1
    assert case.connector.reconciled_actions[0].action_id == action.id


async def test_grace_uses_config(case):
    config = replace(case.system.config, reconcile_grace_sec=120)
    recovery = Recovery(case.repos.tasks, config, case.clock, gateway=case.system.gateway,
                        actions=case.repos.actions, events=case.repos.events)
    case.clock.advance(119)
    await recovery.tick(case.clock())
    assert case.connector.reconcile_calls == 0
    case.clock.advance(1)
    await recovery.tick(case.clock())
    assert case.connector.reconcile_calls == 1


async def test_reconcile_grace_config_default_env_and_positive():
    assert HarnessConfig().reconcile_grace_sec == HarnessConfig().lease_ttl_sec == 60
    config = HarnessConfig.from_env({"DARAOS_HARNESS_RECONCILE_GRACE_SEC": "90"})
    assert config.reconcile_grace_sec == config.safe_dict()["reconcile_grace_sec"] == 90
    for invalid in (0, -1):
        with pytest.raises(ValueError, match="reconcile_grace_sec"):
            HarnessConfig(reconcile_grace_sec=invalid)
        with pytest.raises(ValueError, match="reconcile_grace_sec"):
            HarnessConfig.from_env({"DARAOS_HARNESS_RECONCILE_GRACE_SEC": str(invalid)})


async def test_missing_connector_needs_manual(case):
    other = FakeExternalConnector(ReconcileResult(status="unknown"))
    other.spec = other.spec.model_copy(update={"name": "other.write"})
    case.system.gateway._connectors[other.spec.name] = other
    await extra_task(case, tool=other.spec.name, wait_condition=WaitCondition.RECONCILIATION)
    del case.system.gateway._connectors[case.connector.spec.name]
    case.clock.advance(60)
    await case.recovery.tick(case.clock())
    response = await case.client.get(case.url, headers=AUTH)
    assert response.json()["unsettled_actions"][0]["needs_manual_confirmation"] is True
    events = await case.repos.events.list("s1", task_id=case.task.id)
    event = [event for event in events if event.type is EventType.TOOL_RECONCILE][-1]
    assert event.payload["status"] == "unsupported"
    assert event.payload["reason"] == "connector_missing"
    assert other.reconcile_calls == 1 and other.execute_calls == 0


async def test_reconcile_error_counts_as_unknown(case, monkeypatch):
    other = FakeExternalConnector(ReconcileResult(status="unknown"))
    other.spec = other.spec.model_copy(update={"name": "other.write"})
    case.system.gateway._connectors[other.spec.name] = other
    await extra_task(case, tool=other.spec.name, wait_condition=WaitCondition.RECONCILIATION)

    async def timeout(action):
        raise TimeoutError("remote delayed")

    monkeypatch.setattr(case.connector, "reconcile", timeout)
    case.clock.advance(60)
    await case.recovery.tick(case.clock())
    events = await case.repos.events.list("s1", task_id=case.task.id)
    event = [event for event in events if event.type is EventType.TOOL_RECONCILE][-1]
    assert event.payload["status"] == "unknown"
    assert event.payload["error"] == {"type": "TimeoutError", "message": "remote delayed"}
    assert other.reconcile_calls == 1 and other.execute_calls == 0
    response = await case.client.get(case.url, headers=AUTH)
    assert response.json()["unsettled_actions"][0]["needs_manual_confirmation"] is False


async def test_unexpected_reconcile_error_propagates(case, monkeypatch):
    async def broken(action):
        raise RuntimeError("connector bug")

    monkeypatch.setattr(case.connector, "reconcile", broken)
    case.clock.advance(60)
    with pytest.raises(RuntimeError, match="connector bug"):
        await case.recovery.tick(case.clock())


@pytest.mark.parametrize("status,previous_attempts", [("unknown", 5), ("unsupported", 1)])
async def test_attempts_reset_after_decision(case, status, previous_attempts):
    case.connector.reconcile_result = ReconcileResult(status=status)
    for seconds in (60, 60, 120, 240, 480)[:previous_attempts]:
        case.clock.advance(seconds)
        await case.recovery.tick(case.clock())
    response = await case.client.post(case.resolve, headers=AUTH, json={"outcome": "not_happened"})
    assert response.status_code == 200
    lease = await case.repos.tasks.claim("retry", [case.task.run_lane], 60, case.clock())
    await case.repos.actions.update(case.action.id, ActionStatus.UNKNOWN, now=case.clock())
    await case.repos.tasks.commit(lease, TaskOutcome(
        status=TaskStatus.WAITING, wait_condition=WaitCondition.RECONCILIATION,
    ), case.clock())
    response = await case.client.get(case.url, headers=AUTH)
    assert response.json()["unsettled_actions"][0]["needs_manual_confirmation"] is False
    case.connector.reconcile_result = ReconcileResult(status="unknown")
    case.clock.advance(60)
    await case.recovery.tick(case.clock())
    assert case.connector.reconcile_calls == previous_attempts + 1
    events = await case.repos.events.list("s1", task_id=case.task.id)
    event = [event for event in events if event.type is EventType.TOOL_RECONCILE][-1]
    assert event.payload["attempt"] == 1 and event.payload["status"] == "unknown"


async def test_backoff_schedule(case):
    for count, seconds in enumerate((60, 60, 120, 240, 480, 960), start=1):
        case.clock.advance(seconds - 1)
        await case.recovery.tick(case.clock())
        assert case.connector.reconcile_calls == count - 1
        case.clock.advance(1)
        await case.recovery.tick(case.clock())
        assert case.connector.reconcile_calls == count
        await case.recovery.tick(case.clock())
        assert case.connector.reconcile_calls == count
        response = await case.client.get(case.url, headers=AUTH)
        assert response.json()["unsettled_actions"][0]["needs_manual_confirmation"] is (count == 6)
