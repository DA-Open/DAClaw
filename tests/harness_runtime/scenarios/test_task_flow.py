"""End-to-end worker scenario over in-memory storage (item 3 + 5 + 7).

A real TaskService, a real gateway with the file connectors and a scripted
model: claim -> context -> run -> commit, plus the two error paths the spec
cares about (stale lease must not commit; a cancel request ends the run).
"""

import pytest

from harness.config import HarnessConfig
from harness.context.builder import SimpleContextBuilder, StaticInstructions
from harness.contracts import (
    Acceptance,
    Actor,
    AgentRole,
    RunLane,
    StaleLease,
    TaskOrigin,
    TaskRequest,
    TaskStatus,
    TriggerType,
    VerificationStatus,
)
from harness.runtime.models import ScriptedModelAdapter, result_step, tool_step
from harness.runtime.runner import AgentRunner
from harness.storage.artifacts import InMemoryArtifactStore
from harness.storage.memory import InMemoryRepositories
from harness.tasks.service import TaskService
from harness.tasks.worker import StaticAgentRegistry, Worker
from harness.tools.catalog import PolicyToolCatalog
from harness.tools.connectors.files import ReadFileConnector, WriteFileConnector
from harness.tools.gateway import ToolGateway
from harness.tools.policy import ToolPolicy

from ..fakes import ACTOR, FakeVerifier

pytestmark = pytest.mark.asyncio

ORIGIN = TaskOrigin(
    agent_role=AgentRole.MAIN,
    agent_profile_ref="main@1",
    run_lane=RunLane.FOREGROUND,
    trigger_type=TriggerType.USER,
)


class RecordingGateway(ToolGateway):
    """Remembers the artifacts tools produced, so the script can submit one."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.produced: list[str] = []

    async def invoke(self, execution, call):
        outcome = await super().invoke(execution, call)
        self.produced.extend(outcome.artifact_refs)
        return outcome


class StaleRunner:
    """A Runner that loses the lease instead of finishing."""

    async def run(self, context, profile, execution, *, lease_lost=None):
        raise StaleLease("another worker owns this task now")


class Env:
    def __init__(self, root):
        self.repos = InMemoryRepositories()
        self.store = InMemoryArtifactStore(self.repos)
        self.service = TaskService(self.repos, HarnessConfig(), artifacts=self.store)
        policy = ToolPolicy(
            role_tools={AgentRole.MAIN.value: {"files.read", "files.write"}}
        )
        connectors = {
            "files.read": ReadFileConnector(root, artifacts=self.store),
            "files.write": WriteFileConnector(root, self.store),
        }
        self.gateway = RecordingGateway(self.repos, policy, connectors, self.store)
        self.builder = SimpleContextBuilder(
            self.repos,
            instructions=StaticInstructions({"main@1": "You are Main."}),
            tools=PolicyToolCatalog.from_connectors(connectors, policy),
        )

    def runner(self, model, verifier=None):
        return AgentRunner(
            self.repos, model, self.gateway, verifier or FakeVerifier(self.repos), self.store
        )

    def worker(self, runner) -> Worker:
        return Worker(
            self.service,
            self.builder,
            runner,
            StaticAgentRegistry(),
            worker_id="w1",
            lanes=[RunLane.FOREGROUND],
            heartbeat_interval_sec=0.01,
        )

    async def create_task(self, key="k1"):
        return await self.service.create(
            TaskRequest(
                request_key=key,
                goal="Draft the notes",
                capability_ref="document_draft@1",
                acceptance=Acceptance(required_artifacts=["draft"]),
            ),
            ACTOR,
            ORIGIN,
        )


async def test_worker_runs_a_task_end_to_end(tmp_path):
    (tmp_path / "notes.md").write_text("- one\n- two\n")
    env = Env(tmp_path)
    task = await env.create_task()
    model = ScriptedModelAdapter(
        [
            tool_step(("files.read", {"path": "notes.md"})),
            tool_step(
                (
                    "files.write",
                    {"path": "draft.md", "content": "# Draft\n- one\n- two\n", "name": "draft"},
                )
            ),
            lambda context: result_step("Draft written.", {"draft": env.gateway.produced[-1]}),
        ]
    )
    worker = env.worker(env.runner(model))

    result = await worker.run_once()

    assert result is not None
    assert result.id == task.id
    assert result.status is TaskStatus.SUCCEEDED
    assert result.result_ref == env.gateway.produced[-1]
    assert (tmp_path / "draft.md").read_text() == "# Draft\n- one\n- two\n"

    stored = await env.service.get("s1", task.id)
    assert stored.status is TaskStatus.SUCCEEDED
    assert stored.usage.model_calls == 3
    assert stored.usage.tool_calls == 2
    assert [v.status for v in await env.repos.verifications.list_for_task(task.id)] == [
        VerificationStatus.PASSED
    ]

    # The queue is empty now.
    assert await worker.run_once() is None


async def test_stale_lease_is_never_committed(tmp_path):
    env = Env(tmp_path)
    task = await env.create_task()
    worker = env.worker(StaleRunner())

    assert await worker.run_once() is None

    stored = await env.service.get("s1", task.id)
    assert stored.status is TaskStatus.RUNNING
    assert stored.result_ref is None
    assert await env.repos.verifications.list_for_task(task.id) == []


async def test_cancel_request_stops_the_run_and_commits_cancelled(tmp_path):
    env = Env(tmp_path)
    task = await env.create_task()
    model = ScriptedModelAdapter(
        [
            tool_step(("files.write", {"path": "draft.md", "content": "x", "name": "draft"})),
            result_step("should never be reached", {"draft": "art_none"}),
        ]
    )
    original = env.gateway.invoke

    async def cancel_after_first_call(execution, call):
        outcome = await original(execution, call)
        await env.service.request_cancel("s1", execution.lease.task_id, "stop")
        return outcome

    env.gateway.invoke = cancel_after_first_call
    worker = env.worker(env.runner(model))

    result = await worker.run_once()

    assert result is not None
    assert result.status is TaskStatus.CANCELLED
    assert len(model.calls) == 1  # the model was not asked for another round
    stored = await env.service.get("s1", task.id)
    assert stored.status is TaskStatus.CANCELLED
    assert stored.cancel_reason == "stop"
