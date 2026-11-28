"""The P0 chain with real modules only (no fakes for tools, store or checks).

Main profile + document_draft@1 pack + ToolGateway + FileArtifactStore +
CapabilityVerifier + the Runner, driven by a scripted model:
read the material -> write the draft -> submit -> verified -> committed.

Also the counter-examples: a draft missing a required section fails
verification; a task asking for an outside action ends ``waiting`` instead
of ``succeeded``.
"""

import pytest

from harness.agents import MAIN_PROFILE, AgentRegistry, PackageInstructions, main_role_whitelist
from harness.capabilities import CapabilityRegistry
from harness.context.builder import SimpleContextBuilder
from harness.contracts import (
    Acceptance,
    ActionStatus,
    AgentRole,
    ErrorCode,
    EventType,
    TaskStatus,
    WaitCondition,
)
from harness.results import CapabilityVerifier, FileArtifactStore
from harness.runtime.models import ScriptedModelAdapter, result_step, tool_step
from harness.runtime.runner import AgentRunner
from harness.storage.memory import InMemoryRepositories
from harness.tools import PolicyToolCatalog, ToolGateway, UserGrant, WhitelistPolicy
from harness.tools.connectors import p0_connectors

from ..fakes import NoMemory, NoRecompute, ACTOR, Clock, claimed_task

pytestmark = pytest.mark.asyncio

ACCEPTANCE = Acceptance(
    required_artifacts=["draft"],
    checks=[{"id": "artifact_readable"}, {"id": "required_sections", "sections": ["目标", "方案", "计划"]}],
)
GOOD_DRAFT = "# 项目说明草稿\n\n## 目标\n交付 P0。\n\n## 方案\n新 harness。\n\n## 计划\n两周。\n"


class System:
    """Everything wired the way a Worker would wire it."""

    def __init__(self, tmp_path):
        self.material = tmp_path / "material"
        self.material.mkdir()
        (self.material / "brief.md").write_text("目标：交付 P0。方案：新 harness。计划：两周。", encoding="utf-8")

        self.repos = InMemoryRepositories()
        self.clock = Clock()
        self.store = FileArtifactStore(tmp_path / "artifacts", self.repos.artifacts)
        self.agents = AgentRegistry.builtin()
        self.packs = CapabilityRegistry.builtin()
        self.policy = WhitelistPolicy(
            roles=main_role_whitelist(),
            users={ACTOR.scope_id: {AgentRole.MAIN: UserGrant(paths=(str(self.material),), ref="grant:s1")}},
            capabilities=self.packs.tool_map(),
        )
        connectors = p0_connectors(self.store)
        self.gateway = ToolGateway(
            self.repos, self.store, self.policy, connectors, profiles=self.agents.tool_policies(), clock=self.clock
        )
        self.verifier = CapabilityVerifier(self.repos, self.store, self.packs, clock=self.clock)
        self.builder = SimpleContextBuilder(
            self.repos,
            instructions=PackageInstructions(),
            tools=PolicyToolCatalog(self.policy, self.gateway.specs),
            capabilities=self.packs,
            clock=self.clock,
            recomputer=NoRecompute(), memory=NoMemory(),
        )

    def draft_id(self):
        return self._draft

    async def run(self, draft_text, *, acceptance=ACCEPTANCE):
        task, execution = await claimed_task(self.repos, acceptance=acceptance, goal="根据材料写项目说明草稿，不发布")
        profile = self.agents.resolve_ref(task.agent_profile_ref)
        assert profile is MAIN_PROFILE

        def submit(ctx):
            # The write tool's result names the new artifact; take it from there.
            written = [m for m in ctx.messages if m.get("role") == "tool" and "wrote artifact" in m.get("content", "")]
            art_id = written[-1]["content"].split(" as ")[1].split(" ")[0]
            return result_step("草稿已写好。", {"draft": art_id})

        self.model = ScriptedModelAdapter(
            [
                tool_step(("files.read", {"path": str(self.material / "brief.md")})),
                tool_step(("artifacts.write", {"name": "draft", "content": draft_text})),
                submit,
            ]
        )
        runner = AgentRunner(self.repos, self.model, self.gateway, self.verifier, self.store, clock=self.clock)
        context = await self.builder.build(task, profile, execution.lease)
        self.context = context
        outcome = await runner.run(context, profile, execution)
        committed = await self.repos.tasks.commit(execution.lease, outcome, self.clock())
        return task, outcome, committed


async def test_full_chain_succeeds_and_is_committed(tmp_path):
    s = System(tmp_path)
    task, outcome, committed = await s.run(GOOD_DRAFT)

    assert outcome.status is TaskStatus.SUCCEEDED, outcome
    assert committed.status is TaskStatus.SUCCEEDED and committed.result_ref == outcome.result_ref
    # The result is the real file on disk, readable back through the store.
    content = await s.store.read(committed.result_ref, ACTOR)
    assert content.content.decode("utf-8") == GOOD_DRAFT
    # Verification is stored and passed for the same result.
    v = await s.repos.verifications.get(outcome.verification_ref)
    assert str(v.status) == "passed" and v.result_ref == committed.result_ref
    # The model saw the Main profile and the pack instructions, and exactly
    # the tools the pack, role and user allow.
    system = s.context.messages[0]["content"]
    assert "Main agent of DaraOS" in system and "Capability: document draft" in system
    assert {t.name for t in s.context.tool_specs} == {"files.read", "artifacts.read", "artifacts.write"}
    actions = await s.repos.actions.list_for_task(task.id)
    assert sorted(a.tool for a in actions) == ["artifacts.write", "files.read"]
    assert all(a.status is ActionStatus.SUCCEEDED for a in actions)
    kinds = [e.type for e in await s.repos.events.list("s1", task_id=task.id, limit=500)]
    assert EventType.VERIFICATION_RECORDED in kinds and kinds.count(EventType.TOOL_ACTION) == 2


async def test_missing_section_fails_the_task(tmp_path):
    s = System(tmp_path)
    task, outcome, committed = await s.run("## 目标\nx\n## 方案\ny\n")
    assert outcome.status is TaskStatus.FAILED
    assert outcome.error.code is ErrorCode.VERIFICATION_FAILED and "计划" in outcome.error.message
    assert committed.status is TaskStatus.FAILED
    # The draft is kept with the failed task (spec 4.3: 保存已有产物).
    work = await s.repos.artifacts.list_for_task("s1", task.id)
    assert [a.name for a in work] == ["draft"] and work[0].id in outcome.artifact_refs


async def test_unconfirmable_outside_action_waits_for_the_user(tmp_path):
    s = System(tmp_path)
    acceptance = ACCEPTANCE.model_copy(update={"required_actions": ["send_to_reviewer"]})
    task, outcome, committed = await s.run(GOOD_DRAFT, acceptance=acceptance)
    assert outcome.status is TaskStatus.WAITING and outcome.wait_condition is WaitCondition.INPUT
    assert committed.status is TaskStatus.WAITING and committed.result_ref is None
