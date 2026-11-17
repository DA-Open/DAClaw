"""Lease recovery has the same behaviour on memory and PostgreSQL."""

from datetime import timedelta

import pytest

from harness.config import HarnessConfig
from harness.contracts import (
    Action, ActionStatus, Artifact, ErrorCode, EventType, RunLane,
    StaleLease, TaskStatus, Usage, UsageKind, WaitCondition,
)
from harness.tasks.recovery import Recovery

from .test_repositories import T0, TTL, claimed, created, failed_outcome, repos

pytestmark = pytest.mark.asyncio
EXPIRED = T0 + timedelta(seconds=TTL)


async def add_action(repos, task, lease, status, read_only):
    action = Action(
        task_id=task.id, attempt_id=lease.attempt_id, logical_key="step-1.0",
        tool="files.read" if read_only else "external.write", request_hash="hash",
        read_only=read_only, status=status, created_at=T0, updated_at=T0,
    )
    await repos.actions.register(action)
    return action


async def test_reclaim_requeues_readonly_task(repos):
    task, lease = await claimed(repos)
    await add_action(repos, task, lease, ActionStatus.RUNNING, True)
    await repos.tasks.checkpoint(lease, "checkpoint-a", T0)
    await repos.usage.reserve(task.id, lease.attempt_id, "model-1", UsageKind.MODEL,
                              Usage(model_calls=1, input_tokens=7), T0)
    await repos.usage.settle("model-1", Usage(model_calls=1, input_tokens=7), T0)
    await repos.usage.reserve(task.id, lease.attempt_id, "open-tool", UsageKind.TOOL,
                              Usage(tool_calls=1), T0)
    before = await repos.tasks.get(task.scope_id, task.id)
    recovery = Recovery(repos.tasks, HarnessConfig(), lambda: T0)
    assert await recovery.tick(EXPIRED) == 1
    got = await repos.tasks.get(task.scope_id, task.id)
    attempt = await repos.tasks.get_attempt(task.scope_id, lease.attempt_id)
    assert got.status is TaskStatus.QUEUED
    assert got.current_attempt_id is None
    assert got.checkpoint_ref == "checkpoint-a"
    assert got.usage == before.usage
    assert got.deadline == before.deadline
    assert len(await repos.usage.open_reservations(task.id)) == 1
    assert attempt.exit_reason == "lease_expired"
    assert attempt.ended_at == EXPIRED
    assert got.version == before.version + 1
    events = await repos.events.list(task.scope_id, task.id)
    assert events[-1].type is EventType.TASK_STATUS_CHANGED
    assert events[-1].payload["from"] == "running"
    assert events[-1].payload["to"] == "queued"
    assert events[-1].attempt_id == lease.attempt_id
    assert await recovery.tick(EXPIRED) == 0


@pytest.mark.parametrize("status", [ActionStatus.PLANNED, ActionStatus.RUNNING, ActionStatus.UNKNOWN])
async def test_reclaim_parks_unfinished_side_effect(repos, status):
    task, lease = await claimed(repos)
    await add_action(repos, task, lease, status, False)
    assert await repos.tasks.reclaim_expired(EXPIRED, 1) == 1
    got = await repos.tasks.get(task.scope_id, task.id)
    assert got.status is TaskStatus.WAITING
    assert got.wait_condition is WaitCondition.RECONCILIATION
    assert got.current_attempt_id is None
    assert (await repos.tasks.get_attempt(task.scope_id, lease.attempt_id)).exit_reason == "lease_expired"


@pytest.mark.parametrize("status", [ActionStatus.SUCCEEDED, ActionStatus.FAILED])
async def test_reclaim_requeues_settled_side_effect(repos, status):
    task, lease = await claimed(repos)
    await add_action(repos, task, lease, status, False)
    assert await repos.tasks.reclaim_expired(EXPIRED, 3) == 1
    assert (await repos.tasks.get(task.scope_id, task.id)).status is TaskStatus.QUEUED


async def test_reclaim_fails_after_max_attempts(repos):
    task, lease = await claimed(repos)
    artifact = await repos.artifacts.add(Artifact(
        scope_id=task.scope_id, task_id=task.id, name="draft", uri="file:///draft",
        media_type="text/plain", content_hash="sha256:x", size_bytes=3,
    ))
    for attempt_number in range(1, 4):
        now = T0 + timedelta(seconds=TTL * attempt_number)
        assert await repos.tasks.reclaim_expired(now, 3) == 1
        got = await repos.tasks.get(task.scope_id, task.id)
        assert got.current_attempt_id is None
        assert (await repos.tasks.get_attempt(task.scope_id, lease.attempt_id)).exit_reason == "lease_expired"
        if attempt_number < 3:
            assert got.status is TaskStatus.QUEUED
            lease = await repos.tasks.claim("next-worker", [RunLane.FOREGROUND], TTL, now)
    assert got.status is TaskStatus.FAILED
    assert got.error.code is ErrorCode.LEASE_LOST
    assert await repos.artifacts.get(task.scope_id, artifact.id) == artifact


async def test_reclaim_cancelling_becomes_cancelled(repos):
    task, lease = await claimed(repos)
    await add_action(repos, task, lease, ActionStatus.UNKNOWN, False)
    await repos.tasks.request_cancel(task.scope_id, task.id, "stop", T0)
    assert await repos.tasks.reclaim_expired(EXPIRED, 1) == 1
    got = await repos.tasks.get(task.scope_id, task.id)
    assert got.status is TaskStatus.CANCELLED
    assert got.wait_condition is None
    assert got.current_attempt_id is None
    assert (await repos.tasks.get_attempt(task.scope_id, lease.attempt_id)).exit_reason == "lease_expired"


async def test_old_worker_rejected_after_reclaim(repos):
    task, lease = await claimed(repos)
    await repos.tasks.reclaim_expired(EXPIRED, 3)
    new_lease = await repos.tasks.claim("new-worker", [RunLane.FOREGROUND], TTL, EXPIRED)
    assert new_lease.lease_epoch > lease.lease_epoch
    with pytest.raises(StaleLease):
        await repos.tasks.heartbeat(lease, TTL, EXPIRED)
    with pytest.raises(StaleLease):
        await repos.tasks.commit(lease, failed_outcome(), EXPIRED)
    assert (await repos.tasks.get(task.scope_id, task.id)).current_attempt_id == new_lease.attempt_id


async def test_reclaim_ignores_live_lease(repos):
    task, lease = await claimed(repos)
    await created(repos, key="queued")
    assert await repos.tasks.reclaim_expired(EXPIRED - timedelta(microseconds=1), 3) == 0
    got = await repos.tasks.get(task.scope_id, task.id)
    assert got.status is TaskStatus.RUNNING
    assert got.current_attempt_id == lease.attempt_id
    assert (await repos.tasks.get_attempt(task.scope_id, lease.attempt_id)).ended_at is None


async def test_reclaim_only_checks_current_attempt_actions(repos):
    task, first = await claimed(repos)
    await repos.tasks.reclaim_expired(EXPIRED, 3)
    second = await repos.tasks.claim("new-worker", [RunLane.FOREGROUND], TTL, EXPIRED)
    await add_action(repos, task, first, ActionStatus.UNKNOWN, False)
    assert await repos.tasks.reclaim_expired(EXPIRED + timedelta(seconds=TTL), 3) == 1
    assert (await repos.tasks.get(task.scope_id, task.id)).status is TaskStatus.QUEUED
    assert (await repos.tasks.get_attempt(task.scope_id, second.attempt_id)).exit_reason == "lease_expired"
