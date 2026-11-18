"""Reconciliation discovery and release on both repository implementations."""

import asyncio

import pytest

from harness.contracts import (
    Action, ActionStatus, Event, EventType, NotFound, Task, TaskStatus, Usage, UsageKind, WaitCondition,
)
from harness.storage.repositories import round_attempts

from ..integration.test_postgres import pools
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
    action = await action_for(repos, task, ActionStatus.UNKNOWN)
    snapshot = await repos.tasks.list_reconciliation_candidates()
    assert [item.id for item in snapshot] == [task.id]
    cancelled = await repos.tasks.request_cancel(task.scope_id, task.id, "stop", T0)
    before_events = await repos.events.list(task.scope_id, task.id)
    assert await decide_action(repos, task, action, ActionStatus.SUCCEEDED) is not None
    result = await repos.tasks.get(task.scope_id, task.id)
    assert result == cancelled
    assert result.status is TaskStatus.CANCELLED
    events = await repos.events.list(task.scope_id, task.id)
    assert [event for event in events if event.type is not EventType.TOOL_RECONCILE] == before_events


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


async def decide_action(repos, task, action, status, result_status=None, expected_attempt=1):
    event = Event(
        scope_id=task.scope_id, task_id=task.id, attempt_id=action.attempt_id,
        type=EventType.TOOL_RECONCILE, occurred_at=T0, received_at=T0,
        payload={"action_id": action.id, "status": result_status or status.value, "actor": "u1"},
    )
    return await repos.actions.decide(
        task.scope_id, task.id, action.id, status, result_ref="result", receipt_ref="receipt",
        usage=Usage(tool_calls=1), event=event, now=T0, expected_attempt=expected_attempt,
    )


@pytest.mark.parametrize("status", [ActionStatus.SUCCEEDED, ActionStatus.FAILED])
async def test_decide_atomically_settles_or_releases_and_requeues(repos, status):
    task = await task_in_state(repos, "waiting", TaskStatus.WAITING, WaitCondition.RECONCILIATION)
    action = await action_for(repos, task, ActionStatus.UNKNOWN)
    for call_id in (action.id, f"{action.id}.retry-next", "unrelated"):
        await repos.usage.reserve(task.id, action.attempt_id, call_id, UsageKind.TOOL, Usage(tool_calls=1), T0)
    got = await decide_action(repos, task, action, status)
    assert got.status is status
    assert got.result_ref == "result" and got.receipt_ref == "receipt"
    updated = await repos.tasks.get(task.scope_id, task.id)
    assert updated.status is TaskStatus.QUEUED
    assert updated.usage.tool_calls == (2 if status is ActionStatus.SUCCEEDED else 0)
    assert [r.call_id for r in await repos.usage.open_reservations(task.id)] == ["unrelated"]
    events = await repos.events.list(task.scope_id, task.id)
    decisions = [e for e in events if e.type is EventType.TOOL_RECONCILE]
    assert len(decisions) == 1 and decisions[0].payload["attempt"] == 1
    assert len([e for e in events if e.type is EventType.TASK_STATUS_CHANGED]) == 1


@pytest.mark.parametrize("status,result_status", [
    (ActionStatus.UNKNOWN, "unknown"), (ActionStatus.RUNNING, "unsupported"),
])
async def test_decide_uncertain_keeps_reservation_and_waiting(repos, status, result_status):
    task = await task_in_state(repos, "waiting", TaskStatus.WAITING, WaitCondition.RECONCILIATION)
    action = await action_for(repos, task, ActionStatus.RUNNING)
    await repos.usage.reserve(task.id, action.attempt_id, action.id, UsageKind.TOOL, Usage(tool_calls=1), T0)
    before = await repos.usage.open_reservations(task.id)
    for attempt in (1, 2):
        assert (await decide_action(repos, task, action, status, result_status, attempt)).status is status
    assert await repos.usage.open_reservations(task.id) == before
    assert await repos.tasks.get(task.scope_id, task.id) == task
    events = await repos.events.list(task.scope_id, task.id)
    assert [e.payload["attempt"] for e in events if e.type is EventType.TOOL_RECONCILE] == [1, 2]


@pytest.mark.parametrize("terminal", [ActionStatus.SUCCEEDED, ActionStatus.FAILED])
@pytest.mark.parametrize("incoming", [ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.UNKNOWN, ActionStatus.RUNNING])
async def test_decide_terminal_is_noop(repos, terminal, incoming):
    task = await task_in_state(repos, "waiting", TaskStatus.WAITING, WaitCondition.RECONCILIATION)
    action = await action_for(repos, task, terminal)
    await repos.usage.reserve(task.id, action.attempt_id, action.id, UsageKind.TOOL, Usage(tool_calls=1), T0)
    events = await repos.events.list(task.scope_id, task.id)
    reservations = await repos.usage.open_reservations(task.id)
    assert await decide_action(repos, task, action, incoming) is None
    assert await repos.actions.get(task.id, action.logical_key) == action
    assert await repos.tasks.get(task.scope_id, task.id) == task
    assert await repos.events.list(task.scope_id, task.id) == events
    assert await repos.usage.open_reservations(task.id) == reservations


async def test_decide_checks_scope_and_action_ownership(repos):
    task = await task_in_state(repos, "waiting", TaskStatus.WAITING, WaitCondition.RECONCILIATION)
    other = await task_in_state(repos, "other", TaskStatus.WAITING, WaitCondition.RECONCILIATION, OTHER)
    action = await action_for(repos, task, ActionStatus.UNKNOWN)
    with pytest.raises(NotFound):
        await decide_action(repos, other, action, ActionStatus.SUCCEEDED)
    wrong_scope = task.model_copy(update={"scope_id": OTHER.scope_id})
    with pytest.raises(NotFound):
        await decide_action(repos, wrong_scope, action, ActionStatus.SUCCEEDED)
    missing = action.model_copy(update={"id": "missing"})
    with pytest.raises(NotFound):
        await decide_action(repos, task, missing, ActionStatus.SUCCEEDED)
    assert await repos.actions.get(task.id, action.logical_key) == action


@pytest.mark.integration
async def test_concurrent_decide_once(pools):
    task = await task_in_state(pools[0], "waiting", TaskStatus.WAITING, WaitCondition.RECONCILIATION)
    action = await action_for(pools[0], task, ActionStatus.UNKNOWN)
    await pools[0].usage.reserve(task.id, action.attempt_id, action.id, UsageKind.TOOL, Usage(tool_calls=1), T0)
    results = await asyncio.gather(*(
        decide_action(pools[index], task, action, ActionStatus.SUCCEEDED) for index in range(2)
    ))
    assert sum(result is not None for result in results) == 1
    got = await pools[0].tasks.get(task.scope_id, task.id)
    assert got.status is TaskStatus.QUEUED and got.usage.tool_calls == 1
    assert await pools[0].usage.open_reservations(task.id) == []
    events = await pools[0].events.list(task.scope_id, task.id)
    assert len([event for event in events if event.type is EventType.TOOL_RECONCILE]) == 1
    assert len([event for event in events if event.type is EventType.TASK_STATUS_CHANGED]) == 1


@pytest.mark.parametrize("statuses,expected", [
    ([], 0), (["unknown"], 1), (["unknown", "unsupported"], 2),
    (["unknown"] * 5 + ["failed"], 0),
    (["unknown", "succeeded", "unknown"], 1),
    (["unknown", "failed", "unsupported", "failed", "unknown", "unknown"], 2),
])
async def test_round_attempts_counts_current_round(statuses, expected):
    assert round_attempts(iter(statuses)) == expected


@pytest.mark.parametrize("status,condition", [
    (TaskStatus.RUNNING, None), (TaskStatus.QUEUED, None), (TaskStatus.CANCELLING, None),
    (TaskStatus.SUCCEEDED, None), (TaskStatus.FAILED, None),
    (TaskStatus.WAITING, WaitCondition.INPUT), (TaskStatus.WAITING, WaitCondition.APPROVAL),
])
async def test_decide_rejects_running_task(repos, status, condition):
    task = await task_in_state(repos, "live", status, condition)
    action = await action_for(repos, task, ActionStatus.RUNNING)
    await repos.usage.reserve(task.id, action.attempt_id, action.id, UsageKind.TOOL, Usage(tool_calls=1), T0)
    reservations = await repos.usage.open_reservations(task.id)
    events = await repos.events.list(task.scope_id, task.id)
    assert await decide_action(repos, task, action, ActionStatus.FAILED) is None
    assert await repos.actions.get(task.id, action.logical_key) == action
    assert await repos.tasks.get(task.scope_id, task.id) == task
    assert await repos.usage.open_reservations(task.id) == reservations
    assert await repos.events.list(task.scope_id, task.id) == events


async def test_decide_resets_round_after_terminal_event(repos):
    task = await task_in_state(repos, "waiting", TaskStatus.WAITING, WaitCondition.RECONCILIATION)
    action = await action_for(repos, task, ActionStatus.UNKNOWN)
    for status in ["unknown"] * 5 + ["failed"]:
        await repos.events.append(Event(
            scope_id=task.scope_id, task_id=task.id, type=EventType.TOOL_RECONCILE,
            payload={"action_id": action.id, "status": status}, occurred_at=T0, received_at=T0,
        ))
    assert (await decide_action(repos, task, action, ActionStatus.UNKNOWN)).status is ActionStatus.UNKNOWN
    events = await repos.events.list(task.scope_id, task.id)
    assert events[-1].payload["attempt"] == 1


async def test_decide_expected_attempt_mismatch_writes_nothing(repos):
    task = await task_in_state(repos, "waiting", TaskStatus.WAITING, WaitCondition.RECONCILIATION)
    action = await action_for(repos, task, ActionStatus.UNKNOWN)
    await repos.usage.reserve(task.id, action.attempt_id, action.id, UsageKind.TOOL, Usage(tool_calls=1), T0)
    reservations = await repos.usage.open_reservations(task.id)
    events = await repos.events.list(task.scope_id, task.id)
    assert await decide_action(repos, task, action, ActionStatus.SUCCEEDED, expected_attempt=2) is None
    assert await repos.actions.get(task.id, action.logical_key) == action
    assert await repos.tasks.get(task.scope_id, task.id) == task
    assert await repos.usage.open_reservations(task.id) == reservations
    assert await repos.events.list(task.scope_id, task.id) == events


async def assert_concurrent_unknown_once(first, second):
    task = await task_in_state(first, "waiting", TaskStatus.WAITING, WaitCondition.RECONCILIATION)
    action = await action_for(first, task, ActionStatus.UNKNOWN)
    await first.usage.reserve(task.id, action.attempt_id, action.id, UsageKind.TOOL, Usage(tool_calls=1), T0)
    before = await first.usage.open_reservations(task.id)
    results = await asyncio.gather(*(
        decide_action(store, task, action, ActionStatus.UNKNOWN) for store in (first, second)
    ))
    assert sum(result is not None for result in results) == 1
    assert await first.usage.open_reservations(task.id) == before
    assert await first.tasks.get(task.scope_id, task.id) == task
    events = await first.events.list(task.scope_id, task.id)
    decisions = [event for event in events if event.type is EventType.TOOL_RECONCILE]
    assert len(decisions) == 1 and decisions[0].payload["attempt"] == 1


async def test_concurrent_unknown_counted_once(repos):
    await assert_concurrent_unknown_once(repos, repos)


@pytest.mark.integration
async def test_concurrent_unknown_counted_once_separate_connections(pools):
    await assert_concurrent_unknown_once(pools[0], pools[1])
