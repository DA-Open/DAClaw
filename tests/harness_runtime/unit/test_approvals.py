"""Approvals: a side-effecting call runs only with a decision bound to its arguments."""

import pytest

from datetime import timedelta

from harness.contracts import (
    ActionStatus,
    Approval,
    InvalidTransition,
    ApprovalDecision,
    ErrorCode,
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
        # The policy has no capability map, so the pack's tool list does not
        # narrow anything here: the role and user whitelists decide.
        self.task, self.execution = await claimed_task(self.repos)
        return self

    async def wait_for_approval(self):
        """Drive the task into waiting/approval the way the Runner does."""
        outcome = TaskOutcome(status=TaskStatus.WAITING, wait_condition=WaitCondition.APPROVAL)
        return await self.repos.tasks.commit(self.execution.lease, outcome, self.clock())


async def test_side_effect_parks_until_approved(tmp_path):
    s = await Setup(tmp_path).start()
    first = await s.call()
    assert first.status is ToolStatus.NEEDS_APPROVAL
    assert first.action_id
    assert s.mail.sent == 0
    # Repeating the same call does not register a second action.
    second = await s.call()
    assert second.action_id == first.action_id
    actions = await s.repos.actions.list_for_task(s.task.id)
    assert len(actions) == 1 and actions[0].status is ActionStatus.NEEDS_APPROVAL


async def test_approved_call_runs_exactly_once(tmp_path):
    s = await Setup(tmp_path).start()
    parked = await s.call()
    await s.repos.approvals.add(
        Approval(
            action_id=parked.action_id,
            request_hash=(await s.repos.actions.get(s.task.id, "mail.1.0")).request_hash,
            approver="u1",
            decision=ApprovalDecision.APPROVE,
        )
    )
    done = await s.call()
    assert done.status is ToolStatus.SUCCEEDED
    assert s.mail.sent == 1
    action = await s.repos.actions.get(s.task.id, "mail.1.0")
    assert action.status is ActionStatus.SUCCEEDED and action.approval_ref


async def test_expired_or_rejected_approval_does_not_run(tmp_path):
    s = await Setup(tmp_path).start()
    parked = await s.call()
    action = await s.repos.actions.get(s.task.id, "mail.1.0")
    await s.repos.approvals.add(
        Approval(
            action_id=parked.action_id,
            request_hash=action.request_hash,
            approver="u1",
            decision=ApprovalDecision.APPROVE,
            expires_at=s.clock.now - timedelta(seconds=1),
        )
    )
    assert (await s.call()).status is ToolStatus.NEEDS_APPROVAL
    await s.repos.approvals.add(
        Approval(
            action_id=parked.action_id,
            request_hash=action.request_hash,
            approver="u1",
            decision=ApprovalDecision.REJECT,
        )
    )
    assert (await s.call()).status is ToolStatus.NEEDS_APPROVAL
    assert s.mail.sent == 0


async def test_decide_requeues_the_task(tmp_path):
    s = await Setup(tmp_path).start()
    parked = await s.call()
    await s.wait_for_approval()
    task = await s.repos.tasks.get("s1", s.task.id)
    assert task.status is TaskStatus.WAITING
    action = await s.repos.actions.get(s.task.id, "mail.1.0")

    decided, approval = await Approvals(s.repos, clock=s.clock).decide(
        "s1",
        s.task.id,
        parked.action_id,
        actor=ACTOR,
        decision=ApprovalDecision.APPROVE,
        request_hash=action.request_hash,
    )
    assert decided.status is TaskStatus.QUEUED and decided.wait_condition is None
    assert approval.action_id == parked.action_id


async def test_decide_rejects_a_stale_hash(tmp_path):
    s = await Setup(tmp_path).start()
    parked = await s.call()
    await s.wait_for_approval()
    with pytest.raises(ApprovalRefused):
        await Approvals(s.repos, clock=s.clock).decide(
            "s1",
            s.task.id,
            parked.action_id,
            actor=ACTOR,
            decision=ApprovalDecision.APPROVE,
            request_hash="not-the-hash",
        )


async def test_decide_needs_a_waiting_task(tmp_path):
    s = await Setup(tmp_path).start()
    parked = await s.call()
    action = await s.repos.actions.get(s.task.id, "mail.1.0")
    with pytest.raises(InvalidTransition):
        await Approvals(s.repos, clock=s.clock).decide(
            "s1",
            s.task.id,
            parked.action_id,
            actor=ACTOR,
            decision=ApprovalDecision.APPROVE,
            request_hash=action.request_hash,
        )


async def test_api_decides_and_requeues(tmp_path):
    import httpx

    from harness.api import TokenAuth, create_app, token_digest

    s = await Setup(tmp_path).start()
    parked = await s.call()
    await s.wait_for_approval()
    action = await s.repos.actions.get(s.task.id, "mail.1.0")
    token = "approver-token"
    auth = TokenAuth([{"sha256": token_digest(token), "scope_id": "s1", "actor_id": "u1"}])

    class Stub:
        repos = s.repos

    app = create_app(Stub(), auth)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {token}"}
        ok = await client.post(
            f"/tasks/{s.task.id}/approvals/{parked.action_id}",
            json={"decision": "approve", "request_hash": action.request_hash},
            headers=headers,
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "queued"
        stale = await client.post(
            f"/tasks/{s.task.id}/approvals/{parked.action_id}",
            json={"decision": "approve", "request_hash": "wrong"},
            headers=headers,
        )
        assert stale.status_code == 409
