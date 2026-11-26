"""Approve and replay the same logical action through the HTTP API and a Worker."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from harness.api import TokenAuth, create_app, token_digest
from harness.app import HarnessSystem, build_system
from harness.config import HarnessConfig
from harness.contracts import Action, ActionStatus, EventType, ReconcileResult, TaskStatus, WaitCondition
from harness.runtime.models import ScriptedModelAdapter, result_step, tool_step
from harness.tools.connectors import p0_connectors

from ..fakes import ACTOR, Clock, FakeExternalConnector
from ..unit.test_repositories import repos  # noqa: F401 (parametrized fixture)
from .test_worker import ACCEPTANCE, DRAFT, written_id

pytestmark = pytest.mark.asyncio

PACK = "document_draft@1"
ARGS = {"message": "draft ready"}
DEFAULT_TTL = 12


@dataclass
class ApprovalCase:
    system: HarnessSystem
    client: httpx.AsyncClient
    clock: Clock
    connector: FakeExternalConnector
    model: ScriptedModelAdapter
    action: Action

    @property
    def url(self):
        return f"/tasks/{self.action.task_id}"

    async def decide(self, decision, **body):
        return await self.client.post(
            f"{self.url}/approvals/{self.action.id}",
            json={"decision": decision, "request_hash": self.action.request_hash, **body},
        )

    async def task(self):
        return await self.system.tasks.get(ACTOR.scope_id, self.action.task_id)

    async def stored_action(self):
        return await self.system.repos.actions.get(self.action.task_id, self.action.logical_key)

    async def approval(self):
        return await self.system.repos.approvals.get(self.action.id, self.action.request_hash)

    async def events(self, kind):
        events = await self.system.repos.events.list(ACTOR.scope_id, task_id=self.action.task_id)
        return [event for event in events if event.type is kind]

    async def run_worker(self, expected_status):
        report = await self.system.worker().run_once()
        assert report and report.committed and report.status is expected_status, report


@pytest_asyncio.fixture
async def case(repos, tmp_path, monkeypatch):
    clock = Clock()
    connector = FakeExternalConnector(ReconcileResult(status="failed"))
    name = connector.spec.name
    monkeypatch.setattr("harness.app.p0_connectors", lambda store: [*p0_connectors(store), connector])
    config = HarnessConfig.from_env({
        "DARAOS_HARNESS_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        "DARAOS_HARNESS_APPROVAL_TTL_SEC": str(DEFAULT_TTL),
    })
    model = ScriptedModelAdapter([
        tool_step((name, ARGS)),
        tool_step(("artifacts.write", {"name": "draft", "content": DRAFT})),
        lambda ctx: result_step("done", {"draft": written_id(ctx)}),
    ])
    grants = {"users": {ACTOR.scope_id: {"main": {"tools": [name, "artifacts.write"]}}}}
    system = build_system(config, model, repos=repos, policy_spec=grants, clock=clock)
    system.policy.roles["main_tools@1"] |= {name}
    system.policy.capabilities[PACK] |= {name}
    token = uuid4().hex
    auth = TokenAuth([{"sha256": token_digest(token), **ACTOR.model_dump()}])
    transport = httpx.ASGITransport(app=create_app(system, auth))
    async with system.lifespan(), httpx.AsyncClient(
        transport=transport, base_url="http://harness", headers={"Authorization": f"Bearer {token}"}
    ) as client:
        response = await client.post(
            "/tasks", headers={"Idempotency-Key": "draft"},
            json={"goal": "Send the draft", "capability_ref": PACK, "acceptance": ACCEPTANCE.model_dump(mode="json")},
        )
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]
        report = await system.worker().run_once()
        assert report and report.committed and report.status is TaskStatus.WAITING, report
        (action,) = await repos.actions.list_for_task(task_id)
        assert action.status is ActionStatus.NEEDS_APPROVAL
        assert (await system.tasks.get(ACTOR.scope_id, task_id)).wait_condition is WaitCondition.APPROVAL
        assert connector.execute_calls == 0
        yield ApprovalCase(system, client, clock, connector, model, action)


async def test_get_task_exposes_version_and_pending_approvals(case):
    response = await case.client.get(case.url)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == (await case.task()).version
    (item,) = body["pending_approvals"]
    assert item["action_id"] == case.action.id
    assert item["tool"] == case.connector.spec.name
    assert item["logical_key"] == case.action.logical_key
    assert item["request_hash"] == case.action.request_hash
    assert item["args_ref"] == case.action.args_ref
    assert datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")) == case.action.created_at
    args = await case.client.get(f"/artifacts/{item['args_ref']}")
    assert args.status_code == 200 and args.json() == ARGS
    (requested,) = await case.events(EventType.APPROVAL_REQUESTED)
    assert requested.payload["action_id"] == item["action_id"]
    assert requested.payload["request_hash"] == item["request_hash"]


async def test_duplicate_approval_is_idempotent(case):
    expected_expiry = case.clock() + timedelta(seconds=DEFAULT_TTL)
    first = await case.decide("approve")
    assert first.status_code == 200, first.text
    queued = await case.task()
    assert queued.status is TaskStatus.QUEUED
    status_events = await case.events(EventType.TASK_STATUS_CHANGED)
    case.clock.advance(1)
    second = await case.decide("approve")
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert (await case.approval()).expires_at == expected_expiry
    assert datetime.fromisoformat(second.json()["expires_at"].replace("Z", "+00:00")) == expected_expiry
    assert await case.task() == queued
    assert await case.events(EventType.TASK_STATUS_CHANGED) == status_events
    assert len(await case.events(EventType.APPROVAL_DECIDED)) == 1
    await case.run_worker(TaskStatus.SUCCEEDED)
    assert case.connector.execute_calls == 1
    assert (await case.stored_action()).status is ActionStatus.SUCCEEDED


@pytest.mark.parametrize("original,changed", [("approve", "reject"), ("reject", "approve")])
async def test_changed_decision_after_queue_409(case, original, changed):
    first = await case.decide(original)
    assert first.status_code == 200 and first.json()["status"] == "queued"
    queued = await case.task()
    response = await case.decide(changed)
    assert response.status_code == 409, response.text
    assert (await case.approval()).decision.value == original
    assert await case.task() == queued
    assert len(await case.events(EventType.APPROVAL_DECIDED)) == 1
    assert case.connector.execute_calls == 0


async def test_expired_approval_can_be_reapproved(case):
    first = await case.decide("approve", expires_in_sec=60)
    assert first.status_code == 200 and first.json()["status"] == "queued"
    old = await case.approval()
    case.clock.advance(61)
    await case.run_worker(TaskStatus.WAITING)
    assert (await case.task()).wait_condition is WaitCondition.APPROVAL
    assert (await case.stored_action()).status is ActionStatus.NEEDS_APPROVAL
    assert case.connector.execute_calls == 0
    renewed = await case.decide("approve", reason="reviewed again")
    assert renewed.status_code == 200 and renewed.json()["status"] == "queued", renewed.text
    current = await case.approval()
    assert old.expires_at == case.clock() - timedelta(seconds=1)
    assert current.expires_at == case.clock() + timedelta(seconds=DEFAULT_TTL)
    assert current.reason == "reviewed again" and current.decided_at == case.clock()
    assert len(await case.events(EventType.APPROVAL_DECIDED)) == 2
    await case.run_worker(TaskStatus.SUCCEEDED)
    assert case.connector.execute_calls == 1


@pytest.mark.parametrize("expiry", [{}, {"expires_in_sec": 2}], ids=["default-ttl", "explicit-ttl"])
async def test_reject_never_expires(case, expiry):
    rejected = await case.decide("reject", reason="do not send", **expiry)
    assert rejected.status_code == 200, rejected.text
    assert (await case.approval()).expires_at is None
    assert rejected.json()["expires_at"] is None
    case.clock.advance(DEFAULT_TTL + 1)
    await case.run_worker(TaskStatus.SUCCEEDED)
    assert case.connector.execute_calls == 0
    assert (await case.stored_action()).status is ActionStatus.FAILED
    (refusal,) = [msg for msg in case.model.calls[1].messages
                  if msg["role"] == "tool" and msg["name"] == case.connector.spec.name]
    assert refusal["is_error"] and "status: denied" in refusal["content"]
    assert "do not send" in refusal["content"]


@pytest.mark.parametrize("decided", [False, True], ids=["waiting", "approved-and-queued"])
async def test_cancel_voids_pending_approval(case, decided):
    if decided:
        approved = await case.decide("approve")
        assert approved.status_code == 200 and approved.json()["status"] == "queued"
    case.clock.advance(1)
    cancelled = await case.client.post(f"{case.url}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    action = await case.stored_action()
    assert action.status is ActionStatus.FAILED and action.updated_at == case.clock()
    view = await case.client.get(case.url)
    assert view.status_code == 200 and view.json()["pending_approvals"] == []
    denied = await case.decide("approve")
    assert denied.status_code == 409, denied.text
    assert len(await case.events(EventType.APPROVAL_DECIDED)) == int(decided)
    assert await case.system.worker().run_once() is None
    assert case.connector.execute_calls == 0


async def test_failed_reconciliation_requests_fresh_approval(case):
    approved = await case.decide("approve", expires_in_sec=2)
    assert approved.status_code == 200, approved.text
    case.connector.execute_behavior = "accepted_timeout"
    await case.run_worker(TaskStatus.WAITING)
    assert (await case.task()).wait_condition is WaitCondition.RECONCILIATION
    assert (await case.stored_action()).status is ActionStatus.UNKNOWN
    assert case.connector.execute_calls == 1
    case.clock.advance(61)
    await case.system.scheduler.tick()
    assert case.connector.reconcile_calls == 1
    assert (await case.stored_action()).status is ActionStatus.FAILED
    assert (await case.task()).status is TaskStatus.QUEUED

    case.connector.execute_behavior = "success"
    await case.run_worker(TaskStatus.WAITING)
    assert (await case.task()).wait_condition is WaitCondition.APPROVAL
    assert (await case.stored_action()).status is ActionStatus.NEEDS_APPROVAL
    requests = await case.events(EventType.APPROVAL_REQUESTED)
    assert [(e.payload["action_id"], e.payload["request_hash"]) for e in requests] == [
        (case.action.id, case.action.request_hash), (case.action.id, case.action.request_hash),
    ]
    assert case.connector.execute_calls == 1
    renewed = await case.decide("approve")
    assert renewed.status_code == 200 and renewed.json()["status"] == "queued", renewed.text
    await case.run_worker(TaskStatus.SUCCEEDED)
    assert case.connector.execute_calls == 2
