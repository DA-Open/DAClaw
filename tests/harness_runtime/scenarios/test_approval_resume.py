"""The approval round trip through the real Runner, not a hand-built wait state.

First attempt: the model asks to send a mail -> action parked -> task waiting
for approval with a checkpoint. A human approves. The task is claimed again, its
context is rebuilt from the checkpoint, and the same step runs once.
"""

import pytest

from harness.agents import AgentRegistry, PackageInstructions, main_role_whitelist
from harness.capabilities import CapabilityRegistry
from harness.context.builder import SimpleContextBuilder
from harness.context.resume import ResumingContextBuilder
from harness.contracts import (
    Acceptance,
    ActionStatus,
    AgentRole,
    ApprovalDecision,
    RunLane,
    Task,
    TaskOrigin,
    TaskRequest,
    TaskStatus,
    ToolResult,
    ToolSpec,
    ToolStatus,
    TriggerType,
    WaitCondition,
)
from harness.results import CapabilityVerifier, FileArtifactStore
from harness.runtime.models import ScriptedModelAdapter, result_step, tool_step
from harness.runtime.runner import AgentRunner
from harness.storage.memory import InMemoryRepositories
from harness.tasks.approvals import Approvals
from harness.tools import PolicyToolCatalog, ToolGateway, UserGrant, WhitelistPolicy
from harness.tools.connectors import p0_connectors

from ..fakes import ACTOR, T0, TTL, Clock

pytestmark = pytest.mark.asyncio

ACCEPTANCE = Acceptance(required_artifacts=["draft"], checks=[{"id": "artifact_readable"}])


class Mail:
    spec = ToolSpec(
        name="mail.send",
        description="send a mail",
        input_schema={"type": "object", "required": ["to"], "additionalProperties": False,
                      "properties": {"to": {"type": "string", "minLength": 1}}},
        read_only=False,
        side_effects=True,
        idempotent=False,
    )

    def __init__(self):
        self.sent = 0

    async def execute(self, call):
        self.sent += 1
        return ToolResult(status=ToolStatus.SUCCEEDED, summary="sent", receipt_ref="rcpt")

    async def reconcile(self, action):
        raise NotImplementedError


async def test_approved_step_runs_once_after_resume(tmp_path):
    mail = Mail()
    repos = InMemoryRepositories()
    clock = Clock()
    store = FileArtifactStore(tmp_path / "artifacts", repos.artifacts)
    agents = AgentRegistry.builtin()
    caps = CapabilityRegistry.builtin()
    tools = {"mail.send", "artifacts.read", "artifacts.write", "files.read"}
    policy = WhitelistPolicy(
        roles={"main_tools@1": tools},
        users={ACTOR.scope_id: {AgentRole.MAIN: UserGrant(tools=tools, paths=())}},
    )
    gateway = ToolGateway(repos, store, policy, [*p0_connectors(store), mail],
                          profiles=agents.tool_policies(), clock=clock)
    verifier = CapabilityVerifier(repos, store, caps, clock=clock)
    builder = SimpleContextBuilder(repos, instructions=PackageInstructions(),
                                   tools=PolicyToolCatalog(policy, gateway.specs), capabilities=caps, clock=clock)
    def submit(ctx):
        written = [
            m.get("content", "")
            for m in ctx.messages
            if m.get("role") == "tool" and "wrote artifact" in m.get("content", "")
        ]
        draft = written[-1].split(" as ")[1].split(" ")[0]
        return result_step("sent", {"draft": draft})


    model = ScriptedModelAdapter([
        tool_step(("mail.send", {"to": "ops@example.com"})),
        tool_step(("artifacts.write", {"name": "draft", "content": "# done\n"})),
        submit,
    ])
    runner = AgentRunner(repos, model, gateway, verifier, store, clock=clock)

    request = TaskRequest(request_key="k1", goal="mail ops", capability_ref="document_draft@1",
                          acceptance=ACCEPTANCE, limits=__import__("harness.contracts", fromlist=["Limits"]).Limits())
    origin = TaskOrigin(agent_role=AgentRole.MAIN, agent_profile_ref="main@1",
                        run_lane=RunLane.FOREGROUND, trigger_type=TriggerType.USER)
    created = await repos.tasks.create(Task.new(request, ACTOR, origin, now=T0))
    lease = await repos.tasks.claim("w1", [RunLane.FOREGROUND], TTL, T0)
    task = await repos.tasks.get(ACTOR.scope_id, created.task.id)
    from harness.contracts import ExecutionContext

    execution = ExecutionContext(lease=lease, actor=ACTOR, agent_role=AgentRole.MAIN, run_lane=RunLane.FOREGROUND)
    profile = agents.resolve_ref(task.agent_profile_ref)
    context = await builder.build(task, profile, lease)
    outcome = await runner.run(context, profile, execution)
    assert outcome.status is TaskStatus.WAITING and outcome.wait_condition is WaitCondition.APPROVAL
    await repos.tasks.commit(lease, outcome, clock())
    assert mail.sent == 0

    action = (await repos.actions.list_for_task(task.id))[0]
    waiting = await repos.tasks.get(ACTOR.scope_id, task.id)
    assert waiting.checkpoint_ref
    decided, _approval = await Approvals(repos, clock=clock).decide(
        ACTOR.scope_id, task.id, action.id, actor=ACTOR,
        decision=ApprovalDecision.APPROVE, request_hash=action.request_hash,
    )
    assert decided.status is TaskStatus.QUEUED

    lease2 = await repos.tasks.claim("w1", [RunLane.FOREGROUND], TTL, clock())
    assert lease2 is not None
    task2 = await repos.tasks.get(ACTOR.scope_id, task.id)
    resumed = ResumingContextBuilder(builder, store)
    context2 = await resumed.build(task2, profile, lease2)
    execution2 = ExecutionContext(lease=lease2, actor=ACTOR, agent_role=AgentRole.MAIN, run_lane=RunLane.FOREGROUND)
    outcome2 = await runner.run(context2, profile, execution2)
    assert outcome2.status is TaskStatus.SUCCEEDED, (outcome2.error and outcome2.error.message)
    assert mail.sent == 1
    final = await repos.actions.get(task.id, action.logical_key)
    assert final.status is ActionStatus.SUCCEEDED and final.approval_ref
