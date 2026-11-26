"""The Runner with the real ToolGateway, policy and connectors (no fakes for tools).

Scripted model: read a file -> write the draft -> submit. Also the P0
acceptance counter-example "越权被拒绝": a read outside the user's
directories is denied and the model is told so.
"""

import pytest

from harness.contracts import AgentRole, EventType, TaskStatus
from harness.context.builder import SimpleContextBuilder, StaticInstructions
from harness.runtime.models import ScriptedModelAdapter, result_step, tool_step
from harness.runtime.runner import AgentRunner
from harness.storage.memory import InMemoryRepositories
from harness.tools import PolicyToolCatalog, ToolGateway, UserGrant, WhitelistPolicy
from harness.tools.connectors import p0_connectors

from ..fakes import NoRecompute, ACTOR, MAIN_PROFILE, Clock, FakeVerifier, MemoryArtifactStore, claimed_task

pytestmark = pytest.mark.asyncio


class World:
    """A task, the real gateway and connectors, and a scripted model."""

    def __init__(self, tmp_path):
        self.allowed = tmp_path / "allowed"
        self.allowed.mkdir()
        (self.allowed / "notes.md").write_text("# Notes\n- goal: ship P0\n", encoding="utf-8")
        self.secret = tmp_path / "secret.env"
        self.secret.write_text("API_KEY=do-not-read", encoding="utf-8")

        self.repos = InMemoryRepositories()
        self.clock = Clock()
        self.store = MemoryArtifactStore(self.repos)
        self.policy = WhitelistPolicy(
            roles={MAIN_PROFILE.tool_policy_ref: {"files.read", "artifacts.read", "artifacts.write"}},
            users={ACTOR.scope_id: {AgentRole.MAIN: UserGrant(paths=(str(self.allowed),), ref="grant:s1")}},
        )
        self.connectors = p0_connectors(self.store)
        self.gateway = ToolGateway(
            self.repos,
            self.store,
            self.policy,
            self.connectors,
            profiles={"main@1": MAIN_PROFILE.tool_policy_ref},
            clock=self.clock,
        )

    def draft_ref(self) -> str:
        return next(a for a, data in self.store.content.items() if data.startswith(b"# Draft"))

    async def run(self, script):
        self.model = ScriptedModelAdapter(script)
        runner = AgentRunner(self.repos, self.model, self.gateway, FakeVerifier(self.repos), self.store, clock=self.clock)
        self.task, self.execution = await claimed_task(self.repos)
        builder = SimpleContextBuilder(
            self.repos,
            instructions=StaticInstructions({"main@1": "You draft documents."}),
            tools=PolicyToolCatalog(self.policy, [c.spec for c in self.connectors]),
            clock=self.clock,
        
            recomputer=NoRecompute(),
)
        context = await builder.build(self.task, MAIN_PROFILE, self.execution.lease)
        self.outcome = await runner.run(context, MAIN_PROFILE, self.execution)
        return self.outcome


async def test_read_write_submit_succeeds(tmp_path):
    w = World(tmp_path)
    outcome = await w.run(
        [
            tool_step(("files.read", {"path": str(w.allowed / "notes.md")})),
            tool_step(("artifacts.write", {"name": "draft", "content": "# Draft\nShip P0."})),
            lambda ctx: result_step("Draft written.", {"draft": w.draft_ref()}),
        ]
    )
    assert outcome.status is TaskStatus.SUCCEEDED
    committed = await w.repos.tasks.commit(w.execution.lease, outcome, w.clock())
    assert committed.status is TaskStatus.SUCCEEDED
    # The model was shown exactly the granted tools (plus submit_result).
    offered = {t.name for t in w.model.calls[0].tool_specs}
    assert offered == {"files.read", "artifacts.read", "artifacts.write", "submit_result"}
    actions = {a.tool: a for a in await w.repos.actions.list_for_task(w.task.id)}
    assert set(actions) == {"files.read", "artifacts.write"}
    assert actions["files.read"].read_only and not actions["artifacts.write"].read_only
    assert actions["files.read"].logical_key == "step-1.0"
    assert actions["artifacts.write"].logical_key == "step-2.0"
    assert all(str(a.status) == "succeeded" for a in actions.values())
    usage = (await w.repos.tasks.get("s1", w.task.id)).usage
    assert usage.tool_calls == 2 and usage.model_calls == 3
    events = await w.repos.events.list("s1", task_id=w.task.id, limit=500)
    assert sum(e.type is EventType.TOOL_ACTION for e in events) == 2


async def test_read_outside_the_whitelist_is_denied_and_reported(tmp_path):
    w = World(tmp_path)
    seen = {}

    def after_denial(ctx):
        seen["last"] = ctx.messages[-1]
        return result_step("Could not read the file.", {})

    outcome = await w.run([tool_step(("files.read", {"path": str(w.secret)})), after_denial])
    last = seen["last"]
    assert last["role"] == "tool" and last.get("is_error") is True
    assert "denied" in last["content"] and "outside" in last["content"]
    assert "do-not-read" not in str([c.model_dump() for c in w.model.calls])
    assert await w.repos.actions.list_for_task(w.task.id) == []
    # No draft was written, so the verifier fails the result: the task fails
    # honestly instead of pretending success.
    assert outcome.status is TaskStatus.FAILED
