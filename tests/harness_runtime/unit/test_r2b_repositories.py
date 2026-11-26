"""Input resumption, cancellation and recovery on the shared repository fixtures."""

from datetime import timedelta

import pytest

from harness.contracts import (
    Action, ActionStatus, ApprovalConflict, ApprovalDecision, EventType, InvalidTransition, NotFound,
    TaskOutcome, TaskStatus, WaitCondition,
)

from .test_repositories import OTHER, T0, TTL, claimed, repos

pytestmark = pytest.mark.asyncio


async def register_side_effect(repos, lease, key, status):
    action = Action(
        task_id=lease.task_id, attempt_id=lease.attempt_id, logical_key=key,
        tool="mail.send", request_hash=f"hash-{key}", read_only=False,
        status=status, created_at=T0, updated_at=T0,
    )
    registered = await repos.actions.register(action)
    assert registered.created
    return registered.action


async def test_resume_hides_missing_and_cross_scope_tasks(repos):
    task, lease = await claimed(repos)
    waiting = await repos.tasks.commit(
        lease, TaskOutcome(status=TaskStatus.WAITING, wait_condition=WaitCondition.INPUT), T0
    )
    history = await repos.events.list(task.scope_id, task.id)
    for scope_id, task_id in [(OTHER.scope_id, task.id), (task.scope_id, "missing-task")]:
        with pytest.raises(NotFound):
            await repos.tasks.resume(scope_id, task_id, expected_version=waiting.version,
                                     input_refs=["artifact:new"], now=T0)
    assert await repos.tasks.get(task.scope_id, task.id) == waiting
    assert await repos.events.list(task.scope_id, task.id) == history


async def test_reclaim_with_needs_approval_requeues(repos):
    task, lease = await claimed(repos)
    action = await register_side_effect(repos, lease, "pending", ActionStatus.NEEDS_APPROVAL)
    now = lease.lease_expires_at
    assert await repos.tasks.reclaim_expired(now, 3) == 1
    stored = await repos.tasks.get(task.scope_id, task.id)
    assert stored.status is TaskStatus.QUEUED
    assert stored.wait_condition is None and stored.current_attempt_id is None
    assert (await repos.tasks.get_attempt(task.scope_id, lease.attempt_id)).exit_reason == "lease_expired"
    assert await repos.actions.get(task.id, action.logical_key) == action


@pytest.mark.parametrize("requeue_first", [False, True], ids=["waiting", "queued"])
async def test_cancel_only_voids_this_tasks_pending_actions(repos, requeue_first):
    task, lease = await claimed(repos)
    peer, peer_lease = await claimed(repos, actor=OTHER)
    peer_action = await register_side_effect(repos, peer_lease, "pending", ActionStatus.NEEDS_APPROVAL)
    pending = [await register_side_effect(repos, lease, key, ActionStatus.NEEDS_APPROVAL)
               for key in ("send-1", "send-2")]
    preserved = [await register_side_effect(repos, lease, str(status), status)
                 for status in (ActionStatus.PLANNED, ActionStatus.RUNNING, ActionStatus.UNKNOWN,
                                ActionStatus.SUCCEEDED, ActionStatus.FAILED)]
    await repos.tasks.commit(
        lease, TaskOutcome(status=TaskStatus.WAITING, wait_condition=WaitCondition.APPROVAL), T0
    )
    if requeue_first:
        await repos.tasks.requeue(task.scope_id, task.id, T0)
    now = T0 + timedelta(seconds=1)
    cancelled = await repos.tasks.request_cancel(task.scope_id, task.id, "cancel the task", now)
    assert cancelled.status is TaskStatus.CANCELLED
    for action in pending:
        stored = await repos.actions.get(task.id, action.logical_key)
        assert stored.status is ActionStatus.FAILED and stored.updated_at == now
    for action in preserved:
        assert await repos.actions.get(task.id, action.logical_key) == action
    assert await repos.actions.get(peer.id, peer_action.logical_key) == peer_action


async def test_cancelling_active_attempt_preserves_pending_action(repos):
    task, lease = await claimed(repos)
    action = await register_side_effect(repos, lease, "pending", ActionStatus.NEEDS_APPROVAL)
    now = T0 + timedelta(seconds=1)
    cancelling = await repos.tasks.request_cancel(task.scope_id, task.id, "cancel now", now)
    assert cancelling.status is TaskStatus.CANCELLING
    assert await repos.actions.get(task.id, action.logical_key) == action


async def pending_approval(repos):
    task, lease = await claimed(repos)
    action = await register_side_effect(repos, lease, "pending", ActionStatus.NEEDS_APPROVAL)
    waiting = await repos.tasks.commit(
        lease, TaskOutcome(status=TaskStatus.WAITING, wait_condition=WaitCondition.APPROVAL), T0
    )
    return waiting, action


async def test_cancelled_task_refuses_replay_of_a_recorded_approval(repos):
    task, action = await pending_approval(repos)
    decision = dict(request_hash=action.request_hash, approver=task.actor_id, decision="approve",
                    reason="", expires_at=T0 + timedelta(hours=1), now=T0)
    await repos.tasks.decide_approval(task.scope_id, task.id, action.id, **decision)
    cancelled = await repos.tasks.request_cancel(task.scope_id, task.id, "stop", T0)
    events = await repos.events.list(task.scope_id, task.id)
    with pytest.raises(InvalidTransition):
        await repos.tasks.decide_approval(task.scope_id, task.id, action.id, **decision)
    assert cancelled.status is TaskStatus.CANCELLED
    assert await repos.tasks.get(task.scope_id, task.id) == cancelled
    assert await repos.events.list(task.scope_id, task.id) == events


async def test_queued_repeat_requires_same_approver_and_unexpired_decision(repos):
    task, action = await pending_approval(repos)
    expiry = T0 + timedelta(minutes=1)
    decision = dict(request_hash=action.request_hash, decision="approve", reason="", expires_at=expiry)
    queued, original = await repos.tasks.decide_approval(
        task.scope_id, task.id, action.id, approver=task.actor_id, now=T0, **decision
    )
    history = await repos.events.list(task.scope_id, task.id)
    for approver, now in [("another-approver", T0), (task.actor_id, expiry)]:
        with pytest.raises(InvalidTransition):
            await repos.tasks.decide_approval(task.scope_id, task.id, action.id, approver=approver,
                                              now=now, **decision)
    assert await repos.approvals.get(action.id, action.request_hash) == original
    assert await repos.tasks.get(task.scope_id, task.id) == queued
    assert await repos.events.list(task.scope_id, task.id) == history


async def test_reparked_approval_can_change_to_reject_only_after_expiry(repos):
    task, action = await pending_approval(repos)
    expiry = T0 + timedelta(minutes=1)
    arguments = dict(request_hash=action.request_hash, approver=task.actor_id, reason="")
    await repos.tasks.decide_approval(task.scope_id, task.id, action.id, decision="approve",
                                      expires_at=expiry, now=T0, **arguments)
    replay = await repos.tasks.claim("replay", [task.run_lane], TTL, T0)
    reparked = await repos.tasks.commit(
        replay, TaskOutcome(status=TaskStatus.WAITING, wait_condition=WaitCondition.APPROVAL), T0
    )
    before = await repos.events.list(task.scope_id, task.id)
    with pytest.raises(ApprovalConflict):
        await repos.tasks.decide_approval(task.scope_id, task.id, action.id, decision="approve",
                                          expires_at=expiry, now=expiry - timedelta(seconds=1), **arguments)
    assert await repos.events.list(task.scope_id, task.id) == before
    queued, rejection = await repos.tasks.decide_approval(
        task.scope_id, task.id, action.id, decision="reject", expires_at=None, now=expiry, **arguments
    )
    assert queued.status is TaskStatus.QUEUED and queued.version == reparked.version + 1
    assert rejection.decision is ApprovalDecision.REJECT and rejection.expires_at is None
    assert rejection.decided_at == expiry
    assert await repos.approvals.get(action.id, action.request_hash) == rejection
    history = await repos.events.list(task.scope_id, task.id)
    assert history[:len(before)] == before
    assert [e.payload["decision"] for e in history if e.type is EventType.APPROVAL_DECIDED] == ["approve", "reject"]
