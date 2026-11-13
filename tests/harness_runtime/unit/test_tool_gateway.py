"""ToolGateway: policy, action reuse, budget and the file connectors (item 5)."""

import pytest

from harness.contracts import (
    Action,
    ActionConflict,
    ActionStatus,
    AgentRole,
    ErrorCode,
    EventType,
    HarnessError,
    Limits,
    ReconcileResult,
    StaleLease,
    ToolCall,
    ToolSpec,
    ToolStatus,
)
from harness.storage.artifacts import InMemoryArtifactStore
from harness.storage.memory import InMemoryRepositories
from harness.tools.catalog import PolicyToolCatalog
from harness.tools.connectors.files import ReadFileConnector, WriteFileConnector
from harness.tools.gateway import ToolGateway
from harness.tools.policy import PolicyDecision, ToolPolicy

from ..fakes import MAIN_PROFILE, Clock, claimed_task

pytestmark = pytest.mark.asyncio

ALL_MAIN = frozenset({"files.read", "files.write"})


class GatewayHarness:
    def __init__(
        self,
        root,
        *,
        role_tools=None,
        user_tools=None,
        approval=frozenset(),
        extra_connectors=None,
    ):
        self.root = root
        self.repos = InMemoryRepositories()
        self.clock = Clock()
        self.store = InMemoryArtifactStore(self.repos)
        self.connectors = {
            "files.read": ReadFileConnector(root, artifacts=self.store),
            "files.write": WriteFileConnector(root, self.store),
        }
        self.connectors.update(extra_connectors or {})
        self.policy = ToolPolicy(
            role_tools={AgentRole.MAIN.value: ALL_MAIN} if role_tools is None else role_tools,
            user_tools=user_tools or {},
            approval_required=approval,
        )
        self.gateway = ToolGateway(
            self.repos, self.policy, self.connectors, self.store, clock=self.clock
        )

    async def start(self, limits=None):
        self.task, self.execution = await claimed_task(self.repos, limits=limits)
        return self.task, self.execution

    async def usage(self):
        return (await self.repos.tasks.get("s1", self.task.id)).usage

    async def actions(self):
        return await self.repos.actions.list_for_task(self.task.id)


def write_call(key="step-1.0", path="draft.md", content="# Draft", name="draft"):
    return ToolCall(
        tool="files.write",
        args={"path": path, "content": content, "name": name},
        logical_key=key,
    )


def read_call(key="step-1.0", path="draft.md"):
    return ToolCall(tool="files.read", args={"path": path}, logical_key=key)


# -- the happy path ----------------------------------------------------------


async def test_write_then_read_produces_artifacts(tmp_path):
    h = GatewayHarness(tmp_path)
    await h.start()

    written = await h.gateway.invoke(h.execution, write_call())
    assert written.status is ToolStatus.SUCCEEDED
    assert len(written.artifact_refs) == 1
    assert (tmp_path / "draft.md").read_text() == "# Draft"

    used = await h.gateway.invoke(h.execution, read_call(key="step-1.1"))
    assert used.status is ToolStatus.SUCCEEDED
    assert used.summary == "# Draft"

    assert (await h.usage()).tool_calls == 2
    artifacts = await h.repos.artifacts.list_for_task("s1", h.task.id)
    assert [a.name for a in artifacts] == ["draft"]
    events = [
        e
        for e in await h.repos.events.list("s1", task_id=h.task.id, limit=100)
        if e.type is EventType.TOOL_ACTION
    ]
    assert [e.payload["status"] for e in events] == ["succeeded", "succeeded"]


async def test_same_key_and_args_reuse_the_recorded_outcome(tmp_path):
    h = GatewayHarness(tmp_path)
    await h.start()

    first = await h.gateway.invoke(h.execution, write_call())
    second = await h.gateway.invoke(h.execution, write_call())

    assert second.action_id == first.action_id
    assert second.status is ToolStatus.SUCCEEDED
    assert second.summary == first.summary
    assert second.artifact_refs == first.artifact_refs
    assert len(await h.actions()) == 1
    assert (await h.usage()).tool_calls == 1
    # The connector ran once: one work artifact, not two.
    assert len(await h.repos.artifacts.list_for_task("s1", h.task.id)) == 1


async def test_failed_outcome_is_reused_without_rerunning(tmp_path):
    h = GatewayHarness(tmp_path)
    await h.start()
    call = read_call(path="missing.txt")

    first = await h.gateway.invoke(h.execution, call)
    second = await h.gateway.invoke(h.execution, call)

    assert first.status is ToolStatus.FAILED
    assert second.status is ToolStatus.FAILED
    assert second.action_id == first.action_id
    assert second.error.message == first.error.message
    assert (await h.usage()).tool_calls == 1


async def test_same_key_with_different_args_conflicts(tmp_path):
    h = GatewayHarness(tmp_path)
    await h.start()
    await h.gateway.invoke(h.execution, write_call())

    with pytest.raises(ActionConflict):
        await h.gateway.invoke(h.execution, write_call(content="# Different"))


# -- policy ------------------------------------------------------------------


async def test_role_whitelist_denies_without_registering_or_charging(tmp_path):
    h = GatewayHarness(tmp_path, role_tools={AgentRole.MAIN.value: {"files.read"}})
    await h.start()

    denied = await h.gateway.invoke(h.execution, write_call())

    assert denied.status is ToolStatus.DENIED
    assert denied.action_id is None
    assert denied.error.code is ErrorCode.FORBIDDEN
    assert await h.actions() == []
    assert (await h.usage()).tool_calls == 0
    assert not (tmp_path / "draft.md").exists()


async def test_user_whitelist_can_only_narrow_the_role_whitelist(tmp_path):
    narrow = GatewayHarness(
        tmp_path,
        role_tools={AgentRole.MAIN.value: ALL_MAIN},
        user_tools={("s1", AgentRole.MAIN.value): {"files.read"}},
    )
    await narrow.start()
    assert (await narrow.gateway.invoke(narrow.execution, write_call())).status is ToolStatus.DENIED

    widen = GatewayHarness(
        tmp_path,
        role_tools={AgentRole.MAIN.value: {"files.read"}},
        user_tools={("s1", AgentRole.MAIN.value): {"files.write"}},
    )
    await widen.start()
    assert (await widen.gateway.invoke(widen.execution, write_call())).status is ToolStatus.DENIED
    assert widen.policy.allowed_tools(AgentRole.MAIN, "s1") == frozenset()


async def test_policy_decisions():
    policy = ToolPolicy(
        role_tools={AgentRole.MAIN.value: {"files.read", "files.write"}},
        user_tools={("s1", AgentRole.MAIN.value): {"files.read"}},
        approval_required=frozenset({"files.read"}),
    )
    # The user whitelist removed files.write for s1 only.
    assert policy.authorize(AgentRole.MAIN, "s1", "files.write") is PolicyDecision.DENIED
    assert policy.authorize(AgentRole.MAIN, "s1", "files.read") is PolicyDecision.NEEDS_APPROVAL
    assert policy.authorize(AgentRole.MAIN, "s1", "shell.exec") is PolicyDecision.DENIED
    # A user without an entry keeps the role list.
    assert policy.allowed_tools(AgentRole.MAIN, "s2") == frozenset({"files.read", "files.write"})


async def test_needs_approval_runs_nothing_and_waits(tmp_path):
    h = GatewayHarness(tmp_path, approval=frozenset({"files.write"}))
    await h.start()

    outcome = await h.gateway.invoke(h.execution, write_call())
    assert outcome.status is ToolStatus.NEEDS_APPROVAL
    assert outcome.action_id

    actions = await h.actions()
    assert [a.status for a in actions] == [ActionStatus.PLANNED]
    assert (await h.usage()).tool_calls == 0
    assert not (tmp_path / "draft.md").exists()
    # A retry of the same key returns the same pending state.
    assert (await h.gateway.invoke(h.execution, write_call())).status is ToolStatus.NEEDS_APPROVAL


async def test_catalog_offers_only_the_policy_intersection(tmp_path):
    h = GatewayHarness(
        tmp_path,
        role_tools={AgentRole.MAIN.value: ALL_MAIN},
        user_tools={("s1", AgentRole.MAIN.value): {"files.read"}},
    )
    await h.start()
    catalog = PolicyToolCatalog.from_connectors(h.connectors, h.policy)

    names = [spec.name for spec in await catalog.tools_for(h.task, MAIN_PROFILE)]
    assert names == ["files.read"]
    assert "submit_result" not in names


# -- budget ------------------------------------------------------------------


async def test_budget_refusal_returns_denied_and_spends_nothing(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    h = GatewayHarness(tmp_path)
    await h.start(limits=Limits(max_tool_calls=1))

    first = await h.gateway.invoke(h.execution, read_call(key="step-1.0", path="a.txt"))
    assert first.status is ToolStatus.SUCCEEDED

    refused = await h.gateway.invoke(h.execution, read_call(key="step-1.1", path="b.txt"))
    assert refused.status is ToolStatus.DENIED
    assert refused.action_id is None
    assert refused.error.code is ErrorCode.BUDGET_EXHAUSTED
    assert (await h.usage()).tool_calls == 1


# -- refusals and recovery ---------------------------------------------------


async def test_old_lease_is_refused(tmp_path):
    h = GatewayHarness(tmp_path)
    await h.start()
    stale = h.execution.model_copy(
        update={"lease": h.execution.lease.model_copy(update={"lease_epoch": 99})}
    )

    with pytest.raises(StaleLease):
        await h.gateway.invoke(stale, write_call())


async def test_unknown_tool_raises_for_the_runner(tmp_path):
    h = GatewayHarness(tmp_path, role_tools={AgentRole.MAIN.value: {"shell.exec"}})
    await h.start()
    call = ToolCall(tool="shell.exec", args={"cmd": "ls"}, logical_key="step-1.0")

    with pytest.raises(HarnessError) as raised:
        await h.gateway.invoke(h.execution, call)
    assert raised.value.code is ErrorCode.TOOL_ERROR


async def test_reusing_an_action_still_running_reports_unknown(tmp_path):
    h = GatewayHarness(tmp_path)
    await h.start()
    call = write_call()
    await h.repos.actions.register(
        Action(
            task_id=h.task.id,
            attempt_id=h.execution.lease.attempt_id,
            logical_key=call.logical_key,
            tool=call.tool,
            request_hash=call.request_hash(),
            read_only=False,
            status=ActionStatus.RUNNING,
        )
    )

    outcome = await h.gateway.invoke(h.execution, call)

    # A worker died mid-call: the safe answer is "unknown", not a second write.
    assert outcome.status is ToolStatus.UNKNOWN
    assert outcome.error.code is ErrorCode.TOOL_ERROR
    assert not (tmp_path / "draft.md").exists()


class ExplodingConnector:
    spec = ToolSpec(
        name="boom.exec",
        description="Always fails.",
        input_schema={"type": "object"},
        read_only=False,
        side_effects=True,
    )

    async def execute(self, call):
        raise RuntimeError("network down")

    async def reconcile(self, action):
        return ReconcileResult(status="unsupported")


async def test_connector_crash_with_side_effects_is_unknown(tmp_path):
    h = GatewayHarness(
        tmp_path,
        role_tools={AgentRole.MAIN.value: {"boom.exec"}},
        extra_connectors={"boom.exec": ExplodingConnector()},
    )
    await h.start()

    outcome = await h.gateway.invoke(
        h.execution, ToolCall(tool="boom.exec", args={}, logical_key="step-1.0")
    )

    assert outcome.status is ToolStatus.UNKNOWN
    assert outcome.error.code is ErrorCode.TOOL_ERROR
    assert [a.status for a in await h.actions()] == [ActionStatus.UNKNOWN]
    # The outcome is unknown, so the tool reservation stays open for reconciliation.
    assert (await h.usage()).tool_calls == 0
