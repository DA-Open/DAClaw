"""R1 checkpoint replay with the real gateway and files.read connector."""

import json

import pytest

from harness.contracts import (
    ActionConflict,
    ActionStatus,
    AgentRole,
    Checkpoint,
    ErrorCode,
    EventType,
    Limits,
    StaleLease,
    TaskOutcome,
    TaskStatus,
    ToolOutcome,
    ToolStatus,
    Usage,
    UsageKind,
    WaitCondition,
)
from harness.context.builder import SimpleContextBuilder, StaticInstructions, StaticToolCatalog
from harness.context.resume import ResumingContextBuilder
from harness.runtime import messages as msg
from harness.runtime.models import ScriptedModelAdapter, result_step, tool_step
from harness.runtime.models.base import estimate_tokens
from harness.runtime.runner import AgentRunner
from harness.storage.memory import InMemoryRepositories
from harness.tools import ToolGateway, UserGrant, WhitelistPolicy
from harness.tools.connectors.files import FilesReadConnector

from ..fakes import NoRecompute, ACTOR, MAIN_PROFILE, TTL, Clock, FakeVerifier, MemoryArtifactStore, claimed_task

pytestmark = pytest.mark.asyncio


class CountingRead(FilesReadConnector):
    def __init__(self, artifacts):
        super().__init__(artifacts)
        self.calls = []

    async def execute(self, call):
        self.calls.append(call)
        return await super().execute(call)


class Setup:
    def __init__(self, tmp_path):
        self.path = tmp_path / "notes.md"
        self.path.write_text("Source material", encoding="utf-8")
        self.clock = Clock()
        self.repos = InMemoryRepositories()
        self.store = MemoryArtifactStore(self.repos)
        self.connector = CountingRead(self.store)
        self.model = ScriptedModelAdapter()
        self.gateway = ToolGateway(
            self.repos,
            self.store,
            WhitelistPolicy(
                roles={"main_tools@1": {"files.read"}},
                users={"s1": {AgentRole.MAIN: UserGrant(paths=(str(tmp_path),), ref="grant:s1")}},
                capabilities={},
            ),
            [self.connector],
            profiles={"main@1": MAIN_PROFILE.tool_policy_ref},
            clock=self.clock,
        )
        self.builder = ResumingContextBuilder(
            SimpleContextBuilder(
                self.repos,
                instructions=StaticInstructions({"main@1": "Current instructions"}),
                tools=StaticToolCatalog([self.connector.spec]),
                clock=self.clock,
                recomputer=NoRecompute(),
            ),
            self.store,
        )
        self.runner = AgentRunner(
            self.repos, self.model, self.gateway, FakeVerifier(self.repos), self.store, clock=self.clock
        )

    def read_step(self):
        return tool_step(("files.read", {"path": str(self.path)}))

    async def start(self, limits=None):
        self.task, self.execution = await claimed_task(self.repos, limits=limits or Limits(timeout_sec=20000))
        return self

    async def run(self):
        task = await self.repos.tasks.get("s1", self.task.id)
        context = await self.builder.build(task, MAIN_PROFILE, self.execution.lease)
        return await self.runner.run(context, MAIN_PROFILE, self.execution)

    async def recover(self):
        self.clock.advance(TTL + 1)
        assert await self.repos.tasks.reclaim_expired(self.clock(), 3) == 1
        lease = await self.repos.tasks.claim("w2", [self.task.run_lane], TTL, self.clock())
        assert lease is not None
        self.execution = self.execution.model_copy(update={"lease": lease})

    async def finish(self):
        outcome = await self.run()
        assert outcome.status is TaskStatus.SUCCEEDED
        return await self.repos.tasks.commit(self.execution.lease, outcome, self.clock())

    def checkpoints(self):
        return [json.loads(raw) for raw in self.store.content.values() if b'"checkpoint":{' in raw]


async def test_tool_step_writes_checkpoint_before_and_after(tmp_path):
    s = await Setup(tmp_path).start()
    step = s.read_step()
    s.model._script = [step, result_step("Done", {"draft": "result"})]
    await s.finish()
    checkpoints = s.checkpoints()
    assert len(checkpoints) == 2
    expected = [step.tool_calls[0].model_copy(update={"logical_key": "step-1.0"}).model_dump(mode="json")]
    assert checkpoints[0]["checkpoint"]["pending_calls"] == expected
    assert checkpoints[1]["checkpoint"]["pending_calls"] == []
    assert checkpoints[0]["messages"][-1]["role"] == "assistant"
    assert checkpoints[1]["messages"][-1]["role"] == "tool"
    current = await s.repos.tasks.get("s1", s.task.id)
    assert s.store.json(current.checkpoint_ref) == checkpoints[1]


async def test_resume_skips_succeeded_action(tmp_path, monkeypatch):
    s = await Setup(tmp_path).start()
    s.model._script = [s.read_step(), result_step("Done", {"draft": "result"})]
    invoke = s.gateway.invoke

    async def crash_after_success(execution, call):
        await invoke(execution, call)
        raise StaleLease("crash after files.read")

    monkeypatch.setattr(s.gateway, "invoke", crash_after_success)
    with pytest.raises(StaleLease):
        await s.run()
    assert len(s.connector.calls) == 1
    assert len(s.checkpoints()) == 1
    await s.recover()
    monkeypatch.setattr(s.gateway, "invoke", invoke)
    await s.finish()
    assert len(s.connector.calls) == 1
    actions = await s.repos.actions.list_for_task(s.task.id)
    assert [a.logical_key for a in actions] == ["step-1.0"]
    assert s.model.calls[1].messages[-1]["role"] == "tool"


async def test_resume_replays_unfinished_readonly_call(tmp_path, monkeypatch):
    s = await Setup(tmp_path).start()
    s.model._script = [s.read_step(), result_step("Done", {"draft": "result"})]
    execute = s.connector.execute

    async def crash_before_connector(call):
        raise StaleLease("crash before connector work")

    monkeypatch.setattr(s.connector, "execute", crash_before_connector)
    with pytest.raises(StaleLease):
        await s.run()
    (action,) = await s.repos.actions.list_for_task(s.task.id)
    assert action.status is ActionStatus.RUNNING
    assert len(s.connector.calls) == 0
    assert len(s.checkpoints()) == 1
    await s.recover()
    monkeypatch.setattr(s.connector, "execute", execute)
    await s.finish()
    assert len(s.connector.calls) == 1
    assert s.connector.calls[0].logical_key == "step-1.0"
    assert len(await s.repos.actions.list_for_task(s.task.id)) == 1


async def test_resume_trims_oversized_history_after_replay(tmp_path):
    limits = Limits(timeout_sec=20000, max_total_tokens=2800)
    s = await Setup(tmp_path).start(limits)
    call = s.read_step().tool_calls[0].model_copy(update={"logical_key": "step-2.0"})
    old_tool_call = msg.ToolCallPart(id="old-call", name=call.tool)
    pending_tool_call = msg.ToolCallPart(id=call.native_call_id, name=call.tool, args=call.args)
    messages = [
        msg.system("Old system"),
        msg.user("Original task brief"),
        msg.Message(role="assistant", tool_calls=[old_tool_call]).to_dict(),
        msg.tool_result("old-call", call.tool, "x" * 8000),
        msg.Message(role="assistant", tool_calls=[pending_tool_call]).to_dict(),
    ]
    checkpoint = Checkpoint(stage="tools_pending", step_id=f"{s.task.id}.step-2", pending_calls=[call])
    ref = await s.runner._store_json(s.task, "checkpoint", {"checkpoint": checkpoint, "messages": messages})
    lease = s.execution.lease
    await s.repos.tasks.checkpoint(lease, ref, s.clock())
    reserved = Usage(input_tokens=1300)
    assert (await s.repos.usage.reserve(
        s.task.id, lease.attempt_id, "prior-open", UsageKind.MODEL, reserved, s.clock(),
    )).granted
    await s.recover()
    s.model._script = [result_step("Done", {"draft": "result"})]
    await s.finish()
    sent, = s.model.calls
    max_output, = s.model.max_tokens_seen
    assert estimate_tokens(sent.messages, sent.tool_specs) + max_output + reserved.total_tokens <= limits.max_total_tokens
    assert "Current instructions" in sent.messages[0]["content"]
    assert sent.messages[1] == msg.user("Original task brief")
    tool_messages = [m for m in sent.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == [call.native_call_id]
    assert [m["tool_calls"][0]["id"] for m in sent.messages if m["role"] == "assistant"] == [call.native_call_id]
    assert [record.logical_key for record in s.connector.calls] == ["step-2.0"]


async def test_resume_does_not_reuse_model_call_ids(tmp_path, monkeypatch):
    s = await Setup(tmp_path).start()
    s.model._script = [s.read_step(), s.read_step(), s.read_step(), result_step("Done", {"draft": "result"})]
    store_json = s.runner._store_json

    async def crash_before_second_checkpoint(task, name, value):
        if name == "checkpoint" and value["checkpoint"].step_id.endswith(".step-2"):
            raise StaleLease("model step 2 finished, its checkpoint not written")
        return await store_json(task, name, value)

    monkeypatch.setattr(s.runner, "_store_json", crash_before_second_checkpoint)
    with pytest.raises(StaleLease):
        await s.run()
    await s.recover()
    monkeypatch.setattr(s.runner, "_store_json", store_json)
    await s.finish()
    events = await s.repos.events.list("s1", task_id=s.task.id)
    ids = [event.payload["call_id"] for event in events if event.type is EventType.MODEL_CALL]
    assert ids == [f"{s.task.id}.step-{n}" for n in range(1, 5)]
    assert len(ids) == len(set(ids))
    assert [call.logical_key for call in s.connector.calls] == ["step-1.0", "step-3.0"]


async def test_usage_accumulates_across_attempts(tmp_path, monkeypatch):
    s = await Setup(tmp_path).start()
    s.model._script = [
        s.read_step().model_copy(update={"usage": Usage(model_calls=1, input_tokens=120, output_tokens=20)}),
        result_step("Done", {"draft": "result"}).model_copy(
            update={"usage": Usage(model_calls=1, input_tokens=150, output_tokens=30)}
        ),
    ]
    invoke = s.gateway.invoke

    async def crash_after_success(execution, call):
        await invoke(execution, call)
        raise StaleLease("crash after files.read")

    monkeypatch.setattr(s.gateway, "invoke", crash_after_success)
    with pytest.raises(StaleLease):
        await s.run()
    await s.recover()
    monkeypatch.setattr(s.gateway, "invoke", invoke)
    task = await s.finish()
    assert task.usage.model_calls == 2
    assert task.usage.input_tokens == 270
    assert task.usage.output_tokens == 50
    assert task.usage.tool_calls == 1


@pytest.mark.parametrize(
    ("status", "condition"),
    [(ToolStatus.NEEDS_APPROVAL, WaitCondition.APPROVAL), (ToolStatus.UNKNOWN, WaitCondition.RECONCILIATION)],
)
async def test_resume_wait_keeps_original_pending_keys(tmp_path, monkeypatch, status, condition):
    s = await Setup(tmp_path).start()
    first, blocked, last = [s.read_step().tool_calls[0] for _ in range(3)]
    s.model._script = [tool_step(first, blocked, last), result_step("Must not run")]
    invoke = s.gateway.invoke

    async def wait_second(execution, call):
        if call.logical_key == "step-1.1":
            return ToolOutcome(status=status)
        return await invoke(execution, call)

    monkeypatch.setattr(s.gateway, "invoke", wait_second)
    outcome = await s.run()
    assert outcome.status is TaskStatus.WAITING
    checkpoint = s.store.json(outcome.checkpoint_ref)["checkpoint"]
    assert [call["logical_key"] for call in checkpoint["pending_calls"]] == ["step-1.1", "step-1.2"]
    await s.repos.tasks.commit(s.execution.lease, TaskOutcome(status=TaskStatus.QUEUED), s.clock())
    lease = await s.repos.tasks.claim("w2", [s.task.run_lane], TTL, s.clock())
    s.execution = s.execution.model_copy(update={"lease": lease})
    outcome = await s.run()
    assert outcome.status is TaskStatus.WAITING and outcome.wait_condition is condition
    assert len(s.model.calls) == 1
    assert len(s.connector.calls) == 1
    replayed = s.store.json(outcome.checkpoint_ref)["checkpoint"]
    assert [call["logical_key"] for call in replayed["pending_calls"]] == ["step-1.1", "step-1.2"]


async def test_resume_action_conflict_propagates(tmp_path, monkeypatch):
    s = await Setup(tmp_path).start()
    s.model._script = [s.read_step()]
    invoke = s.gateway.invoke

    async def crash_after_success(execution, call):
        await invoke(execution, call)
        raise StaleLease("crash after files.read")

    monkeypatch.setattr(s.gateway, "invoke", crash_after_success)
    with pytest.raises(StaleLease):
        await s.run()
    task = await s.repos.tasks.get("s1", s.task.id)
    saved = s.store.json(task.checkpoint_ref)
    saved["checkpoint"]["pending_calls"][0]["args"]["path"] = str(s.path) + ".changed"
    s.store.content[task.checkpoint_ref] = json.dumps(saved).encode()
    await s.recover()
    monkeypatch.setattr(s.gateway, "invoke", invoke)
    with pytest.raises(ActionConflict):
        await s.run()
    assert len(s.connector.calls) == 1


async def test_cancelled_resume_stops_before_replaying(tmp_path, monkeypatch):
    s = await Setup(tmp_path).start()
    s.model._script = [s.read_step()]
    invoke = s.gateway.invoke

    async def crash_before_gateway(execution, call):
        raise StaleLease("crash before files.read")

    monkeypatch.setattr(s.gateway, "invoke", crash_before_gateway)
    with pytest.raises(StaleLease):
        await s.run()
    await s.recover()
    monkeypatch.setattr(s.gateway, "invoke", invoke)
    await s.repos.tasks.request_cancel("s1", s.task.id, "user cancelled", s.clock())
    assert (await s.run()).status is TaskStatus.CANCELLED
    assert len(s.connector.calls) == 0
    assert len(s.model.calls) == 1


async def test_resume_preserves_wrap_up_tool_denial(tmp_path, monkeypatch):
    s = Setup(tmp_path)
    s.task, s.execution = await claimed_task(s.repos, limits=Limits(max_model_calls=1, timeout_sec=20000))
    s.model._script = [s.read_step()]
    checkpoint = s.repos.tasks.checkpoint

    async def crash_after_checkpoint(lease, ref, now):
        await checkpoint(lease, ref, now)
        raise StaleLease("crash before refusing wrap-up tool call")

    monkeypatch.setattr(s.repos.tasks, "checkpoint", crash_after_checkpoint)
    with pytest.raises(StaleLease):
        await s.run()
    await s.recover()
    monkeypatch.setattr(s.repos.tasks, "checkpoint", checkpoint)
    outcome = await s.run()
    assert outcome.status is TaskStatus.FAILED and outcome.error.code is ErrorCode.BUDGET_EXHAUSTED
    assert len(s.connector.calls) == 0
    assert len(s.model.calls) == 1
