"""Reconciliation discovery and release on both repository implementations."""

import pytest

from harness.contracts import (
    Action, ActionStatus, EventType, NotFound, Task, TaskStatus, WaitCondition,
)

from .test_repositories import ACTOR, OTHER, T0, make_task, repos

pytestmark = pytest.mark.asyncio


async def task_in_state(repos, key, status, condition, actor=ACTOR):
    task = Task.model_validate({
        **make_task(key=key, actor=actor).model_dump(),
        "status": status, "wait_condition": condition,
    })
    return (await repos.tasks.create(task)).task


async def action_for(repos, task, status, read_only=False, key="step-1.0"):
    action = Action(
        task_id=task.id, attempt_id="previous-attempt", logical_key=key,
        tool="external.write", request_hash="hash", read_only=read_only,
        status=status, created_at=T0, updated_at=T0,
    )
    return (await repos.actions.register(action)).action


async def test_candidates_lists_waiting_and_dirty_cancelled(repos):
    expected = []
    for actor in (OTHER, ACTOR):
        waiting = await task_in_state(repos, "waiting", TaskStatus.WAITING,
                                      WaitCondition.RECONCILIATION, actor)
        expected.append(waiting)
        for status in (ActionStatus.PLANNED, ActionStatus.RUNNING, ActionStatus.UNKNOWN):
            dirty = await task_in_state(repos, f"dirty-{status}", TaskStatus.CANCELLED, None, actor)
            await action_for(repos, dirty, status)
            await action_for(repos, dirty, status, key="step-1.1")
            expected.append(dirty)
        for condition in (WaitCondition.APPROVAL, WaitCondition.INPUT, WaitCondition.DEPENDENCY):
            await task_in_state(repos, f"waiting-{condition}", TaskStatus.WAITING, condition, actor)
        for status in (ActionStatus.FAILED, ActionStatus.SUCCEEDED):
            clean = await task_in_state(repos, f"settled-{status}", TaskStatus.CANCELLED, None, actor)
            await action_for(repos, clean, status)
        readonly = await task_in_state(repos, "readonly", TaskStatus.CANCELLED, None, actor)
        await action_for(repos, readonly, ActionStatus.UNKNOWN, read_only=True)
        await task_in_state(repos, "empty-cancelled", TaskStatus.CANCELLED, None, actor)
        for status in (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.CANCELLING,
                       TaskStatus.SUCCEEDED, TaskStatus.FAILED):
            ignored = await task_in_state(repos, f"ignored-{status}", status, None, actor)
            await action_for(repos, ignored, ActionStatus.UNKNOWN)
    found = await repos.tasks.list_reconciliation_candidates()
    assert found == sorted(expected, key=lambda task: (task.scope_id, task.id))
    found[0].goal = "mutated copy"
    assert (await repos.tasks.get(found[0].scope_id, found[0].id)).goal == "draft"


async def test_list_unsettled_filters_and_returns_copies(repos):
    task = await task_in_state(repos, "waiting", TaskStatus.WAITING, WaitCondition.RECONCILIATION)
    other = await task_in_state(repos, "other", TaskStatus.WAITING, WaitCondition.RECONCILIATION, OTHER)
    expected = []
    for status in ActionStatus:
        action = await action_for(repos, task, status, key=f"write-{status}")
        await action_for(repos, task, status, read_only=True, key=f"read-{status}")
        await action_for(repos, other, status, key=f"other-{status}")
        if status in (ActionStatus.PLANNED, ActionStatus.RUNNING, ActionStatus.UNKNOWN):
            expected.append(action)
    found = await repos.actions.list_unsettled(task.id)
    assert found == sorted(expected, key=lambda action: (action.created_at, action.id))
    found[0].tool = "changed"
    assert (await repos.actions.get(task.id, found[0].logical_key)).tool == "external.write"


async def test_release_rechecks_status_under_lock(repos):
    task = await task_in_state(repos, "waiting", TaskStatus.WAITING, WaitCondition.RECONCILIATION)
    snapshot = await repos.tasks.list_reconciliation_candidates()
    assert [item.id for item in snapshot] == [task.id]
    cancelled = await repos.tasks.request_cancel(task.scope_id, task.id, "stop", T0)
    before_events = await repos.events.list(task.scope_id, task.id)
    result = await repos.tasks.release_reconciliation(task.scope_id, task.id, T0)
    assert result == cancelled
    assert result.status is TaskStatus.CANCELLED
    assert await repos.events.list(task.scope_id, task.id) == before_events


async def test_release_waits_for_all_actions_and_releases_once(repos):
    task = await task_in_state(repos, "waiting", TaskStatus.WAITING, WaitCondition.RECONCILIATION)
    first = await action_for(repos, task, ActionStatus.UNKNOWN)
    second = await action_for(repos, task, ActionStatus.PLANNED, key="step-1.1")
    await action_for(repos, task, ActionStatus.RUNNING, read_only=True, key="read")
    assert await repos.tasks.release_reconciliation(task.scope_id, task.id, T0) == task
    await repos.actions.update(first.id, ActionStatus.SUCCEEDED, now=T0)
    assert await repos.tasks.release_reconciliation(task.scope_id, task.id, T0) == task
    await repos.actions.update(second.id, ActionStatus.FAILED, now=T0)
    released = await repos.tasks.release_reconciliation(task.scope_id, task.id, T0)
    assert released.status is TaskStatus.QUEUED
    assert released.wait_condition is None
    assert released.version == task.version + 1
    assert await repos.tasks.release_reconciliation(task.scope_id, task.id, T0) == released
    events = await repos.events.list(task.scope_id, task.id)
    changes = [event for event in events if event.type is EventType.TASK_STATUS_CHANGED]
    assert len(changes) == 1
    assert changes[0].payload == {"from": "waiting", "to": "queued"}


async def test_release_empty_reconciliation_task(repos):
    task = await task_in_state(repos, "waiting", TaskStatus.WAITING, WaitCondition.RECONCILIATION)
    assert (await repos.tasks.release_reconciliation(task.scope_id, task.id, T0)).status is TaskStatus.QUEUED


@pytest.mark.parametrize("condition", [WaitCondition.APPROVAL, WaitCondition.INPUT, WaitCondition.DEPENDENCY])
async def test_release_keeps_other_wait_conditions(repos, condition):
    task = await task_in_state(repos, "waiting", TaskStatus.WAITING, condition)
    assert await repos.tasks.release_reconciliation(task.scope_id, task.id, T0) == task


async def test_release_reconciliation_checks_scope_and_existence(repos):
    task = await task_in_state(repos, "waiting", TaskStatus.WAITING, WaitCondition.RECONCILIATION)
    with pytest.raises(NotFound):
        await repos.tasks.release_reconciliation(OTHER.scope_id, task.id, T0)
    with pytest.raises(NotFound):
        await repos.tasks.release_reconciliation(ACTOR.scope_id, "missing", T0)
    assert await repos.tasks.get(task.scope_id, task.id) == task
