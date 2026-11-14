"""Scenario: a model that never stops calling tools still returns a result.

The scripted model behaves like a real one that only stops when it has to:
it calls a tool whenever tools are offered, and submits once the Runner
takes them away. All three reminders must appear, the last turn must offer
no work tools, and the task must end verified, not budget_exhausted.
"""

import pytest

from harness.contracts import EventType, Limits, StepKind, TaskStatus
from harness.context.builder import SimpleContextBuilder, StaticInstructions, StaticToolCatalog
from harness.runtime import messages as msg
from harness.runtime.models import ScriptedModelAdapter, result_step, tool_step
from harness.runtime.runner import AgentRunner
from harness.storage.memory import InMemoryRepositories

from ..fakes import MAIN_PROFILE, READ, WRITE, Clock, FakeGateway, FakeVerifier, MemoryArtifactStore, claimed_task

pytestmark = pytest.mark.asyncio


async def test_tool_hungry_model_wraps_up_with_a_verified_result():
    repos = InMemoryRepositories()
    clock = Clock()
    store = MemoryArtifactStore(repos)
    written: dict[str, str] = {}

    def keep_going(context):
        offered = {t.name for t in context.tool_specs}
        if WRITE.name not in offered:
            return result_step("Draft from the notes read so far.", written)
        if not written:
            return tool_step((WRITE.name, {"name": "draft", "content": "# Draft\n- point"}))
        return tool_step((READ.name, {"path": f"notes/{len(context.messages)}.md"}))

    model = ScriptedModelAdapter([keep_going], repeat_last=True)
    gateway = FakeGateway(repos, store)
    original = gateway.invoke

    async def remember_draft(execution, call):
        outcome = await original(execution, call)
        if call.tool == WRITE.name:
            written["draft"] = outcome.artifact_refs[0]
        return outcome

    gateway.invoke = remember_draft
    runner = AgentRunner(repos, model, gateway, FakeVerifier(repos), store, clock=clock)
    task, execution = await claimed_task(repos, limits=Limits(max_model_calls=20))
    builder = SimpleContextBuilder(
        repos,
        instructions=StaticInstructions({"main@1": "You draft documents."}),
        tools=StaticToolCatalog([READ, WRITE]),
        clock=clock,
    )
    context = await builder.build(task, MAIN_PROFILE, execution.lease)

    outcome = await runner.run(context, MAIN_PROFILE, execution)

    assert outcome.status is TaskStatus.SUCCEEDED
    assert (await repos.tasks.commit(execution.lease, outcome, clock())).status is TaskStatus.SUCCEEDED
    events = await repos.events.list("s1", task_id=task.id, limit=1000)
    tiers = [e.payload["tier"] for e in events if e.type is EventType.BUDGET_REMINDER]
    assert tiers == ["50%", "20%", "last_tool_turn", "wrap_up"]
    calls = [e for e in events if e.type is EventType.MODEL_CALL]
    assert len(calls) == 20
    assert calls[-1].payload["step_kind"] == StepKind.CANDIDATE_RESULT
    assert [t.name for t in model.calls[-1].tool_specs] == [msg.SUBMIT_RESULT]
    assert (await repos.tasks.get("s1", task.id)).usage.model_calls == 20
