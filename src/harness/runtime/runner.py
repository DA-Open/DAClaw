"""AgentRunner: the model / tool loop of one attempt (spec 4.1 steps 4-7).

Each round:

1. Check cancellation, deadline, lease and the accumulated budget. Any of
   them ends the attempt; there is no unbounded loop.
2. Add a budget reminder when a tier is crossed (review comment 6). When
   only the wrap-up call is left, offer no work tools: the model must return
   its result (DaraOS addition, pending review).
3. Reserve the model call, call the model, settle real usage, write a
   ``model_call`` event with the exact input and output.
4. ``tool_calls``: give each call a ``logical_key`` from the durable step
   number (counted over the whole task, no attempt ID; see ``_logical_key``)
   and pass it to ToolGateway (which reserves the tool budget). ``needs_approval`` or
   ``unknown`` ends the attempt in ``waiting`` with a checkpoint; the model
   is not allowed to try the same action another way (spec 5.2).
   ``candidate_result``: hand it to the Verifier. Only ``passed`` gives
   ``succeeded``. ``failed`` ends the task (no automatic repair rounds in the
   first version, spec 6.3). ``inconclusive`` waits for the user.

The Runner never changes task status: it returns a ``TaskOutcome`` that
TaskService commits. It writes the attempt's run record (``model_version``,
``context_ref``: spec 3 "Runner 提交运行记录"), ``model_call`` and
``budget_reminder`` events and the checkpoint content; ToolGateway writes
``tool_action`` events.
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..config import BudgetReminderConfig
from ..contracts import (
    AgentProfile,
    ArtifactInput,
    AssistantStep,
    BudgetReminderRecord,
    CandidateResult,
    Checkpoint,
    ContextBuiltRecord,
    ContextBundle,
    ErrorCode,
    Event,
    EventType,
    ExecutionContext,
    HarnessError,
    ModelCallRecord,
    RemainingBudget,
    ReservationResult,
    StaleLease,
    StepKind,
    Task,
    TaskError,
    TaskOutcome,
    TaskStatus,
    ToolCall,
    ToolOutcome,
    ToolSpec,
    ToolStatus,
    Usage,
    VerificationResult,
    VerificationStatus,
    WaitCondition,
    canonical_json,
    utcnow,
)
from ..interfaces import ArtifactStore, ToolGateway, Verifier
from ..storage.repositories import Repositories
from . import context_sizes
from . import messages as msg
from .budget import BudgetReminders, ModelBudget, ModelReservation, ReminderState
from .models.base import ModelAdapter, ModelError

log = logging.getLogger(__name__)

# Longest tool summary passed back to the model. Gateways should keep
# summaries short and put full output in ArtifactStore; this is a backstop.
MAX_TOOL_TEXT = 8000


@dataclass
class _Run:
    """State of one attempt."""

    context: ContextBundle
    profile: AgentProfile
    execution: ExecutionContext
    messages: list[dict[str, Any]]
    tools: list[ToolSpec]
    reminders: ReminderState = field(default_factory=ReminderState)
    artifact_refs: list[str] = field(default_factory=list)
    usage_refs: list[str] = field(default_factory=list)
    completed_action_ids: list[str] = field(default_factory=list)
    # Model-call steps in this task so far, all attempts included.
    steps: int = 0
    model_failures: int = 0
    model_version: str | None = None

    @property
    def lease(self):
        return self.execution.lease

    def add_artifacts(self, refs: list[str]) -> None:
        for ref in refs:
            if ref not in self.artifact_refs:
                self.artifact_refs.append(ref)


class AgentRunner:
    def __init__(
        self,
        repos: Repositories,
        model: ModelAdapter,
        gateway: ToolGateway,
        verifier: Verifier,
        artifacts: ArtifactStore,
        *,
        reminders: BudgetReminderConfig | None = None,
        model_retries: int = 2,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._repos = repos
        self._model = model
        self._gateway = gateway
        self._verifier = verifier
        self._artifacts = artifacts
        self._budget = ModelBudget(repos.usage)
        self._reminders = BudgetReminders(reminders or BudgetReminderConfig())
        self._model_retries = model_retries
        self._clock = clock
        self._max_output = int(getattr(model, "max_output_tokens", 4096))
        self._pass_max_output = "max_output_tokens" in inspect.signature(model.next_step).parameters

    async def run(
        self,
        context: ContextBundle,
        profile: AgentProfile,
        execution: ExecutionContext,
        *,
        lease_lost: Callable[[], bool] | None = None,
    ) -> TaskOutcome:
        """Run one attempt and return how it ended.

        ``lease_lost`` is the Worker's view of its heartbeat; when it reports
        the lease gone, the Runner raises ``StaleLease`` and returns nothing
        to commit (spec 4.4: the old executor may not commit).
        """
        run = _Run(
            context=context,
            profile=profile,
            execution=execution,
            messages=list(context.messages),
            tools=[t for t in context.tool_specs if t.name != msg.SUBMIT_RESULT],
            artifact_refs=list(context.artifact_refs),
        )
        lease = execution.lease
        task = await self._repos.tasks.get(lease.scope_id, lease.task_id)
        # Step numbers run over the whole task, so a later attempt never
        # reuses an earlier attempt's step IDs (and thus its action keys).
        run.steps = await self._steps_so_far(task)
        context_ref = await self._store_json(task, "context", {"attempt_id": lease.attempt_id, "context": context})
        await self._repos.tasks.record_run(
            lease, self._clock(), model_version=self._model.model, context_ref=context_ref
        )
        while True:
            task = await self._repos.tasks.get(execution.lease.scope_id, execution.lease.task_id)
            stop = self._check(task, run, lease_lost)
            if stop is not None:
                return stop
            remaining = RemainingBudget.for_task(task, self._clock())
            if remaining.model_calls <= 0 or remaining.total_tokens <= 0:
                return self._exhausted(run, remaining)
            await self._remind(task, run, remaining)
            wrap_up = self._reminders.is_wrap_up(remaining)
            step = await self._call_model(task, run, remaining, wrap_up)
            if isinstance(step, TaskOutcome):
                return step
            if step is None:
                continue  # retryable model failure, budget permitting
            if step.kind is StepKind.CANDIDATE_RESULT:
                return await self._finish(task, run, step.candidate)
            outcome = await self._tools(task, run, step, wrap_up)
            if outcome is not None:
                return outcome

    async def _steps_so_far(self, task: Task) -> int:
        """Model-call steps any earlier attempt of this task already used.

        Counted from ``model_call`` events, which the Runner writes for every
        call, failed ones included. Events are paged by ``seq``.
        """
        count, after = 0, 0
        while True:
            page = await self._repos.events.list(task.scope_id, task_id=task.id, after_seq=after, limit=500)
            if not page:
                return count
            count += sum(1 for e in page if e.type is EventType.MODEL_CALL)
            after = page[-1].seq

    # -- round checks ---------------------------------------------------------

    def _check(self, task: Task, run: _Run, lease_lost: Callable[[], bool] | None) -> TaskOutcome | None:
        lease = run.lease
        if (
            task.current_attempt_id != lease.attempt_id
            or task.lease_epoch != lease.lease_epoch
            or (lease_lost is not None and lease_lost())
        ):
            raise StaleLease(
                f"lease {lease.attempt_id}/{lease.lease_epoch} is no longer current", task_id=task.id
            )
        if task.status is TaskStatus.CANCELLING or task.cancel_requested_at is not None:
            return self._outcome(run, TaskStatus.CANCELLED)
        if task.deadline is not None and self._clock() >= task.deadline:
            return self._failed(run, ErrorCode.DEADLINE_EXCEEDED, "the task deadline has passed")
        return None

    def _exhausted(self, run: _Run, remaining: RemainingBudget) -> TaskOutcome:
        used = []
        if remaining.model_calls <= 0:
            used.append("max_model_calls")
        if remaining.total_tokens <= 0:
            used.append("max_total_tokens")
        return self._failed(
            run,
            ErrorCode.BUDGET_EXHAUSTED,
            f"budget used up ({', '.join(used) or 'limits'}) before a result was returned",
        )

    async def _remind(self, task: Task, run: _Run, remaining: RemainingBudget) -> None:
        reminder = self._reminders.check(remaining, task.limits, run.reminders)
        if reminder is None:
            return
        run.messages.append(msg.user(reminder.text, kind="budget_reminder"))
        record = BudgetReminderRecord(tier=reminder.tier, text=reminder.text, remaining=remaining)
        await self._event(task, run, EventType.BUDGET_REMINDER, record.model_dump(mode="json"))

    # -- model call -----------------------------------------------------------

    async def _call_model(
        self, task: Task, run: _Run, remaining: RemainingBudget, wrap_up: bool
    ) -> AssistantStep | TaskOutcome | None:
        tools = [msg.SUBMIT_RESULT_SPEC] if wrap_up else [*run.tools, msg.SUBMIT_RESULT_SPEC]
        context = run.context.model_copy(
            update={"messages": list(run.messages), "tool_specs": tools, "remaining_budget": remaining}
        )
        step_id = _step_id(task.id, run.steps + 1)
        sizes = context_sizes.measure(context.messages, tools)
        input_tokens = sizes.est_tokens
        reservation = await self._budget.reserve(
            task,
            run.lease.attempt_id,
            step_id,
            input_tokens=input_tokens,
            max_output_tokens=self._max_output,
            now=self._clock(),
        )
        if isinstance(reservation, ReservationResult):
            return self._failed(
                run,
                ErrorCode.BUDGET_EXHAUSTED,
                f"no budget for another model call ({', '.join(reservation.exceeded)})",
            )
        run.steps += 1
        built = ContextBuiltRecord(call_id=reservation.call_id, wrap_up=wrap_up, sizes=sizes)
        await self._event(task, run, EventType.CONTEXT_BUILT, built.model_dump(mode="json"))
        input_ref = await self._store_json(
            task,
            "model_input",
            {"step_id": step_id, "model": self._model.model, "wrap_up": wrap_up, "context": context},
        )
        started = time.monotonic()
        try:
            if self._pass_max_output:
                step = await self._model.next_step(context, max_output_tokens=reservation.max_output_tokens)
            else:
                step = await self._model.next_step(context)
        except ModelError as exc:
            return await self._model_failed(task, run, reservation, input_ref, exc, started)
        except StaleLease:
            await self._budget.settle(reservation.call_id, reservation.reserved, self._clock())
            raise
        except Exception as exc:  # noqa: BLE001 - adapter bug or unexpected SDK error
            log.exception("model adapter raised %s on call %s", type(exc).__name__, reservation.call_id)
            wrapped = ModelError(
                f"model adapter error: {type(exc).__name__}: {exc}",
                retryable=False,
                responded=False,
            )
            return await self._model_failed(task, run, reservation, input_ref, wrapped, started)
        latency = int((time.monotonic() - started) * 1000)
        await self._budget.settle(reservation.call_id, step.usage, self._clock())
        run.usage_refs.append(reservation.call_id)
        run.model_failures = 0
        if step.model and step.model != run.model_version:
            # The provider names the exact version it ran (e.g. a dated
            # snapshot behind an alias); keep that on the attempt.
            run.model_version = step.model
            await self._repos.tasks.record_run(run.lease, self._clock(), model_version=step.model)
        output_ref = await self._store_json(task, "model_output", step)
        record = ModelCallRecord(
            call_id=reservation.call_id,
            model=step.model,
            input_ref=input_ref,
            output_ref=output_ref,
            usage=step.usage,
            step_kind=step.kind,
            latency_ms=latency,
        )
        await self._event(task, run, EventType.MODEL_CALL, record.model_dump(mode="json"))
        run.messages.append(_assistant_message(step))
        return step

    async def _model_failed(
        self,
        task: Task,
        run: _Run,
        reservation: ModelReservation,
        input_ref: str,
        exc: ModelError,
        started: float,
    ) -> TaskOutcome | None:
        now = self._clock()
        if exc.usage is not None:
            await self._budget.settle(reservation.call_id, exc.usage, now)
            run.usage_refs.append(reservation.call_id)
        elif exc.details.get("responded") is False:
            # No answer (timeout, dropped connection): the provider may have
            # run and billed the call, so charge what was reserved.
            await self._budget.settle(reservation.call_id, reservation.reserved, now)
            run.usage_refs.append(reservation.call_id)
        else:
            await self._budget.release(reservation.call_id, now)
        output_ref = await self._store_json(
            task,
            "model_output",
            {"error": exc.message, "status_code": exc.status_code, "retryable": exc.retryable},
        )
        record = ModelCallRecord(
            call_id=reservation.call_id,
            model=self._model.model,
            input_ref=input_ref,
            output_ref=output_ref,
            usage=exc.usage or Usage(),
            step_kind=None,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        await self._event(task, run, EventType.MODEL_CALL, record.model_dump(mode="json") | {"error": exc.message})
        run.model_failures += 1
        if exc.retryable and run.model_failures <= self._model_retries:
            log.warning("model call %s failed, retrying: %s", reservation.call_id, exc.message)
            return None
        return self._failed(run, ErrorCode.MODEL_ERROR, exc.message, retryable=exc.retryable)

    # -- tools --------------------------------------------------------------------

    async def _tools(self, task: Task, run: _Run, step: AssistantStep, wrap_up: bool) -> TaskOutcome | None:
        offered = {t.name for t in run.tools} if not wrap_up else set()
        invalid = _invalid_args(step)
        calls = step.tool_calls
        for index, call in enumerate(calls):
            logical_key = _logical_key(run.steps, index)
            call_id = call.native_call_id or logical_key
            problem = None
            if call.tool == msg.SUBMIT_RESULT:
                problem = f"call {msg.SUBMIT_RESULT} alone, after the other tools have finished"
            elif call.native_call_id in invalid:
                problem = invalid[call.native_call_id]
            elif call.tool not in offered:
                problem = (
                    "tools are not available in this turn; return the result with submit_result"
                    if wrap_up
                    else f"unknown tool {call.tool!r}; available: {', '.join(sorted(offered)) or 'none'}"
                )
            if problem is not None:
                run.messages.append(msg.tool_result(call_id, call.tool, problem, is_error=True))
                continue
            keyed = call.model_copy(update={"logical_key": logical_key})
            try:
                outcome = await self._gateway.invoke(run.execution, keyed)
            except StaleLease:
                raise
            except HarnessError as exc:
                if exc.code is ErrorCode.IDEMPOTENCY_CONFLICT:
                    run.messages.append(msg.tool_result(call_id, call.tool, exc.message, is_error=True))
                    continue
                return self._failed(run, exc.code, f"tool {call.tool} failed: {exc.message}", retryable=exc.retryable)
            run.add_artifacts(outcome.artifact_refs)
            if outcome.status in (ToolStatus.NEEDS_APPROVAL, ToolStatus.UNKNOWN):
                return await self._wait(task, run, outcome, [c.native_call_id for c in calls[index + 1 :]])
            if outcome.action_id and outcome.status is ToolStatus.SUCCEEDED:
                run.completed_action_ids.append(outcome.action_id)
            run.messages.append(
                msg.tool_result(
                    call_id,
                    call.tool,
                    _tool_text(outcome),
                    is_error=outcome.status is not ToolStatus.SUCCEEDED,
                )
            )
        return None

    async def _wait(
        self, task: Task, run: _Run, outcome: ToolOutcome, skipped: list[str | None]
    ) -> TaskOutcome:
        """Save where the attempt stopped and leave it ``waiting`` (spec 5.2)."""
        condition = (
            WaitCondition.APPROVAL if outcome.status is ToolStatus.NEEDS_APPROVAL else WaitCondition.RECONCILIATION
        )
        checkpoint = Checkpoint(
            stage=f"waiting_{condition}",
            step_id=_step_id(task.id, run.steps),
            completed_action_ids=list(run.completed_action_ids),
            artifact_refs=list(run.artifact_refs),
            pending_action_ids=[outcome.action_id] if outcome.action_id else [],
        )
        ref = await self._store_json(
            task,
            "checkpoint",
            {"checkpoint": checkpoint, "messages": run.messages, "unanswered_tool_calls": skipped},
        )
        return self._outcome(run, TaskStatus.WAITING, wait_condition=condition, checkpoint_ref=ref)

    # -- result -----------------------------------------------------------------

    async def _finish(self, task: Task, run: _Run, candidate: CandidateResult | None) -> TaskOutcome:
        assert candidate is not None
        run.add_artifacts(list(candidate.artifact_refs.values()))
        verification: VerificationResult = await self._verifier.verify(candidate, task)
        if verification.status is VerificationStatus.PASSED:
            return self._outcome(
                run,
                TaskStatus.SUCCEEDED,
                result_ref=verification.result_ref,
                verification_ref=verification.id,
            )
        problems = [c.id + (f": {c.detail}" if c.detail else "") for c in verification.checks if c.status is not VerificationStatus.PASSED]
        if verification.status is VerificationStatus.FAILED:
            # First version: no automatic repair rounds (spec 6.3).
            return self._failed(
                run,
                ErrorCode.VERIFICATION_FAILED,
                "result did not pass verification: " + ("; ".join(problems) or "failed"),
                verification_ref=verification.id,
            )
        # inconclusive -> waiting / input (decided): never succeeded; the user
        # confirms (spec 6.3 "待确认结果"). result_ref stays unset: the spec
        # writes it only when checks pass.
        # The candidate's artifacts are in artifact_refs.
        return self._outcome(
            run,
            TaskStatus.WAITING,
            wait_condition=WaitCondition.INPUT,
            verification_ref=verification.id,
        )

    # -- helpers -----------------------------------------------------------------

    def _outcome(self, run: _Run, status: TaskStatus, **fields: Any) -> TaskOutcome:
        return TaskOutcome(
            status=status,
            usage_refs=list(run.usage_refs),
            artifact_refs=list(run.artifact_refs),
            **fields,
        )

    def _failed(self, run: _Run, code: ErrorCode, message: str, *, retryable: bool = False, **fields: Any) -> TaskOutcome:
        return self._outcome(
            run,
            TaskStatus.FAILED,
            error=TaskError(code=code, message=message, retryable=retryable),
            **fields,
        )

    async def _event(self, task: Task, run: _Run, kind: EventType, payload: dict[str, Any]) -> None:
        await self._repos.events.append(
            Event(
                scope_id=task.scope_id,
                task_id=task.id,
                attempt_id=run.lease.attempt_id,
                type=kind,
                payload=payload,
                occurred_at=self._clock(),
            )
        )

    async def _store_json(self, task: Task, name: str, value: Any) -> str:
        """Trajectory content. Stored without ``task_id`` so it is not listed
        among the task's work artifacts; events point to it."""
        ref = await self._artifacts.put(
            ArtifactInput(
                scope_id=task.scope_id,
                task_id=None,
                name=f"{task.id}/{name}",
                media_type="application/json",
                content=canonical_json(value).encode("utf-8"),
            )
        )
        return ref.id


def _step_id(task_id: str, step: int) -> str:
    """Durable step ID: task plus step number, no attempt ID.

    Also the ``call_id`` of the step's model-usage row, which is unique per
    task because step numbers continue across attempts.
    """
    return f"{task_id}.step-{step}"


def _logical_key(step: int, index: int) -> str:
    """Action key for the ``index``-th tool call of ``step`` (spec 5.2).

    Unique within the task (actions dedupe on ``(task_id, logical_key)``)
    and without the attempt ID, as spec 3 requires: "重试沿用原动作键，不用新
    Attempt ID 代替". Limit in P0: a later attempt continues the numbering
    instead of replaying a step, so the same key comes back only when a
    resumed attempt re-issues a step from its checkpoint. That replay, with
    the step number taken from the checkpoint, is P1.
    """
    return f"step-{step}.{index}"


def _assistant_message(step: AssistantStep) -> dict[str, Any]:
    if step.message is not None:
        return step.message
    if step.kind is StepKind.CANDIDATE_RESULT and step.candidate is not None:
        return msg.Message(role="assistant", content=step.candidate.summary).to_dict()
    return msg.Message(
        role="assistant",
        tool_calls=[
            msg.ToolCallPart(id=c.native_call_id or f"call_{i}", name=c.tool, args=c.args)
            for i, c in enumerate(step.tool_calls)
        ],
    ).to_dict()


def _invalid_args(step: AssistantStep) -> dict[str | None, str]:
    if not step.message:
        return {}
    parsed = msg.Message.model_validate(step.message)
    return {c.id: c.invalid for c in parsed.tool_calls if c.invalid}


def _tool_text(outcome: ToolOutcome) -> str:
    parts = [f"status: {outcome.status}"]
    if outcome.summary:
        parts.append(outcome.summary)
    if outcome.error is not None:
        parts.append(f"error ({outcome.error.code}): {outcome.error.message}")
    if outcome.artifact_refs:
        parts.append("artifacts: " + ", ".join(outcome.artifact_refs))
    if outcome.action_id:
        parts.append(f"action: {outcome.action_id}")
    text = "\n".join(parts)
    return text if len(text) <= MAX_TOOL_TEXT else text[:MAX_TOOL_TEXT] + "\n[truncated]"
