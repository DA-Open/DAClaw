"""The data-analysis chain with real modules: connect -> query -> report -> verify.

Uses the data_analysis@1 pack, the real connectors (files, artifacts, data),
ToolGateway, FileArtifactStore, CapabilityVerifier (including query_evidence) and
the Runner, driven by a scripted model. The report is built from the tool results
the Runner sent, because the query reference only exists at run time.

Assertions that matter: a report citing the stored query is verified and
committed, a report without the Evidence section fails, and a result citing no
query fails.
"""

import pytest

from harness.agents import AgentRegistry, PackageInstructions, main_role_whitelist
from harness.capabilities import CapabilityRegistry
from harness.context.builder import SimpleContextBuilder
from harness.contracts import (
    Acceptance,
    AgentRole,
    ErrorCode,
    EventType,
    RunLane,
    Task,
    TaskOrigin,
    TaskRequest,
    TaskStatus,
    TriggerType,
)
from harness.results import CapabilityVerifier, FileArtifactStore
from harness.runtime.models import ScriptedModelAdapter, result_step, tool_step
from harness.runtime.runner import AgentRunner
from harness.storage.memory import InMemoryRepositories
from harness.tools import PolicyToolCatalog, ToolGateway, UserGrant, WhitelistPolicy
from harness.tools.connectors import data_connectors, p0_connectors

from ..fakes import ACTOR, T0, TTL, Clock

pytestmark = pytest.mark.asyncio

QUESTION = "Which region sold the most units?"
CSV = "region,units,price\nnorth,3,10.0\nsouth,5,12.5\nnorth,2,10.0\n"
SQL = "select region, sum(units) as units from sales group by 1 order by units desc"

REPORT = """# Sales by region

## Question
Which region sold the most units?

## Answer
south sold 5 units; north sold 5 units as well (3 + 2), so the honest answer names
both and says the question needs a tie-break.

## Evidence
- query_ref: {ref}
  sql: {sql}
"""

NO_EVIDENCE_SECTION = """# Sales by region

## Question
Which region sold the most units?

## Answer
south, with 5 units.
"""

ACCEPTANCE = Acceptance(
    required_artifacts=["analysis"],
    checks=[
        {"id": "artifact_readable"},
        {"id": "required_sections", "sections": ["Question", "Answer", "Evidence"]},
        {"id": "query_evidence"},
    ],
)


def tool_texts(ctx):
    return [m.get("content", "") for m in ctx.messages if m.get("role") == "tool"]


def query_refs(ctx):
    refs = []
    for text in tool_texts(ctx):
        for line in text.splitlines():
            if line.startswith("query_ref: "):
                refs.append(line.split(": ", 1)[1].strip())
    return refs


def written_artifact(ctx):
    texts = [t for t in tool_texts(ctx) if "wrote artifact" in t]
    return texts[-1].split(" as ")[1].split(" ")[0]


class System:
    def __init__(self, tmp_path):
        self.material = tmp_path / "data"
        self.material.mkdir()
        self.csv = self.material / "sales.csv"
        self.csv.write_text(CSV, encoding="utf-8")

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
        connectors = [*p0_connectors(self.store), *data_connectors(self.store)]
        self.gateway = ToolGateway(
            self.repos,
            self.store,
            self.policy,
            connectors,
            profiles=self.agents.tool_policies(),
            clock=self.clock,
        )
        self.verifier = CapabilityVerifier(self.repos, self.store, self.packs, clock=self.clock)
        self.builder = SimpleContextBuilder(
            self.repos,
            instructions=PackageInstructions(),
            tools=PolicyToolCatalog(self.policy, self.gateway.specs),
            capabilities=self.packs,
            clock=self.clock,
        )

    async def claim(self):
        """A claimed task on the data pack (fakes.claimed_task fixes the draft pack)."""
        request = TaskRequest(
            request_key="k1",
            goal=QUESTION,
            capability_ref="data_analysis@1",
            acceptance=ACCEPTANCE,
        )
        origin = TaskOrigin(
            agent_role=AgentRole.MAIN,
            agent_profile_ref="main@1",
            run_lane=RunLane.FOREGROUND,
            trigger_type=TriggerType.USER,
        )
        created = await self.repos.tasks.create(Task.new(request, ACTOR, origin, now=T0))
        lease = await self.repos.tasks.claim("w1", [RunLane.FOREGROUND], TTL, T0)
        assert lease is not None
        task = await self.repos.tasks.get(ACTOR.scope_id, created.task.id)
        from harness.contracts import ExecutionContext

        execution = ExecutionContext(
            lease=lease, actor=ACTOR, agent_role=AgentRole.MAIN, run_lane=RunLane.FOREGROUND
        )
        return task, execution

    async def run(self, *, report=REPORT, cite_evidence=True):
        task, execution = await self.claim()
        profile = self.agents.resolve_ref(task.agent_profile_ref)

        def write(ctx):
            refs = query_refs(ctx)
            text = report.format(ref=refs[-1] if refs else "", sql=SQL) if "{" in report else report
            return tool_step(("artifacts.write", {"name": "analysis", "content": text}))

        def submit(ctx):
            refs = query_refs(ctx)
            evidence = refs[-1:] if (cite_evidence and refs) else []
            return result_step("Analyzed sales by region.", {"analysis": written_artifact(ctx)}, evidence)

        self.model = ScriptedModelAdapter(
            [
                tool_step(("data.connect", {"path": str(self.csv)})),
                tool_step(("data.query", {"sql": SQL})),
                write,
                submit,
            ]
        )
        runner = AgentRunner(self.repos, self.model, self.gateway, self.verifier, self.store, clock=self.clock)
        context = await self.builder.build(task, profile, execution.lease)
        self.context = context
        outcome = await runner.run(context, profile, execution)
        committed = await self.repos.tasks.commit(execution.lease, outcome, self.clock())
        return task, outcome, committed


async def test_connect_query_report_and_verify(tmp_path):
    s = System(tmp_path)
    task, outcome, committed = await s.run()

    assert outcome.status is TaskStatus.SUCCEEDED, outcome
    assert committed.status is TaskStatus.SUCCEEDED and committed.result_ref == outcome.result_ref
    content = (await s.store.read(committed.result_ref, ACTOR)).content.decode()
    assert "## Evidence" in content and "query_ref: " in content
    # Exactly the pack's five tools were offered, nothing else.
    assert {t.name for t in s.context.tool_specs} == {
        "files.read",
        "artifacts.read",
        "artifacts.write",
        "data.list",
        "data.connect",
        "data.query",
    }
    actions = await s.repos.actions.list_for_task(task.id)
    assert sorted(a.tool for a in actions) == ["artifacts.write", "data.connect", "data.query"]
    kinds = [e.type for e in await s.repos.events.list("s1", task_id=task.id, limit=500)]
    assert EventType.VERIFICATION_RECORDED in kinds
    # The data artifacts stay in the task's area: two query files plus the report.
    names = sorted(a.name for a in await s.repos.artifacts.list_for_task("s1", task.id))
    assert names.count("analysis") == 1 and sum(n.startswith("query/") for n in names) == 2


async def test_result_without_evidence_fails(tmp_path):
    s = System(tmp_path)
    _task, outcome, _committed = await s.run(cite_evidence=False)
    assert outcome.status is TaskStatus.FAILED
    assert outcome.error.code is ErrorCode.VERIFICATION_FAILED


async def test_report_without_evidence_section_fails(tmp_path):
    s = System(tmp_path)
    _task, outcome, _committed = await s.run(report=NO_EVIDENCE_SECTION)
    assert outcome.status is TaskStatus.FAILED
    assert outcome.error.code is ErrorCode.VERIFICATION_FAILED
    assert "Evidence" in outcome.error.message
