import asyncio
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from harness.api import TokenAuth, create_app, token_digest
from harness.app import build_system
from harness.config import HarnessConfig
from harness.contracts import (
    Action, ActionStatus, Event, EventType, ReconcileResult, TaskOutcome,
    TaskStatus, Usage, WaitCondition,
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
    case.connector.reconcile_result = ReconcileResult(status="failed")
    await case.recovery.tick(case.clock())
    assert (await stored_action(case)).status is ActionStatus.FAILED
    assert await case.repos.usage.open_reservations(case.task.id) == []
    assert (await case.repos.tasks.get("s1", case.task.id)).usage.tool_calls == 0
    assert case.connector.execute_calls == 0


async def test_reconcile_unknown_backs_off(case):
    await case.recovery.tick(case.clock())
    case.clock.advance(59)
    await case.recovery.tick(case.clock())
    assert case.connector.reconcile_calls == 1
    case.clock.advance(1)
    await case.recovery.tick(case.clock())
    assert case.connector.reconcile_calls == 2


async def test_reconcile_gives_up_after_five(case):
    await case.recovery.tick(case.clock())
    for seconds in (60, 120, 240, 480):
        case.clock.advance(seconds)
        await case.recovery.tick(case.clock())
    assert case.connector.reconcile_calls == 5
    case.clock.advance(10000)
    await case.recovery.tick(case.clock())
    assert case.connector.reconcile_calls == 5
    response = await case.client.get(case.url, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["status"] == "waiting"
    assert response.json()["unsettled_actions"][0]["needs_manual_confirmation"] is True


async def test_unsupported_needs_manual(case):
    case.connector.reconcile_result = ReconcileResult(status="unsupported")
    await case.recovery.tick(case.clock())
    case.clock.advance(10000)
    await case.recovery.tick(case.clock())
    assert case.connector.reconcile_calls == 1
    response = await case.client.get(case.url, headers=AUTH)
    assert response.json()["unsettled_actions"][0]["needs_manual_confirmation"] is True


@pytest.mark.parametrize("outcome,expected", [("happened", ActionStatus.SUCCEEDED), ("not_happened", ActionStatus.FAILED)])
async def test_resolve_happened_or_not_happened(case, outcome, expected):
    response = await case.client.post(case.resolve, headers=AUTH, json={"outcome": outcome})
    assert response.status_code == 200, response.text
    action = await stored_action(case)
    assert action.status is expected
    if outcome == "happened":
        assert action.receipt_ref
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
    case.connector.reconcile_result = ReconcileResult(status=status)
    await case.recovery.tick(case.clock())
    assert case.connector.execute_calls == 0


async def test_late_reconciliation_cannot_overwrite_manual_decision(case, monkeypatch):
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
