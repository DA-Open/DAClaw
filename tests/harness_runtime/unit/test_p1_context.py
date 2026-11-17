import json
from unittest.mock import AsyncMock

import pytest

from harness.app import build_system
from harness.config import HarnessConfig
from harness.context.resume import CheckpointUnreadable, ResumingContextBuilder
from harness.contracts import ArtifactInput, Checkpoint, ErrorCode, TaskOutcome, TaskStatus, utcnow
from harness.runtime import messages as msg
from harness.runtime.models import ScriptedModelAdapter
from harness.storage.memory import InMemoryRepositories

from ..fakes import MAIN_PROFILE, MemoryArtifactStore, claimed_task
from ..scenarios.test_worker import submit

pytestmark = pytest.mark.asyncio


async def test_resume_keeps_current_system_and_permissions():
    repos = InMemoryRepositories()
    task, execution = await claimed_task(repos)
    system = build_system(HarnessConfig(), ScriptedModelAdapter([]), repos=repos)
    profile = system.agents.resolve_ref(task.agent_profile_ref)
    initial = await system.builder.build(task, profile, execution.lease)
    inner = AsyncMock()
    inner.build.return_value = initial
    artifacts = MemoryArtifactStore(repos)
    checkpoint = Checkpoint(stage="tools", step_id="step-1")
    ref = await artifacts.put(ArtifactInput(
        scope_id=task.scope_id, task_id=task.id, media_type="application/json",
        content=json.dumps({"checkpoint": checkpoint.model_dump(mode="json"),
                            "messages": [msg.system("old system"), msg.user("saved history")]}).encode(),
    ))
    task.checkpoint_ref = ref.id
    bundle = await ResumingContextBuilder(inner, artifacts).build(task, profile, execution.lease)
    assert bundle.messages == [initial.messages[0], msg.user("saved history")]
    assert bundle.checkpoint == checkpoint
    assert bundle.tool_specs == initial.tool_specs
    assert bundle.remaining_budget == initial.remaining_budget
    inner.build.assert_awaited_once_with(task, profile, execution.lease)


async def test_resume_without_checkpoint_uses_inner_bundle():
    repos = InMemoryRepositories()
    task, execution = await claimed_task(repos)
    inner = AsyncMock()
    artifacts = AsyncMock()
    result = await ResumingContextBuilder(inner, artifacts).build(task, MAIN_PROFILE, execution.lease)
    assert result is inner.build.return_value
    artifacts.read.assert_not_awaited()


@pytest.mark.parametrize("content", [None, b"{", b'{}', b'{"checkpoint": {}, "messages": []}'])
async def test_unreadable_checkpoint_fails_task(tmp_path, content):
    system = build_system(HarnessConfig(artifact_root=tmp_path), ScriptedModelAdapter([]))
    task = await submit(system)
    if content is None:
        reference = "art_missing_checkpoint"
    else:
        ref = await system.artifacts.put(ArtifactInput(
            scope_id=task.scope_id, task_id=task.id, media_type="application/json", content=content,
        ))
        reference = ref.id
    # Persist a checkpoint on a running attempt, then return it to the queue.
    lease = await system.tasks.claim("first", [task.run_lane])
    await system.repos.tasks.checkpoint(lease, reference, utcnow())
    await system.repos.tasks.commit(lease, TaskOutcome(status=TaskStatus.QUEUED), utcnow())
    connector = system.gateway._connectors["files.read"]
    connector.execute = AsyncMock(wraps=connector.execute)
    task = await system.tasks.get(task.scope_id, task.id)
    with pytest.raises(CheckpointUnreadable, match=reference):
        await system.builder.build(task, system.agents.resolve_ref(task.agent_profile_ref), lease)
    report = await system.worker().run_once()
    stored = await system.tasks.get(task.scope_id, task.id)
    assert report.committed and stored.status is TaskStatus.FAILED
    assert stored.error.code is ErrorCode.INTERNAL
    assert reference in stored.error.message
    connector.execute.assert_not_awaited()


async def test_max_attempts_config():
    assert HarnessConfig.from_env({}).max_attempts == 3
    config = HarnessConfig.from_env({"DARAOS_HARNESS_MAX_ATTEMPTS": "5"})
    assert config.max_attempts == 5
    assert config.safe_dict()["max_attempts"] == 5
    for value in ("0", "-1", "invalid"):
        with pytest.raises(ValueError):
            HarnessConfig.from_env({"DARAOS_HARNESS_MAX_ATTEMPTS": value})
