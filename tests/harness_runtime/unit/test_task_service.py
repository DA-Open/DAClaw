"""TaskService over in-memory repositories (item 3)."""

import asyncio
import json

import pytest

from harness.config import HarnessConfig
from harness.contracts import (
    Acceptance,
    Actor,
    AgentRole,
    Checkpoint,
    EventType,
    IdempotencyConflict,
    Limits,
    LimitsExceeded,
    NotFound,
    RunLane,
    StaleLease,
    TaskOrigin,
    TaskOutcome,
    TaskRequest,
    TaskStatus,
    TriggerType,
)
from harness.storage.artifacts import InMemoryArtifactStore
from harness.storage.memory import InMemoryRepositories
from harness.tasks.service import TaskService

from ..fakes import Clock

pytestmark = pytest.mark.asyncio

ACTOR = Actor(actor_id="u1", scope_id="s1")
ORIGIN = TaskOrigin(
    agent_role=AgentRole.MAIN,
    agent_profile_ref="main@1",
    run_lane=RunLane.FOREGROUND,
    trigger_type=TriggerType.USER,
)


def make_service(config: HarnessConfig | None = None):
    repos = InMemoryRepositories()
    clock = Clock()
    store = InMemoryArtifactStore(repos)
    service = TaskService(repos, config or HarnessConfig(), artifacts=store, clock=clock)
    return service, repos, store, clock


def request(**overrides) -> TaskRequest:
    fields = {
        "request_key": "k1",
        "goal": "Draft the notes",
        "capability_ref": "document_draft@1",
    }
    fields.update(overrides)
    return TaskRequest(**fields)


async def events_of(repos, task_id, kind):
    return [e for e in await repos.events.list("s1", task_id=task_id, limit=1000) if e.type is kind]


# -- creation ----------------------------------------------------------------


async def test_create_uses_request_hash_limits_and_origin():
    service, repos, _, _ = make_service()
    req = request(
        limits=Limits(max_model_calls=5, max_tool_calls=7),
        acceptance=Acceptance(required_artifacts=["draft"]),
    )
    task = await service.create(req, ACTOR, ORIGIN)

    assert task.request_hash == req.request_hash()
    assert task.limits == req.limits
    assert task.acceptance == req.acceptance
    assert (task.agent_role, task.run_lane, task.trigger_type) == (
        AgentRole.MAIN,
        RunLane.FOREGROUND,
        TriggerType.USER,
    )
    assert (task.actor_id, task.scope_id) == ("u1", "s1")
    assert task.status is TaskStatus.QUEUED
    assert len(await events_of(repos, task.id, EventType.TASK_CREATED)) == 1


async def test_create_is_idempotent_and_rejects_changed_content():
    service, repos, _, _ = make_service()
    first = await service.create(request(), ACTOR, ORIGIN)
    second = await service.create(request(), ACTOR, ORIGIN)

    assert second.id == first.id
    assert len(await events_of(repos, first.id, EventType.TASK_CREATED)) == 1

    with pytest.raises(IdempotencyConflict):
        await service.create(request(goal="Something else"), ACTOR, ORIGIN)


async def test_create_rejects_limits_above_deployment_caps():
    service, _, _, _ = make_service()
    caps = HarnessConfig().caps
    too_big = Limits(max_model_calls=caps.max_model_calls + 1)
    with pytest.raises(LimitsExceeded):
        await service.create(request(limits=too_big), ACTOR, ORIGIN)


async def test_reads_are_scoped():
    service, _, _, _ = make_service()
    task = await service.create(request(), ACTOR, ORIGIN)

    with pytest.raises(NotFound):
        await service.get("s2", task.id)
    with pytest.raises(NotFound):
        await service.request_cancel("s2", task.id, "not mine")
    with pytest.raises(NotFound):
        await service.get("s1", "task_missing")


# -- claiming and leases -----------------------------------------------------


async def test_claim_has_exactly_one_winner():
    service, _, _, _ = make_service()
    await service.create(request(), ACTOR, ORIGIN)

    first, second = await asyncio.gather(
        service.claim("w1", [RunLane.FOREGROUND]),
        service.claim("w2", [RunLane.FOREGROUND]),
    )
    winners = [lease for lease in (first, second) if lease is not None]
    assert len(winners) == 1
    assert await service.claim("w3", [RunLane.FOREGROUND]) is None


async def test_claim_respects_lanes():
    service, _, _, _ = make_service()
    await service.create(request(), ACTOR, ORIGIN)

    assert await service.claim("w1", [RunLane.BACKGROUND]) is None
    assert await service.claim("w1", [RunLane.FOREGROUND]) is not None


async def test_heartbeat_returns_the_task_but_not_a_new_version():
    service, _, _, _ = make_service()
    task = await service.create(request(), ACTOR, ORIGIN)
    lease = await service.claim("w1", [RunLane.FOREGROUND])

    heartbeat = await service.heartbeat(lease)
    assert heartbeat.id == task.id
    assert heartbeat.status is TaskStatus.RUNNING
    assert heartbeat.version == task.version + 1  # only the claim changed it


async def test_old_or_expired_lease_cannot_heartbeat_or_commit():
    service, _, _, clock = make_service()
    await service.create(request(), ACTOR, ORIGIN)
    lease = await service.claim("w1", [RunLane.FOREGROUND])
    old = lease.model_copy(update={"lease_epoch": lease.lease_epoch - 1})

    with pytest.raises(StaleLease):
        await service.heartbeat(old)
    with pytest.raises(StaleLease):
        await service.commit(old, TaskOutcome(status=TaskStatus.CANCELLED))

    clock.advance(HarnessConfig().lease_ttl_sec + 1)
    with pytest.raises(StaleLease):
        await service.heartbeat(lease)
    with pytest.raises(StaleLease):
        await service.commit(lease, TaskOutcome(status=TaskStatus.CANCELLED))


# -- cancellation ------------------------------------------------------------


async def test_cancel_running_goes_through_cancelling():
    service, repos, _, _ = make_service()
    task = await service.create(request(), ACTOR, ORIGIN)
    lease = await service.claim("w1", [RunLane.FOREGROUND])

    requested = await service.request_cancel("s1", task.id, "user changed mind")
    assert requested.status is TaskStatus.CANCELLING
    assert requested.cancel_reason == "user changed mind"
    assert requested.cancel_requested_at is not None
    # The worker still holds its lease while it stops.
    assert (await service.heartbeat(lease)).status is TaskStatus.CANCELLING

    done = await service.commit(lease, TaskOutcome(status=TaskStatus.CANCELLED))
    assert done.status is TaskStatus.CANCELLED
    # Cancelling again changes nothing.
    assert (await service.request_cancel("s1", task.id, "again")).status is TaskStatus.CANCELLED

    changes = await events_of(repos, task.id, EventType.TASK_STATUS_CHANGED)
    assert [e.payload["to"] for e in changes] == ["running", "cancelling", "cancelled"]


async def test_cancel_queued_task_is_immediately_cancelled():
    service, _, _, _ = make_service()
    task = await service.create(request(), ACTOR, ORIGIN)

    cancelled = await service.request_cancel("s1", task.id, "never mind")
    assert cancelled.status is TaskStatus.CANCELLED
    assert await service.claim("w1", [RunLane.FOREGROUND]) is None


# -- checkpoints -------------------------------------------------------------


async def test_checkpoint_content_goes_to_the_artifact_store():
    service, repos, store, _ = make_service()
    task = await service.create(request(), ACTOR, ORIGIN)
    lease = await service.claim("w1", [RunLane.FOREGROUND])
    state = Checkpoint(
        stage="working",
        step_id="step-2",
        completed_action_ids=["act_1"],
        artifact_refs=["art_draft"],
    )

    await service.checkpoint(lease, state)

    stored = await service.get("s1", task.id)
    assert stored.checkpoint_ref
    content = await store.read(stored.checkpoint_ref, ACTOR)
    assert json.loads(content.content) == state.model_dump(mode="json")
    # Trajectory content is not one of the task's work artifacts.
    assert await repos.artifacts.list_for_task("s1", task.id) == []
