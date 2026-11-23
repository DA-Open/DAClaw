"""Recovery races across independent PostgreSQL connections."""

import asyncio
from datetime import timedelta

import pytest

from harness.contracts import EventType, RunLane, TaskStatus

from .test_postgres import T0, TTL, make_task, pools

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_concurrent_reclaim_once(pools):
    task = (await pools[0].tasks.create(make_task("recover"))).task
    lease = await pools[0].tasks.claim("worker", [RunLane.FOREGROUND], TTL, T0)
    now = T0 + timedelta(seconds=TTL)
    counts = await asyncio.gather(
        pools[0].tasks.reclaim_expired(now, 3),
        pools[1].tasks.reclaim_expired(now, 3),
    )
    assert sorted(counts) == [0, 1]
    got = await pools[0].tasks.get(task.scope_id, task.id)
    assert got.status is TaskStatus.QUEUED
    assert got.current_attempt_id is None
    attempt = await pools[0].tasks.get_attempt(task.scope_id, lease.attempt_id)
    assert attempt.exit_reason == "lease_expired"
    assert attempt.ended_at == now
    events = await pools[0].events.list(task.scope_id, task.id)
    reclaimed = [event for event in events if event.type is EventType.TASK_STATUS_CHANGED
                 and event.payload["from"] == "running" and event.payload["to"] == "queued"]
    assert len(reclaimed) == 1
    assert reclaimed[0].attempt_id == lease.attempt_id
