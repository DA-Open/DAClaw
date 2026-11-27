"""Approvals: a side effect runs only with a decision bound to its arguments."""

from datetime import timedelta

import pytest

from harness.config import HarnessConfig
from harness.contracts import (
    ActionStatus,
    Approval,
    ApprovalConflict,
    ApprovalDecision,
    EventType,
    InvalidTransition,
    TaskOutcome,
    TaskStatus,
    ToolResult,
    ToolSpec,
    ToolStatus,
    WaitCondition,
)
from harness.storage.memory import InMemoryRepositories
from harness.tasks.approvals import ApprovalRefused, Approvals
from harness.tools import ToolGateway, UserGrant, WhitelistPolicy
from harness.tools.connectors import p0_connectors

from ..fakes import ACTOR, MAIN_PROFILE, Clock, MemoryArtifactStore, claimed_task

pytestmark = pytest.mark.asyncio

ALL = {"files.read", "artifacts.read", "artifacts.write", "mail.send"}


class Mail:
    """A connector with an outside side effect; counts real executions."""

    spec = ToolSpec(
        name="mail.send",
        description="send a mail",
        input_schema={"type": "object", "required": ["to"], "additionalProperties": False,
                      "properties": {"to": {"type": "string", "minLength": 1}}},
        read_only=False,
        side_effects=True,
        idempotent=False,
    )

    def __init__(self):
        self.sent = 0

    async def execute(self, call):
        self.sent += 1
        return ToolResult(status=ToolStatus.SUCCEEDED, summary=f"sent to {call.args['to']}", receipt_ref="rcpt_1")

    async def reconcile(self, action):
        raise NotImplementedError


class Setup:
    def __init__(self, tmp_path):
        self.mail = Mail()
        self.repos = InMemoryRepositories()
        self.clock = Clock()
        self.store = MemoryArtifactStore(self.repos)
        self.policy = WhitelistPolicy(
            roles={"main_tools@1": ALL},
            users={ACTOR.scope_id: {"main": UserGrant(tools=ALL, paths=())}},
        )
        self.gateway = ToolGateway(
            self.repos,
            self.store,
            self.policy,
            [*p0_connectors(self.store), self.mail],
            profiles={"main@1": MAIN_PROFILE.tool_policy_ref},
            clock=self.clock,
        )

    async def call(self, key="mail.1.0", to="ops@example.com"):
        from harness.contracts import ToolCall

        return await self.gateway.invoke(
            self.execution, ToolCall(tool="mail.send", args={"to": to}, logical_key=key)
        )

    async def start(self):
        self.task, self.execution = await claimed_task(self.repos)
        return self

    async def wait_for_approval(self):
        outcome = TaskOutcome(status=TaskStatus.WAITING, wait_condition=WaitCondition.APPROVAL)
        return await self.repos.tasks.commit(self.execution.lease, outcome, self.clock())

    async def action(self, key="mail.1.0"):
        return await self.repos.actions.get(self.task.id, key)

    async def decide(self, action_id, *, decision=ApprovalDecision.APPROVE, request_hash=None, actor=ACTOR, **kw):
        found = await self.action()
        return await Approvals(self.repos, clock=self.clock).decide(
            "s1", self.task.id, action_id, actor=actor, decision=decision,
            request_hash=request_hash or found.request_hash, **kw,
        )


async def test_side_effect_parks_until_approved(tmp_path):
    s = await Setup(tmp_path).start()
    first = await s.call()
    assert first.status is ToolStatus.NEEDS_APPROVAL and first.action_id
    assert s.mail.sent == 0
    # Written in the waiting state in one go: a crash cannot leave PLANNED behind.
    assert (await s.action()).status is ActionStatus.NEEDS_APPROVAL
    # Repeating the same call does not register a second action.
    assert (await s.call()).action_id == first.action_id
    assert len(await s.repos.actions.list_for_task(s.task.id)) == 1


async def test_approved_call_runs_exactly_once(tmp_path):
    s = await Setup(tmp_path).start()
    parked = await s.call()
    action = await s.action()
    await s.repos.approvals.add(
        Approval(action_id=parked.action_id, request_hash=action.request_hash,
                 approver="u1", decision=ApprovalDecision.APPROVE)
    )
    done = await s.call()
    assert done.status is ToolStatus.SUCCEEDED and s.mail.sent == 1
    action = await s.action()
    assert action.status is ActionStatus.SUCCEEDED and action.approval_ref


async def test_rejected_decision_fails_the_action(tmp_path):
    s = await Setup(tmp_path).start()
    parked = await s.call()
    action = await s.action()
    await s.repos.approvals.add(
        Approval(action_id=parked.action_id, request_hash=action.request_hash,
                 approver="u1", decision=ApprovalDecision.REJECT, reason="not to that address")
    )
    refused = await s.call()
    assert refused.status is ToolStatus.DENIED
    assert "not to that address" in refused.error.message
    assert s.mail.sent == 0
    assert (await s.action()).status is ActionStatus.FAILED


async def test_expired_approval_does_not_run(tmp_path):
    s = await Setup(tmp_path).start()
    parked = await s.call()
    action = await s.action()
    await s.repos.approvals.add(
        Approval(action_id=parked.action_id, request_hash=action.request_hash, approver="u1",
                 decision=ApprovalDecision.APPROVE, expires_at=s.clock.now - timedelta(seconds=1))
    )
    assert (await s.call()).status is ToolStatus.NEEDS_APPROVAL
    assert s.mail.sent == 0


async def test_decide_requeues_the_task(tmp_path):
    s = await Setup(tmp_path).start()
    parked = await s.call()
    await s.wait_for_approval()
    decided, approval = await s.decide(parked.action_id)
    assert decided.status is TaskStatus.QUEUED and decided.wait_condition is None
    assert approval.action_id == parked.action_id


async def test_decide_refuses_a_stale_hash(tmp_path):
    s = await Setup(tmp_path).start()
    parked = await s.call()
    await s.wait_for_approval()
    with pytest.raises(InvalidTransition):
        await s.decide(parked.action_id, request_hash="not-the-hash")


async def test_decide_requires_the_action_to_be_waiting(tmp_path):
    s = await Setup(tmp_path).start()
    parked = await s.call()
    action = await s.action()
    # Pretend the action already ran: an already-settled action must not be "approved".
    await s.repos.actions.update(parked.action_id, ActionStatus.SUCCEEDED, now=s.clock())
    await s.wait_for_approval()
    with pytest.raises(InvalidTransition):
        await s.decide(parked.action_id, request_hash=action.request_hash)


async def test_second_decision_conflicts(tmp_path):
    """A decision already exists for these arguments: the second one is refused.

    The task is left waiting so this exercises the conflict path rather than the
    "not waiting any more" path a second decide would hit after a requeue.
    """
    s = await Setup(tmp_path).start()
    parked = await s.call()
    action = await s.action()
    await s.wait_for_approval()
    await s.repos.approvals.add(
        Approval(action_id=parked.action_id, request_hash=action.request_hash,
                 approver="u1", decision=ApprovalDecision.APPROVE)
    )
    with pytest.raises(ApprovalConflict):
        await s.decide(parked.action_id, decision=ApprovalDecision.REJECT)


async def test_decide_needs_a_waiting_task(tmp_path):
    s = await Setup(tmp_path).start()
    parked = await s.call()
    with pytest.raises(InvalidTransition):
        await s.decide(parked.action_id)


async def _client(s):
    import httpx

    from harness.api import TokenAuth, create_app, token_digest

    token = "approver-token"
    auth = TokenAuth([{"sha256": token_digest(token), "scope_id": "s1", "actor_id": "u1"}])

    class Stub:
        repos = s.repos
        memory = None
        clock = s.clock
        config = HarnessConfig()

    app = create_app(Stub(), auth)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_api_decides_and_requeues(tmp_path):
    s = await Setup(tmp_path).start()
    parked = await s.call()
    await s.wait_for_approval()
    action = await s.action()
    async with await _client(s) as client:
        headers = {"Authorization": "Bearer approver-token"}
        url = f"/tasks/{s.task.id}/approvals/{parked.action_id}"
        stale = await client.post(url, json={"decision": "approve", "request_hash": "wrong"}, headers=headers)
        assert stale.status_code == 409, stale.text
        ok = await client.post(url, json={"decision": "approve", "request_hash": action.request_hash}, headers=headers)
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "queued"
        again = await client.post(url, json={"decision": "reject", "request_hash": action.request_hash}, headers=headers)
        assert again.status_code == 409, again.text


async def test_api_refuses_another_actor(tmp_path):
    import httpx

    from harness.api import TokenAuth, create_app, token_digest

    s = await Setup(tmp_path).start()
    parked = await s.call()
    await s.wait_for_approval()
    action = await s.action()
    auth = TokenAuth([{"sha256": token_digest("other"), "scope_id": "s1", "actor_id": "u2"}])

    class Stub:
        repos = s.repos
        memory = None
        clock = s.clock
        config = HarnessConfig()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(Stub(), auth)), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/tasks/{s.task.id}/approvals/{parked.action_id}",
            json={"decision": "approve", "request_hash": action.request_hash},
            headers={"Authorization": "Bearer other"},
        )
    assert response.status_code == 403


async def test_retry_failed_side_effect_with_expired_approval_reparks(tmp_path):
    s = await Setup(tmp_path).start()
    parked = await s.call()
    await s.wait_for_approval()
    await s.decide(parked.action_id, expires_in_sec=1)
    lease = await s.repos.tasks.claim("retry", [s.task.run_lane], 60, s.clock())
    s.execution = s.execution.model_copy(update={"lease": lease})
    # Reconciliation has confirmed that the previous attempt did not take effect.
    await s.repos.actions.update(parked.action_id, ActionStatus.FAILED, now=s.clock())
    s.clock.advance(1)

    assert (await s.call()).status is ToolStatus.NEEDS_APPROVAL
    action = await s.action()
    assert action.status is ActionStatus.NEEDS_APPROVAL
    assert s.mail.sent == 0
    assert (await s.call()).action_id == action.id
    requests = [e.payload for e in await s.repos.events.list("s1", task_id=s.task.id)
                if e.type is EventType.APPROVAL_REQUESTED]
    assert [(e["action_id"], e["request_hash"]) for e in requests] == [(action.id, action.request_hash)] * 2
    await s.wait_for_approval()
    queued, approval = await s.decide(action.id, expires_in_sec=60)
    assert queued.status is TaskStatus.QUEUED
    assert approval.expires_at == s.clock() + timedelta(seconds=60)


async def test_rejection_ignores_service_caller_expiry(tmp_path):
    s = await Setup(tmp_path).start()
    parked = await s.call()
    await s.wait_for_approval()
    _, approval = await s.decide(parked.action_id, decision=ApprovalDecision.REJECT, expires_in_sec=1)
    assert approval.expires_at is None
    assert (await s.repos.approvals.get(approval.action_id, approval.request_hash)).expires_at is None


async def test_replay_honors_preexisting_reject_with_elapsed_expiry(tmp_path):
    s = await Setup(tmp_path).start()
    await s.call()
    action = await s.action()
    await s.repos.approvals.add(Approval(
        action_id=action.id, request_hash=action.request_hash, approver=ACTOR.actor_id,
        decision=ApprovalDecision.REJECT, reason="declined", expires_at=s.clock(),
    ))
    result = await s.call()
    assert result.status is ToolStatus.DENIED
    assert "declined" in result.error.message
    assert s.mail.sent == 0
