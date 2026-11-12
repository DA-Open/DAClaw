"""AgentRunner loop (runtime/runner.py) with the scripted model and fakes."""

import pytest

from harness.config import BudgetReminderConfig
from harness.contracts import (
    ErrorCode,
    EventType,
    Limits,
    StaleLease,
    TaskOutcome,
    TaskStatus,
    ToolOutcome,
    ToolStatus,
    VerificationStatus,
    WaitCondition,
)
from harness.context.builder import SimpleContextBuilder, StaticInstructions, StaticToolCatalog
from harness.runtime import messages as msg
from harness.runtime.models import ModelError, ScriptedModelAdapter, result_step, tool_step
from harness.runtime.runner import AgentRunner
from harness.storage.memory import InMemoryRepositories

from ..fakes import MAIN_PROFILE, READ, WRITE, Clock, FakeGateway, FakeVerifier, MemoryArtifactStore, claimed_task

pytestmark = pytest.mark.asyncio


class Harness:
    def __init__(self, script, *, limits=None, handlers=None, verify=None, reminders=None, model=None):
        self.repos = InMemoryRepositories()
        self.clock = Clock()
        self.store = MemoryArtifactStore(self.repos)
        self.gateway = FakeGateway(self.repos, self.store, handlers)
        self.verifier = FakeVerifier(self.repos, verify)
        self.model = model or ScriptedModelAdapter(script)
        self.limits = limits
        self.runner = AgentRunner(
            self.repos,
            self.model,
            self.gateway,
            self.verifier,
            self.store,
            reminders=reminders,
            clock=self.clock,
        )

    async def run(self, **kw):
        self.task, self.execution = await claimed_task(self.repos, limits=self.limits)
        builder = SimpleContextBuilder(
            self.repos,
            instructions=StaticInstructions({"main@1": "You draft documents."}),
            tools=StaticToolCatalog([READ, WRITE]),
            clock=self.clock,
        )
        context = await builder.build(self.task, MAIN_PROFILE, self.execution.lease)
        self.outcome = await self.runner.run(context, MAIN_PROFILE, self.execution, **kw)
        return self.outcome

    async def commit(self):
        return await self.repos.tasks.commit(self.execution.lease, self.outcome, self.clock())

    async def events(self, kind):
        return [e for e in await self.repos.events.list("s1", task_id=self.task.id, limit=1000) if e.type == kind]

    async def usage(self):
        return (await self.repos.tasks.get("s1", self.task.id)).usage


def write_draft():
    return tool_step(("artifacts.write", {"name": "draft", "content": "# Draft\n..."}))


def submit_draft(h):
    def step(context):
        draft = next(a for a in h.store.content if h.store.content[a].startswith(b"# Draft"))
        return result_step("Draft written.", {"draft": draft})

    return step


# -- the main chain --------------------------------------------------------------


async def test_read_write_verify_succeeds():
    h = Harness([])
    h.model._script = [tool_step(("files.read", {"path": "notes.md"})), write_draft(), submit_draft(h)]
    outcome = await h.run()
    assert outcome.status is TaskStatus.SUCCEEDED
    assert outcome.verification_ref and outcome.result_ref
    assert outcome.result_ref in outcome.artifact_refs
    assert (await h.commit()).status is TaskStatus.SUCCEEDED
    assert [c.tool for c in h.gateway.calls] == ["files.read", "artifacts.write"]
    usage = await h.usage()
    assert usage.model_calls == 3 and usage.tool_calls == 2


async def test_logical_keys_come_from_step_ids():
    h = Harness([])
    h.model._script = [
        tool_step(("files.read", {"path": "a"}), ("files.read", {"path": "b"})),
        write_draft(),
        submit_draft(h),
    ]
    await h.run()
    keys = [c.logical_key for c in h.gateway.calls]
    assert keys == ["step-1.0", "step-1.1", "step-2.0"]
    assert all(h.execution.lease.attempt_id not in k for k in keys)


async def test_second_attempt_continues_step_numbers():
    # Attempt 1 makes one tool call, then loses its lease; the task is put
    # back in the queue (safe retry) and attempt 2 finishes it.
    h = Harness([tool_step(("files.read", {"path": "a"}))])
    original = h.gateway.invoke
    lost = {"done": False}

    async def lose_lease_after_first(execution, call):
        outcome = await original(execution, call)
        if not lost["done"]:
            lost["done"] = True
            raise StaleLease("worker lost its lease", task_id=execution.lease.task_id)
        return outcome

    h.gateway.invoke = lose_lease_after_first
    with pytest.raises(StaleLease):
        await h.run()
    await h.repos.tasks.commit(h.execution.lease, TaskOutcome(status=TaskStatus.QUEUED), h.clock())
    lease2 = await h.repos.tasks.claim("w2", [h.task.run_lane], 3600, h.clock())
    execution2 = h.execution.model_copy(update={"lease": lease2})
    task2 = await h.repos.tasks.get("s1", h.task.id)
    h.model._script.extend([tool_step(("files.read", {"path": "a"})), write_draft(), submit_draft(h)])
    builder = SimpleContextBuilder(
        h.repos,
        instructions=StaticInstructions({"main@1": "x"}),
        tools=StaticToolCatalog([READ, WRITE]),
        clock=h.clock,
    )
    context = await builder.build(task2, MAIN_PROFILE, lease2)
    outcome = await h.runner.run(context, MAIN_PROFILE, execution2)
    assert outcome.status is TaskStatus.SUCCEEDED
    keys = [c.logical_key for c in h.gateway.calls]
    assert keys == ["step-1.0", "step-2.0", "step-3.0"]
    # No key clash across attempts: every call became its own action.
    actions = await h.repos.actions.list_for_task(h.task.id)
    assert sorted(a.logical_key for a in actions) == keys
    # Model usage rows are per task step, so attempt 2 did not reuse them.
    assert (await h.usage()).model_calls == 4


async def test_attempt_run_record_is_written():
    h = Harness([])
    h.model._script = [write_draft(), submit_draft(h)]
    await h.run()
    attempt = await h.repos.tasks.get_attempt("s1", h.execution.lease.attempt_id)
    assert attempt.model_version == "scripted"
    stored = h.store.json(attempt.context_ref)
    assert stored["attempt_id"] == attempt.id
    assert stored["context"]["goal"] == h.task.goal


async def test_every_tool_call_is_answered_before_the_next_model_call():
    h = Harness([])
    h.model._script = [tool_step(("files.read", {"path": "a"}), ("files.read", {"path": "b"})), write_draft(), submit_draft(h)]
    await h.run()
    second = msg.parse(h.model.calls[1].messages)
    asked = [c.id for c in second[-3].tool_calls]
    answered = [m.tool_call_id for m in second[-2:]]
    assert second[-3].role == "assistant" and asked == answered


async def test_model_call_events_store_exact_input_and_output():
    h = Harness([])
    h.model._script = [write_draft(), submit_draft(h)]
    await h.run()
    events = await h.events(EventType.MODEL_CALL)
    assert len(events) == 2
    first = events[0].payload
    stored_input = h.store.json(first["input_ref"])
    assert stored_input["context"]["messages"] == h.model.calls[0].model_dump(mode="json")["messages"]
    assert h.store.json(first["output_ref"])["kind"] == "tool_calls"
    # Trajectory content is not listed among the task's work artifacts.
    work = await h.repos.artifacts.list_for_task("s1", h.task.id)
    assert [a.name for a in work] == ["draft"]


# -- tool outcomes ------------------------------------------------------------------


async def test_denied_tool_goes_back_to_the_model():
    denied = ToolOutcome(status=ToolStatus.DENIED, summary="not granted")
    h = Harness([], handlers={"files.read": lambda call: denied})
    h.model._script = [tool_step(("files.read", {"path": "/etc/passwd"})), write_draft(), submit_draft(h)]
    outcome = await h.run()
    assert outcome.status is TaskStatus.SUCCEEDED
    answer = msg.parse(h.model.calls[1].messages)[-1]
    assert answer.role == "tool" and answer.is_error and "denied" in answer.content


@pytest.mark.parametrize(
    ("status", "condition"),
    [(ToolStatus.NEEDS_APPROVAL, WaitCondition.APPROVAL), (ToolStatus.UNKNOWN, WaitCondition.RECONCILIATION)],
)
async def test_approval_or_unknown_stops_the_attempt(status, condition):
    h = Harness([], handlers={"artifacts.write": lambda call: ToolOutcome(status=status)})
    h.model._script = [write_draft(), result_step("should not get here")]
    outcome = await h.run()
    assert outcome.status is TaskStatus.WAITING and outcome.wait_condition is condition
    assert outcome.checkpoint_ref
    checkpoint = h.store.json(outcome.checkpoint_ref)["checkpoint"]
    assert checkpoint["pending_action_ids"] and checkpoint["stage"] == f"waiting_{condition}"
    assert len(h.model.calls) == 1  # the model is not asked to try another way
    assert (await h.commit()).status is TaskStatus.WAITING


async def test_unknown_tool_and_bad_arguments_are_errors_for_the_model():
    h = Harness([])
    h.model._script = [tool_step(("shell.exec", {"cmd": "rm -rf /"})), write_draft(), submit_draft(h)]
    outcome = await h.run()
    assert outcome.status is TaskStatus.SUCCEEDED
    assert [c.tool for c in h.gateway.calls] == ["artifacts.write"]
    answer = msg.parse(h.model.calls[1].messages)[-1]
    assert answer.is_error and "unknown tool" in answer.content


async def test_submit_result_mixed_with_other_tools_is_rejected():
    h = Harness([])
    h.model._script = [
        tool_step(("artifacts.write", {"name": "draft", "content": "# Draft"}), ("submit_result", {"summary": "x"})),
        submit_draft(h),
    ]
    outcome = await h.run()
    assert outcome.status is TaskStatus.SUCCEEDED
    answers = msg.parse(h.model.calls[1].messages)[-2:]
    assert not answers[0].is_error and answers[1].is_error


# -- verification ------------------------------------------------------------------


async def test_verification_failed_ends_without_repair():
    h = Harness([])
    h.model._script = [write_draft(), result_step("done, trust me")]  # names no artifact
    outcome = await h.run()
    assert outcome.status is TaskStatus.FAILED
    assert outcome.error.code is ErrorCode.VERIFICATION_FAILED
    assert outcome.verification_ref and outcome.artifact_refs
    assert len(h.model.calls) == 2


async def test_inconclusive_waits_for_the_user_and_is_never_succeeded():
    h = Harness([], verify=VerificationStatus.INCONCLUSIVE)
    h.model._script = [write_draft(), submit_draft(h)]
    outcome = await h.run()
    assert outcome.status is TaskStatus.WAITING and outcome.wait_condition is WaitCondition.INPUT
    assert outcome.result_ref is None and outcome.verification_ref


# -- stopping -----------------------------------------------------------------------


async def test_model_call_budget_used_up_fails_and_keeps_artifacts():
    # The model ignores the wrap-up turn and keeps asking for tools.
    h = Harness([], limits=Limits(max_model_calls=3))
    h.model._script = [write_draft(), tool_step(("files.read", {"path": "a"})), tool_step(("files.read", {"path": "b"}))]
    outcome = await h.run()
    assert outcome.status is TaskStatus.FAILED and outcome.error.code is ErrorCode.BUDGET_EXHAUSTED
    assert len(outcome.artifact_refs) == 1
    assert (await h.usage()).model_calls == 3


async def test_wrap_up_turn_offers_no_work_tools():
    h = Harness([], limits=Limits(max_model_calls=3))
    h.model._script = [write_draft(), tool_step(("files.read", {"path": "a"})), submit_draft(h)]
    outcome = await h.run()
    assert outcome.status is TaskStatus.SUCCEEDED
    last = h.model.calls[-1]
    assert [t.name for t in last.tool_specs] == [msg.SUBMIT_RESULT]
    assert "Tools are not available" in msg.parse(last.messages)[-1].content


async def test_tool_call_in_wrap_up_turn_is_refused():
    h = Harness([], limits=Limits(max_model_calls=2))
    h.model._script = [write_draft(), tool_step(("files.read", {"path": "a"}))]
    outcome = await h.run()
    assert [c.tool for c in h.gateway.calls] == ["artifacts.write"]
    assert outcome.error.code is ErrorCode.BUDGET_EXHAUSTED


async def test_token_budget_used_up_fails():
    h = Harness([], limits=Limits(max_total_tokens=1500))
    h.model._script = [write_draft(), write_draft(), write_draft(), write_draft()]
    outcome = await h.run()
    assert outcome.status is TaskStatus.FAILED and outcome.error.code is ErrorCode.BUDGET_EXHAUSTED


async def test_tool_budget_used_up_turns_into_wrap_up():
    h = Harness([], limits=Limits(max_tool_calls=1))
    h.model._script = [write_draft(), submit_draft(h)]
    outcome = await h.run()
    assert outcome.status is TaskStatus.SUCCEEDED
    assert [t.name for t in h.model.calls[-1].tool_specs] == [msg.SUBMIT_RESULT]


async def test_cancel_request_stops_before_the_next_call():
    h = Harness([write_draft(), result_step("x")])
    original = h.gateway.invoke

    async def invoke(execution, call):
        result = await original(execution, call)
        await h.repos.tasks.request_cancel("s1", execution.lease.task_id, "user", h.clock())
        return result

    h.gateway.invoke = invoke
    outcome = await h.run()
    assert outcome.status is TaskStatus.CANCELLED
    assert len(h.model.calls) == 1
    assert (await h.commit()).status is TaskStatus.CANCELLED


async def test_deadline_stops_the_loop():
    h = Harness([], limits=Limits(timeout_sec=60))
    h.model._script = [write_draft(), submit_draft(h)]
    original = h.gateway.invoke

    async def slow(execution, call):
        h.clock.advance(120)
        return await original(execution, call)

    h.gateway.invoke = slow
    outcome = await h.run()
    assert outcome.status is TaskStatus.FAILED and outcome.error.code is ErrorCode.DEADLINE_EXCEEDED


async def test_lost_lease_raises_and_returns_nothing():
    h = Harness([write_draft(), result_step("x")])
    calls = {"n": 0}

    def lease_lost():
        calls["n"] += 1
        return calls["n"] > 1

    with pytest.raises(StaleLease):
        await h.run(lease_lost=lease_lost)


# -- model failures -------------------------------------------------------------------


async def test_retryable_model_error_is_retried_and_released():
    h = Harness([])
    h.model._script = [ModelError("HTTP 503", retryable=True, status_code=503, responded=True), write_draft(), submit_draft(h)]
    outcome = await h.run()
    assert outcome.status is TaskStatus.SUCCEEDED
    assert (await h.usage()).model_calls == 2  # the refused call cost nothing


async def test_timeout_is_charged_at_the_reserved_amount():
    h = Harness([])
    h.model._script = [ModelError("timed out", retryable=True, responded=False), write_draft(), submit_draft(h)]
    await h.run()
    assert (await h.usage()).model_calls == 3


async def test_persistent_model_error_fails():
    err = ModelError("HTTP 400 bad request", retryable=False, status_code=400, responded=True)
    h = Harness([err])
    outcome = await h.run()
    assert outcome.status is TaskStatus.FAILED and outcome.error.code is ErrorCode.MODEL_ERROR


# -- reminders in the loop -----------------------------------------------------------------


async def test_reminders_are_messages_and_events():
    h = Harness([], limits=Limits(max_model_calls=20))
    h.model._script = [write_draft()] + [tool_step(("files.read", {"path": "x"}))] * 18 + [submit_draft(h)]
    outcome = await h.run()
    assert outcome.status is TaskStatus.SUCCEEDED
    events = await h.events(EventType.BUDGET_REMINDER)
    assert [(e.payload["tier"], e.payload["remaining"]["model_calls"]) for e in events] == [
        ("50%", 10),
        ("20%", 4),
        ("last_tool_turn", 2),
        ("wrap_up", 1),
    ]
    reminders = [m for m in msg.parse(h.model.calls[-1].messages) if m.kind == "budget_reminder"]
    assert len(reminders) == 4


async def test_two_tiers_on_the_same_call_send_one_reminder():
    # With 10 calls, 20% and the last tool turn both fall on call 9.
    h = Harness([], limits=Limits(max_model_calls=10))
    h.model._script = [write_draft()] + [tool_step(("files.read", {"path": "x"}))] * 8 + [submit_draft(h)]
    await h.run()
    tiers = [e.payload["tier"] for e in await h.events(EventType.BUDGET_REMINDER)]
    assert tiers == ["50%", "last_tool_turn", "wrap_up"]


async def test_reminders_can_be_switched_off():
    h = Harness([], limits=Limits(max_model_calls=10), reminders=BudgetReminderConfig(enabled=False, final_turn=False))
    h.model._script = [write_draft()] * 9 + [submit_draft(h)]
    await h.run()
    tiers = [e.payload["tier"] for e in await h.events(EventType.BUDGET_REMINDER)]
    assert tiers == ["wrap_up"]


# -- context sizes (context_built events) ----------------------------------------


async def test_every_model_call_has_a_context_built_event():
    h = Harness([])
    h.model._script = [tool_step(("files.read", {"path": "notes.md"})), write_draft(), submit_draft(h)]
    await h.run()
    built = await h.events(EventType.CONTEXT_BUILT)
    calls = await h.events(EventType.MODEL_CALL)
    assert len(built) == len(calls) == 3
    assert [b.payload["call_id"] for b in built] == [c.payload["call_id"] for c in calls]
    # Each context_built event comes right before its model call.
    assert all(b.seq < c.seq for b, c in zip(built, calls))


async def test_context_sections_grow_with_the_conversation():
    h = Harness([])
    h.model._script = [tool_step(("files.read", {"path": "notes.md"})), write_draft(), submit_draft(h)]
    await h.run()
    built = [e.payload["sizes"] for e in await h.events(EventType.CONTEXT_BUILT)]
    names = [[s["name"] for s in b["sections"]] for b in built]
    assert names[0] == ["system", "task_brief", "tool_specs"]
    assert names[1] == ["system", "task_brief", "history", "tool_results", "tool_specs"]
    by_name = [{s["name"]: s for s in b["sections"]} for b in built]
    assert by_name[2]["tool_results"]["items"] == 2
    assert by_name[0]["system"]["chars"] == by_name[2]["system"]["chars"]
    assert built[0]["est_tokens"] < built[1]["est_tokens"] < built[2]["est_tokens"]


async def test_context_sizes_match_the_reservation_estimate():
    from harness.runtime.models.base import estimate_tokens

    h = Harness([])
    h.model._script = [write_draft(), submit_draft(h)]
    await h.run()
    built = await h.events(EventType.CONTEXT_BUILT)
    for event, sent in zip(built, h.model.calls):
        sizes = event.payload["sizes"]
        assert sizes["est_tokens"] == estimate_tokens(sent.messages, sent.tool_specs)
        assert sum(s["chars"] for s in sizes["sections"]) <= sizes["total_chars"]


async def test_unexpected_adapter_error_closes_its_reservation():
    """Review of #14: a non-ModelError must not leave the reservation open."""
    h = Harness([ValueError("bad AssistantStep")], limits=Limits(max_model_calls=5))
    outcome = await h.run()
    assert outcome.status is TaskStatus.FAILED
    assert outcome.error.code is ErrorCode.MODEL_ERROR
    assert "ValueError" in outcome.error.message
    assert await h.repos.usage.open_reservations(h.task.id) == []
    # The call may have run, so it is charged at the reserved amount.
    assert (await h.usage()).model_calls == 1
    calls = await h.events(EventType.MODEL_CALL)
    assert len(calls) == 1 and "ValueError" in calls[0].payload["error"]


async def test_stale_lease_from_adapter_closes_its_reservation():
    h = Harness([StaleLease("lease gone")])
    with pytest.raises(StaleLease):
        await h.run()
    assert await h.repos.usage.open_reservations(h.task.id) == []


async def test_reminders_and_wrap_up_show_in_context_sizes():
    h = Harness([], limits=Limits(max_model_calls=4))
    h.model._script = [write_draft()] * 3 + [submit_draft(h)]
    await h.run()
    built = [e.payload for e in await h.events(EventType.CONTEXT_BUILT)]
    last = {s["name"]: s for s in built[-1]["sizes"]["sections"]}
    assert built[-1]["wrap_up"] is True
    assert last["budget_reminders"]["items"] >= 1
    assert last["tool_specs"]["items"] == 1  # only submit_result in the wrap-up turn
