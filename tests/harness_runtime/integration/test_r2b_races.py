"""Race input resumption and approval through independent PostgreSQL pools."""

import asyncio
from datetime import timedelta

import pytest
import pytest_asyncio

from harness.contracts import (
    ActionStatus, ApprovalDecision, ErrorCode, EventType, HarnessError, Task, TaskOutcome, TaskStatus, WaitCondition,
)

from ..unit.test_r2b_repositories import register_side_effect
from ..unit.test_repositories import T0, claimed, repos

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.parametrize("repos", ["postgres"], indirect=True),
]


@pytest_asyncio.fixture
async def peer(repos, pg_dsn):
    other_pool = repos.from_dsn(pg_dsn)
    try:
        yield other_pool
    finally:
        await other_pool.close()


async def test_concurrent_resume_and_approval_once(repos, peer):
    task, lease = await claimed(repos)
    waiting = await repos.tasks.commit(
        lease, TaskOutcome(status=TaskStatus.WAITING, wait_condition=WaitCondition.INPUT), T0
    )
    cursor = (await peer.events.list(task.scope_id, task.id))[-1].seq
    refs = ["artifact:left", "artifact:right"]
    results = await asyncio.gather(*(
        store.tasks.resume(task.scope_id, task.id, expected_version=waiting.version,
                           input_refs=[ref], now=T0)
        for store, ref in zip((repos, peer), refs)
    ), return_exceptions=True)
    wins = [(result, ref) for result, ref in zip(results, refs) if isinstance(result, Task)]
    conflicts = [result for result in results if isinstance(result, HarnessError)]
    assert len(wins) == len(conflicts) == 1, results
    assert conflicts[0].code in (ErrorCode.VERSION_CONFLICT, ErrorCode.INVALID_TRANSITION)
    winner, ref = wins[0]
    assert winner.input_refs == [ref] and winner.version == waiting.version + 1
    assert winner.status is TaskStatus.QUEUED and winner.wait_condition is None
    assert await peer.tasks.get(task.scope_id, task.id) == winner
    events = await peer.events.list(task.scope_id, task.id, after_seq=cursor)
    assert [e.type for e in events] == [EventType.TASK_STATUS_CHANGED]
    assert events[0].payload["added_input_refs"] == [ref]
    assert events[0].payload["reason"] == "resumed_with_input"


async def test_concurrent_identical_approval_decides_once(repos, peer):
    task, lease = await claimed(repos)
    action = await register_side_effect(repos, lease, "send", ActionStatus.NEEDS_APPROVAL)
    waiting = await repos.tasks.commit(
        lease, TaskOutcome(status=TaskStatus.WAITING, wait_condition=WaitCondition.APPROVAL), T0
    )
    cursor = (await peer.events.list(task.scope_id, task.id))[-1].seq
    first, second = await asyncio.gather(*(
        store.tasks.decide_approval(
            task.scope_id, task.id, action.id, request_hash=action.request_hash, approver=task.actor_id,
            decision="approve", reason="reviewed", expires_at=T0 + timedelta(hours=1), now=T0,
        )
        for store in (repos, peer)
    ))
    assert first == second
    queued, approval = first
    assert queued.status is TaskStatus.QUEUED and queued.version == waiting.version + 1
    assert approval.decision is ApprovalDecision.APPROVE
    assert await peer.approvals.get(action.id, action.request_hash) == approval
    assert await peer.tasks.get(task.scope_id, task.id) == queued
    events = await peer.events.list(task.scope_id, task.id, after_seq=cursor)
    assert [e.type for e in events] == [EventType.APPROVAL_DECIDED, EventType.TASK_STATUS_CHANGED]
