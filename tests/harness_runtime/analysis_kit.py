"""Shared harness for the analysis-baseline tests: real gateway, runner, hooks."""

from harness.agents import AgentRegistry, PackageInstructions
from harness.capabilities import CapabilityRegistry
from harness.config import HarnessConfig
from harness.context.builder import SimpleContextBuilder
from harness.contracts import (
    Acceptance,
    AgentRole,
    ExecutionContext,
    Limits,
    RunLane,
    Task,
    TaskOrigin,
    TaskRequest,
    TriggerType,
)
from harness.personal.baseline import BaselineWriter
from harness.personal.memory import MemoryService
from harness.personal.recompute import Recomputer
from harness.results import CapabilityVerifier, FileArtifactStore
from harness.runtime.models import ScriptedModelAdapter, result_step, tool_step
from harness.runtime.runner import AgentRunner
from harness.tasks.service import TaskService
from harness.tools import PolicyToolCatalog, ToolGateway, UserGrant, WhitelistPolicy
from harness.tools.connectors import data_connectors, p0_connectors

from .fakes import ACTOR, T0, TTL, Clock

GOAL = "3 月已完成订单的销售额和订单数"
REVENUE_SQL = """
WITH dedup AS (
  SELECT * FROM orders oi
  WHERE order_item_id = (SELECT min(order_item_id) FROM orders x
                         WHERE x.order_id = oi.order_id AND x.product_id = oi.product_id)
)
SELECT round(sum(quantity * unit_price), 2) AS revenue
FROM dedup JOIN orders_orders o USING (order_id)
WHERE o.status='completed' AND o.order_date LIKE '2025-03%'
"""
COUNT_SQL = "SELECT count(*) AS orders FROM orders_orders WHERE status='completed' AND order_date LIKE '2025-03%'"
REPORT = """# 三月回顾

## Question
3 月已完成订单的销售额和订单数？

## Answer
三月已完成订单的销售额为 219912.81，共 169 单。

## Evidence
- query_ref: {ref}
"""

DEFAULT_TOOLS = {"files.read", "artifacts.read", "artifacts.write", "data.list", "data.connect",
                 "data.query", "memory.read", "memory.search"}


class Kit:
    def __init__(self, tmp_path, repos, *, tools=None):
        self.repos = repos
        self.clock = Clock()
        self.store = FileArtifactStore(tmp_path / "artifacts", repos.artifacts)
        self.memory = MemoryService(repos.memories, self.store, self.clock)
        self.agents = AgentRegistry.builtin()
        self.caps = CapabilityRegistry.builtin()
        allowed = set(tools or DEFAULT_TOOLS)
        grants = UserGrant(tools=allowed, paths=(str(tmp_path),))
        self.policy = WhitelistPolicy(
            roles={"main_tools@1": allowed, "proactive_tools@1": allowed},
            users={ACTOR.scope_id: {AgentRole.MAIN: grants, AgentRole.PROACTIVE: grants}},
        )
        self.connectors = [*p0_connectors(self.store), *data_connectors(self.store)]
        self.gateway = ToolGateway(
            repos, self.store, self.policy, self.connectors,
            profiles={"main@1": "main_tools@1", "proactive@1": "proactive_tools@1"}, clock=self.clock,
        )
        self.verifier = CapabilityVerifier(repos, self.store, self.caps, clock=self.clock)
        self.recomputer = Recomputer(repos, self.memory, self.store, self.policy, clock=self.clock)
        self.baseline = BaselineWriter(repos, self.memory, self.store, clock=self.clock)
        self.builder = SimpleContextBuilder(
            repos, instructions=PackageInstructions(),
            tools=PolicyToolCatalog(self.policy, self.gateway.specs),
            capabilities=self.caps, clock=self.clock, recomputer=self.recomputer, memory=self.memory,
        )
        self.config = HarnessConfig()

    def close_sessions(self) -> None:
        """DuckDB keeps a file attached; release it before the file is rewritten."""
        for connector in self.connectors:
            if hasattr(connector, "close"):
                connector.close()

    def service(self, hooks=None):
        return TaskService(self.repos.tasks, self.config, artifacts=self.store, capabilities=self.caps,
                           agents=self.agents, clock=self.clock,
                           after_commit=[self.baseline] if hooks is None else hooks)

    async def run_analysis(self, dataset, *, service=None, model_script=None):
        service = service or self.service()
        task = (await self.repos.tasks.create(Task.new(
            TaskRequest(request_key="k1", goal=GOAL, capability_ref="data_analysis@1",
                        acceptance=Acceptance(required_artifacts=["analysis"], checks=[
                            {"id": "artifact_readable"},
                            {"id": "required_sections", "sections": ["Question", "Answer", "Evidence"]},
                            {"id": "query_evidence"}]), limits=Limits()),
            ACTOR,
            TaskOrigin(agent_role=AgentRole.MAIN, agent_profile_ref="main@1",
                       run_lane=RunLane.FOREGROUND, trigger_type=TriggerType.USER),
            now=T0,
        ))).task
        lease = await self.repos.tasks.claim("w1", [RunLane.FOREGROUND], TTL, self.clock())
        model = ScriptedModelAdapter(model_script or self.script(dataset))
        runner = AgentRunner(self.repos, model, self.gateway, self.verifier, self.store, clock=self.clock)
        profile = self.agents.resolve_ref(task.agent_profile_ref)
        context = await self.builder.build(task, profile, lease)
        outcome = await runner.run(context, profile, ExecutionContext(
            lease=lease, actor=ACTOR, agent_role=AgentRole.MAIN, run_lane=RunLane.FOREGROUND))
        committed = await service.commit(lease, outcome)
        return task, outcome, committed

    def script(self, dataset):
        def write(ctx):
            refs = _refs(ctx)
            return tool_step(("artifacts.write", {"name": "analysis", "content": REPORT.format(ref=refs[0])}))

        def submit(ctx):
            written = [m.get("content", "") for m in ctx.messages
                       if m.get("role") == "tool" and "wrote artifact" in m.get("content", "")]
            analysis = written[-1].split(" as ")[1].split(" ")[0]
            return result_step("三月回顾已完成", {"analysis": analysis}, _refs(ctx))

        return [
            tool_step(("data.connect", {"path": str(dataset)})),
            tool_step(("data.query", {"sql": REVENUE_SQL})),
            tool_step(("data.query", {"sql": COUNT_SQL})),
            write,
            submit,
        ]


def _refs(ctx):
    return [line.split(": ", 1)[1].strip()
            for m in ctx.messages if m.get("role") == "tool"
            for line in m.get("content", "").splitlines() if line.startswith("query_ref: ")]
