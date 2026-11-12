"""Behaviour every Repositories implementation must have.

Runs against InMemoryRepositories now. The PostgreSQL implementation will be
added to the ``repos`` fixture params and must pass the same tests.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from harness.contracts import (
    Action,
    ActionConflict,
    ActionStatus,
    Actor,
    AgentRole,
    Artifact,
    ErrorCode,
    Event,
    EventType,
    IdempotencyConflict,
    InvalidTransition,
    Limits,
    NotFound,
    RemainingBudget,
    ReservationStatus,
    RunLane,
    StaleLease,
    Task,
    TaskError,
    TaskOrigin,
    TaskOutcome,
    TaskRequest,
    TaskStatus,
    TriggerType,
    Usage,
    UsageKind,
    VerificationRequired,
    VerificationResult,
    VerificationStatus,
)
from harness.storage.memory import InMemoryRepositories

pytestmark = pytest.mark.asyncio

T0 = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)
TTL = 60
ACTOR = Actor(actor_id="u1", scope_id="s1")
OTHER = Actor(actor_id="u2", scope_id="s2")


@pytest.fixture(params=["memory"])
def repos(request):
    return InMemoryRepositories()


def make_task(key="k1", goal="draft", actor=ACTOR, lane=RunLane.FOREGROUND, priority=0, limits=None, at=T0):
    req = TaskRequest(
        request_key=key,
        goal=goal,
        capability_ref="document_draft@1",
        limits=limits or Limits(),
    )
    origin = TaskOrigin(
        agent_role=AgentRole.MAIN,
        agent_profile_ref="main@1",
        run_lane=lane,
        trigger_type=TriggerType.USER,
        priority=priority,
    )
    return Task.new(req, actor, origin, now=at)


async def created(repos, **kw):
    return (await repos.tasks.create(make_task(**kw))).task


async def claimed(repos, **kw):
    task = await created(repos, **kw)
    lease = await repos.tasks.claim("w1", [task.run_lane], TTL, T0)
    assert lease is not None and lease.task_id == task.id
    return task, lease


def failed_outcome():
    return TaskOutcome(status=TaskStatus.FAILED, error=TaskError(code=ErrorCode.TOOL_ERROR, message="x"))


async def statuses(repos, task_id, scope="s1"):
    events = await repos.events.list(scope, task_id=task_id)
    return [(e.type, e.payload.get("to")) for e in events]


# -- create ------------------------------------------------------------------


async def test_create_and_get(repos):
    result = await repos.tasks.create(make_task())
    assert result.created
    got = await repos.tasks.get("s1", result.task.id)
    assert got == result.task
    assert await statuses(repos, got.id) == [(EventType.TASK_CREATED, None)]


async def test_same_request_returns_original_task(repos):
    first = await repos.tasks.create(make_task())
    again = await repos.tasks.create(make_task())
    assert not again.created
    assert again.task.id == first.task.id


async def test_same_key_different_content_conflicts(repos):
    await repos.tasks.create(make_task())
    with pytest.raises(IdempotencyConflict):
        await repos.tasks.create(make_task(goal="something else"))


async def test_same_key_in_another_scope_is_a_new_task(repos):
    a = await created(repos)
    b = await created(repos, actor=OTHER)
    assert a.id != b.id


async def test_other_scope_cannot_read(repos):
    task = await created(repos)
    with pytest.raises(NotFound):
        await repos.tasks.get("s2", task.id)
    assert await repos.events.list("s2", task_id=task.id) == []


async def test_returned_objects_are_copies(repos):
    task = await created(repos)
    task.goal = "changed"
    assert (await repos.tasks.get("s1", task.id)).goal == "draft"


# -- claim -----------------------------------------------------------------------


async def test_claim_sets_running_and_epoch(repos):
    task, lease = await claimed(repos)
    stored = await repos.tasks.get("s1", task.id)
    assert stored.status is TaskStatus.RUNNING
    assert stored.lease_epoch == lease.lease_epoch == 1
    assert stored.current_attempt_id == lease.attempt_id
    assert stored.version == 2
    assert lease.lease_expires_at == T0 + timedelta(seconds=TTL)


async def test_claim_respects_lane_priority_and_age(repos):
    old = await created(repos, key="old", at=T0)
    new_high = await created(repos, key="high", priority=5, at=T0 + timedelta(seconds=1))
    background = await created(repos, key="bg", lane=RunLane.BACKGROUND, priority=9)
    first = await repos.tasks.claim("w1", [RunLane.FOREGROUND], TTL, T0)
    second = await repos.tasks.claim("w1", [RunLane.FOREGROUND], TTL, T0)
    assert [first.task_id, second.task_id] == [new_high.id, old.id]
    assert await repos.tasks.claim("w1", [RunLane.FOREGROUND], TTL, T0) is None
    third = await repos.tasks.claim("w2", [RunLane.BACKGROUND], TTL, T0)
    assert third.task_id == background.id


async def test_concurrent_claims_never_share_a_task(repos):
    for i in range(5):
        await created(repos, key=f"k{i}")
    leases = await asyncio.gather(
        *(repos.tasks.claim(f"w{i}", [RunLane.FOREGROUND], TTL, T0) for i in range(12))
    )
    got = [lease.task_id for lease in leases if lease is not None]
    assert len(got) == 5 and len(set(got)) == 5


# -- heartbeat and stale leases ----------------------------------------------------


async def test_heartbeat_extends_lease(repos):
    task, lease = await claimed(repos)
    later = T0 + timedelta(seconds=50)
    stored = await repos.tasks.heartbeat(lease, TTL, later)
    assert stored.id == task.id and stored.version == 2
    # the extended lease is still valid after the original expiry
    await repos.tasks.heartbeat(lease, TTL, T0 + timedelta(seconds=100))


async def test_expired_lease_is_rejected(repos):
    _, lease = await claimed(repos)
    with pytest.raises(StaleLease):
        await repos.tasks.heartbeat(lease, TTL, T0 + timedelta(seconds=TTL))
    with pytest.raises(StaleLease):
        await repos.tasks.commit(lease, failed_outcome(), T0 + timedelta(seconds=TTL + 1))


async def test_old_worker_cannot_commit_after_requeue(repos):
    task, old_lease = await claimed(repos)
    await repos.tasks.commit(old_lease, TaskOutcome(status=TaskStatus.QUEUED), T0)
    new_lease = await repos.tasks.claim("w2", [RunLane.FOREGROUND], TTL, T0)
    assert new_lease.lease_epoch == 2
    with pytest.raises(StaleLease):
        await repos.tasks.commit(old_lease, failed_outcome(), T0)
    done = await repos.tasks.commit(new_lease, failed_outcome(), T0)
    assert done.status is TaskStatus.FAILED and done.lease_epoch == 2


async def test_committed_lease_is_finished(repos):
    _, lease = await claimed(repos)
    await repos.tasks.commit(lease, failed_outcome(), T0)
    with pytest.raises(StaleLease):
        await repos.tasks.heartbeat(lease, TTL, T0)


# -- commit ------------------------------------------------------------------------


async def test_waiting_outcome(repos):
    task, lease = await claimed(repos)
    stored = await repos.tasks.commit(
        lease, TaskOutcome(status=TaskStatus.WAITING, wait_condition="input", checkpoint_ref="ckpt_1"), T0
    )
    assert stored.status is TaskStatus.WAITING and stored.checkpoint_ref == "ckpt_1"
    assert stored.current_attempt_id is None


async def test_failed_outcome_records_error_and_artifacts(repos):
    task, lease = await claimed(repos)
    outcome = TaskOutcome(
        status=TaskStatus.FAILED,
        error=TaskError(code=ErrorCode.BUDGET_EXHAUSTED, message="max_model_calls"),
        artifact_refs=["art_partial"],
    )
    stored = await repos.tasks.commit(lease, outcome, T0)
    assert stored.error.code is ErrorCode.BUDGET_EXHAUSTED
    events = await repos.events.list("s1", task_id=task.id)
    last = events[-1]
    assert last.payload["to"] == "failed"
    assert last.payload["error_code"] == "budget_exhausted"
    assert last.payload["artifact_refs"] == ["art_partial"]
    assert last.attempt_id == lease.attempt_id


async def test_succeeded_needs_matching_passed_verification(repos):
    task, lease = await claimed(repos)
    outcome = TaskOutcome(status=TaskStatus.SUCCEEDED, result_ref="art_1", verification_ref="ver_missing")
    with pytest.raises(VerificationRequired):
        await repos.tasks.commit(lease, outcome, T0)

    failed = await repos.verifications.add(
        VerificationResult(task_id=task.id, result_ref="art_1", checker_version="1", status=VerificationStatus.FAILED)
    )
    with pytest.raises(VerificationRequired):
        await repos.tasks.commit(lease, outcome.model_copy(update={"verification_ref": failed.id}), T0)

    other_result = await repos.verifications.add(
        VerificationResult(task_id=task.id, result_ref="art_2", checker_version="1", status=VerificationStatus.PASSED)
    )
    with pytest.raises(VerificationRequired):
        await repos.tasks.commit(lease, outcome.model_copy(update={"verification_ref": other_result.id}), T0)

    passed = await repos.verifications.add(
        VerificationResult(task_id=task.id, result_ref="art_1", checker_version="1", status=VerificationStatus.PASSED)
    )
    stored = await repos.tasks.commit(lease, outcome.model_copy(update={"verification_ref": passed.id}), T0)
    assert stored.status is TaskStatus.SUCCEEDED and stored.result_ref == "art_1"


async def test_status_events_follow_every_change(repos):
    task, lease = await claimed(repos)
    await repos.tasks.commit(lease, failed_outcome(), T0)
    assert await statuses(repos, task.id) == [
        (EventType.TASK_CREATED, None),
        (EventType.TASK_STATUS_CHANGED, "running"),
        (EventType.TASK_STATUS_CHANGED, "failed"),
    ]


# -- cancel ------------------------------------------------------------------------


async def test_cancel_queued_task(repos):
    task = await created(repos)
    stored = await repos.tasks.request_cancel("s1", task.id, "user", T0)
    assert stored.status is TaskStatus.CANCELLED and stored.cancel_reason == "user"
    again = await repos.tasks.request_cancel("s1", task.id, "user", T0)
    assert again.version == stored.version
    assert await repos.tasks.claim("w1", [RunLane.FOREGROUND], TTL, T0) is None


async def test_cancel_running_task_goes_through_cancelling(repos):
    task, lease = await claimed(repos)
    stored = await repos.tasks.request_cancel("s1", task.id, "user", T0)
    assert stored.status is TaskStatus.CANCELLING
    seen = await repos.tasks.heartbeat(lease, TTL, T0)
    assert seen.cancel_requested_at == T0
    with pytest.raises(InvalidTransition):
        await repos.tasks.commit(lease, failed_outcome(), T0)
    done = await repos.tasks.commit(lease, TaskOutcome(status=TaskStatus.CANCELLED), T0)
    assert done.status is TaskStatus.CANCELLED


async def test_cancel_other_scope_is_not_found(repos):
    task = await created(repos)
    with pytest.raises(NotFound):
        await repos.tasks.request_cancel("s2", task.id, "x", T0)


# -- events ------------------------------------------------------------------------


async def test_event_seq_and_resume_from(repos):
    task = await created(repos)
    for i in range(3):
        await repos.events.append(Event(scope_id="s1", task_id=task.id, type=EventType.MODEL_CALL, payload={"i": i}))
    events = await repos.events.list("s1", task_id=task.id)
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == 4
    after = await repos.events.list("s1", task_id=task.id, after_seq=seqs[1])
    assert [e.payload.get("i") for e in after] == [1, 2]


async def test_external_events_are_deduplicated(repos):
    event = Event(scope_id="s1", type=EventType.EXTERNAL, source="feishu", source_event_id="e1")
    first = await repos.events.append(event)
    again = await repos.events.append(event.model_copy(update={"id": "evt_other"}))
    assert again.id == first.id
    other_scope = await repos.events.append(event.model_copy(update={"scope_id": "s2", "id": "evt_s2"}))
    assert other_scope.id == "evt_s2"


# -- actions -----------------------------------------------------------------------


def make_action(task, lease, key="step-1:0", args_hash="sha256:a", read_only=True):
    return Action(
        task_id=task.id,
        attempt_id=lease.attempt_id,
        logical_key=key,
        tool="files.read",
        request_hash=args_hash,
        read_only=read_only,
        created_at=T0,
        updated_at=T0,
    )


async def test_action_dedup_on_logical_key(repos):
    task, lease = await claimed(repos)
    first = await repos.actions.register(make_action(task, lease))
    assert first.created and first.action.read_only
    again = await repos.actions.register(make_action(task, lease))
    assert not again.created and again.action.id == first.action.id
    with pytest.raises(ActionConflict):
        await repos.actions.register(make_action(task, lease, args_hash="sha256:b"))


async def test_action_update_and_list(repos):
    task, lease = await claimed(repos)
    reg = await repos.actions.register(make_action(task, lease))
    done = await repos.actions.update(reg.action.id, ActionStatus.SUCCEEDED, result_ref="art_r", now=T0)
    assert done.status is ActionStatus.SUCCEEDED and done.result_ref == "art_r"
    assert (await repos.actions.get(task.id, "step-1:0")).status is ActionStatus.SUCCEEDED
    assert [a.id for a in await repos.actions.list_for_task(task.id)] == [reg.action.id]


async def test_action_with_budget_reservation(repos):
    task, lease = await claimed(repos, limits=Limits(max_tool_calls=1))
    ok = await repos.actions.register(make_action(task, lease), reserve=Usage(tool_calls=1))
    assert ok.created and ok.budget.granted
    refused = await repos.actions.register(make_action(task, lease, key="step-2:0"), reserve=Usage(tool_calls=1))
    assert refused.action is None and not refused.budget.granted
    assert refused.budget.exceeded == ["max_tool_calls"]
    assert await repos.actions.get(task.id, "step-2:0") is None


# -- usage -------------------------------------------------------------------------


async def test_reserve_settle_and_idempotency(repos):
    task, lease = await claimed(repos, limits=Limits(max_model_calls=2))
    first = await repos.usage.reserve(task.id, lease.attempt_id, "call_1", UsageKind.MODEL, Usage(model_calls=1), T0)
    assert first.granted
    repeat = await repos.usage.reserve(task.id, lease.attempt_id, "call_1", UsageKind.MODEL, Usage(model_calls=1), T0)
    assert repeat.granted and len(await repos.usage.open_reservations(task.id)) == 1

    actual = Usage(model_calls=1, input_tokens=120, output_tokens=30)
    settled = await repos.usage.settle("call_1", actual, T0)
    assert settled.status is ReservationStatus.SETTLED
    await repos.usage.settle("call_1", actual, T0)
    stored = await repos.tasks.get("s1", task.id)
    assert stored.usage == actual
    assert await repos.usage.open_reservations(task.id) == []


async def test_reservations_count_against_limits(repos):
    task, lease = await claimed(repos, limits=Limits(max_model_calls=2))
    for call in ("c1", "c2"):
        assert (await repos.usage.reserve(task.id, lease.attempt_id, call, UsageKind.MODEL, Usage(model_calls=1), T0)).granted
    refused = await repos.usage.reserve(task.id, lease.attempt_id, "c3", UsageKind.MODEL, Usage(model_calls=1), T0)
    assert not refused.granted and refused.exceeded == ["max_model_calls"]
    await repos.usage.release("c2", T0)
    assert (await repos.usage.reserve(task.id, lease.attempt_id, "c4", UsageKind.MODEL, Usage(model_calls=1), T0)).granted


async def test_parallel_reservations_do_not_overspend(repos):
    task, lease = await claimed(repos, limits=Limits(max_model_calls=3))
    results = await asyncio.gather(
        *(
            repos.usage.reserve(task.id, lease.attempt_id, f"c{i}", UsageKind.MODEL, Usage(model_calls=1), T0)
            for i in range(10)
        )
    )
    assert sum(r.granted for r in results) == 3


async def test_usage_survives_new_attempt(repos):
    task, lease = await claimed(repos, limits=Limits(max_model_calls=2))
    await repos.usage.reserve(task.id, lease.attempt_id, "c1", UsageKind.MODEL, Usage(model_calls=1), T0)
    await repos.usage.settle("c1", Usage(model_calls=1), T0)
    await repos.tasks.commit(lease, TaskOutcome(status=TaskStatus.QUEUED), T0)
    lease2 = await repos.tasks.claim("w2", [RunLane.FOREGROUND], TTL, T0)
    assert (await repos.usage.reserve(task.id, lease2.attempt_id, "c2", UsageKind.MODEL, Usage(model_calls=1), T0)).granted
    refused = await repos.usage.reserve(task.id, lease2.attempt_id, "c3", UsageKind.MODEL, Usage(model_calls=1), T0)
    assert not refused.granted


async def test_settled_cannot_be_released(repos):
    task, lease = await claimed(repos)
    await repos.usage.reserve(task.id, lease.attempt_id, "c1", UsageKind.MODEL, Usage(model_calls=1), T0)
    await repos.usage.settle("c1", Usage(model_calls=1), T0)
    with pytest.raises(Exception):
        await repos.usage.release("c1", T0)
    with pytest.raises(NotFound):
        await repos.usage.settle("missing", Usage(), T0)


# -- artifacts ---------------------------------------------------------------------


async def test_artifacts_are_scoped(repos):
    task = await created(repos)
    art = await repos.artifacts.add(
        Artifact(scope_id="s1", task_id=task.id, name="draft", uri="file:///a", media_type="text/markdown", content_hash="sha256:x", size_bytes=3)
    )
    assert (await repos.artifacts.get("s1", art.id)).name == "draft"
    with pytest.raises(NotFound):
        await repos.artifacts.get("s2", art.id)
    assert [a.id for a in await repos.artifacts.list_for_task("s1", task.id)] == [art.id]
    assert await repos.artifacts.list_for_task("s2", task.id) == []


# -- deadline and checkpoint ---------------------------------------------------------


async def test_first_claim_sets_deadline_and_retries_keep_it(repos):
    task, lease = await claimed(repos, limits=Limits(timeout_sec=600))
    assert lease.deadline == T0 + timedelta(seconds=600)
    assert (await repos.tasks.get("s1", task.id)).deadline == lease.deadline
    await repos.tasks.commit(lease, TaskOutcome(status=TaskStatus.QUEUED), T0)
    later = T0 + timedelta(seconds=120)
    lease2 = await repos.tasks.claim("w2", [RunLane.FOREGROUND], TTL, later)
    assert lease2.deadline == lease.deadline
    stored = await repos.tasks.get("s1", task.id)
    assert RemainingBudget.for_task(stored, later).seconds == 480


async def test_checkpoint_needs_current_lease(repos):
    task, lease = await claimed(repos)
    stored = await repos.tasks.checkpoint(lease, "art_ckpt", T0)
    assert stored.checkpoint_ref == "art_ckpt" and stored.version == 2
    await repos.tasks.commit(lease, TaskOutcome(status=TaskStatus.QUEUED), T0)
    assert (await repos.tasks.get("s1", task.id)).checkpoint_ref == "art_ckpt"
    with pytest.raises(StaleLease):
        await repos.tasks.checkpoint(lease, "art_late", T0)
