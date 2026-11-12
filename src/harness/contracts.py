"""Shared types for the DaraOS harness runtime.

Every harness module (tasks, context, runtime, tools, results, delivery,
storage) takes its data shapes from this file, so a field change here is a
change for all of them.

Names follow the spec "Harness 架构与开发说明" (2026-11-10), sections 3 and
5.2. Fields the spec does not list are marked "DaraOS addition" with the
reason; most come from review comments on that spec.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Stable JSON: sorted keys, no whitespace, UTF-8 kept as is."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def canonical_hash(value: Any) -> str:
    """request_hash for task requests and tool arguments (spec 3.2, 6.2)."""
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


_VERSIONED_REF = re.compile(r"^[a-z][a-z0-9_.-]*@[A-Za-z0-9_.-]+$")


def split_versioned_ref(ref: str) -> tuple[str, str]:
    """'document_draft@1' -> ('document_draft', '1')."""
    if not _VERSIONED_REF.match(ref):
        raise ValueError(f"expected '<name>@<version>', got {ref!r}")
    name, version = ref.split("@", 1)
    return name, version


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# ---------------------------------------------------------------------------
# enums
# ---------------------------------------------------------------------------


class AgentRole(StrEnum):
    MAIN = "main"
    PROACTIVE = "proactive"


class RunLane(StrEnum):
    FOREGROUND = "foreground"
    BACKGROUND = "background"


class TriggerType(StrEnum):
    USER = "user"
    EVENT = "event"
    SCHEDULE = "schedule"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = frozenset({TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED})


class WaitCondition(StrEnum):
    INPUT = "input"
    APPROVAL = "approval"
    DEPENDENCY = "dependency"
    RECONCILIATION = "reconciliation"


class ToolStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    NEEDS_APPROVAL = "needs_approval"
    UNKNOWN = "unknown"


class ActionStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class VerificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class StepKind(StrEnum):
    TOOL_CALLS = "tool_calls"
    CANDIDATE_RESULT = "candidate_result"


class EventType(StrEnum):
    TASK_CREATED = "task_created"
    TASK_STATUS_CHANGED = "task_status_changed"
    TOOL_ACTION = "tool_action"
    VERIFICATION_RECORDED = "verification_recorded"
    BACKGROUND_RESULT_READY = "background_result_ready"
    EXTERNAL = "external"
    # DaraOS additions, see ModelCallRecord and BudgetReminderRecord
    MODEL_CALL = "model_call"
    BUDGET_REMINDER = "budget_reminder"


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    VERSION_CONFLICT = "version_conflict"
    LIMITS_EXCEEDED = "limits_exceeded"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    VERIFICATION_FAILED = "verification_failed"
    TOOL_ERROR = "tool_error"
    MODEL_ERROR = "model_error"
    LEASE_LOST = "lease_lost"
    INVALID_TRANSITION = "invalid_transition"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


class UsageKind(StrEnum):
    MODEL = "model"
    TOOL = "tool"


class ReservationStatus(StrEnum):
    RESERVED = "reserved"
    SETTLED = "settled"
    RELEASED = "released"


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class HarnessError(Exception):
    code: ErrorCode = ErrorCode.INTERNAL
    retryable: bool = False

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class InvalidTransition(HarnessError):
    code = ErrorCode.INVALID_TRANSITION


class IdempotencyConflict(HarnessError):
    """Same idempotency key, different request content (HTTP 409)."""

    code = ErrorCode.IDEMPOTENCY_CONFLICT


class ActionConflict(HarnessError):
    """Same (task_id, logical_key), different arguments (spec 6.2 step 1)."""

    code = ErrorCode.IDEMPOTENCY_CONFLICT


class StaleLease(HarnessError):
    """The caller no longer holds the task's lease (spec 3.2, 4.4)."""

    code = ErrorCode.LEASE_LOST


class NotFound(HarnessError):
    code = ErrorCode.NOT_FOUND


class VerificationRequired(HarnessError):
    """A task cannot become succeeded without a passed verification."""

    code = ErrorCode.VERIFICATION_FAILED


class LimitsExceeded(HarnessError):
    """Requested limits are above the deployment caps (HTTP 400)."""

    code = ErrorCode.LIMITS_EXCEEDED


# ---------------------------------------------------------------------------
# task state machine (spec 4.3)
# ---------------------------------------------------------------------------

TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.SUCCEEDED,
            TaskStatus.QUEUED,  # only when a safe retry is allowed
            TaskStatus.WAITING,
            TaskStatus.FAILED,
            TaskStatus.CANCELLING,
        }
    ),
    TaskStatus.WAITING: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.CANCELLING: frozenset({TaskStatus.CANCELLED}),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    return target in TASK_TRANSITIONS[current]


def assert_transition(current: TaskStatus, target: TaskStatus) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(
            f"task cannot move from {current} to {target}",
            current=str(current),
            target=str(target),
        )


# ---------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------


class Limits(_Model):
    """Per-task limits. Spec column `tasks.budget`, API field `limits`.

    Defaults are the example values from spec 5.1. All four accumulate over
    the whole task; a new attempt does not reset them (spec 6.2).
    """

    timeout_sec: int = Field(1800, gt=0)
    max_model_calls: int = Field(20, gt=0)
    max_tool_calls: int = Field(50, gt=0)
    max_total_tokens: int = Field(100_000, gt=0)


class Usage(_Model):
    model_calls: int = Field(0, ge=0)
    tool_calls: int = Field(0, ge=0)
    input_tokens: int = Field(0, ge=0)
    output_tokens: int = Field(0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            model_calls=self.model_calls + other.model_calls,
            tool_calls=self.tool_calls + other.tool_calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )

    def exceeded(self, limits: Limits) -> list[str]:
        """Names of the count limits this usage is above."""
        over = []
        if self.model_calls > limits.max_model_calls:
            over.append("max_model_calls")
        if self.tool_calls > limits.max_tool_calls:
            over.append("max_tool_calls")
        if self.total_tokens > limits.max_total_tokens:
            over.append("max_total_tokens")
        return over


class RemainingBudget(_Model):
    """`ContextBundle.remaining_budget` (spec 5.2).

    `fraction` is the tightest of the four ratios, from 1.0 (nothing used) to
    0.0 (one limit used up). The budget reminder picks its tier from it.
    """

    model_calls: int
    tool_calls: int
    total_tokens: int
    seconds: float
    fraction: float = Field(ge=0.0, le=1.0)

    @classmethod
    def compute(cls, limits: Limits, usage: Usage, elapsed_sec: float) -> RemainingBudget:
        model_calls = max(limits.max_model_calls - usage.model_calls, 0)
        tool_calls = max(limits.max_tool_calls - usage.tool_calls, 0)
        tokens = max(limits.max_total_tokens - usage.total_tokens, 0)
        seconds = max(limits.timeout_sec - elapsed_sec, 0.0)
        fraction = min(
            model_calls / limits.max_model_calls,
            tool_calls / limits.max_tool_calls,
            tokens / limits.max_total_tokens,
            seconds / limits.timeout_sec,
        )
        return cls(
            model_calls=model_calls,
            tool_calls=tool_calls,
            total_tokens=tokens,
            seconds=seconds,
            fraction=fraction,
        )


class UsageReservation(_Model):
    """One reserved-then-settled charge, keyed by call_id (spec 6.2).

    DaraOS addition: the spec asks for idempotent accounting per call ID but
    does not name a table; this is the row of `usage_records`.
    """

    call_id: str
    task_id: str
    attempt_id: str
    kind: UsageKind
    reserved: Usage
    settled: Usage | None = None
    status: ReservationStatus = ReservationStatus.RESERVED
    created_at: datetime = Field(default_factory=utcnow)
    settled_at: datetime | None = None


class ReservationResult(_Model):
    granted: bool
    reservation: UsageReservation | None = None
    exceeded: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# identity and requests
# ---------------------------------------------------------------------------


class Actor(_Model):
    """Who is acting, taken from authentication, never from the request body."""

    actor_id: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)


class CheckSpec(BaseModel):
    """One acceptance check, e.g. {"id": "required_sections", "sections": [...]}.

    Extra keys are the checker's parameters, so they are allowed here.
    """

    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)


class Acceptance(_Model):
    """Fixed when the task starts (spec 6.3)."""

    required_artifacts: list[str] = Field(default_factory=list)
    checks: list[CheckSpec] = Field(default_factory=list)
    required_actions: list[str] = Field(default_factory=list)


class TaskRequest(_Model):
    """What a caller may ask for (POST /tasks body plus Idempotency-Key).

    Role, lane and trigger are deliberately absent: they come from TaskOrigin,
    which only trusted entry points build (spec 3.1).
    """

    request_key: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1)
    input_refs: list[str] = Field(default_factory=list)
    capability_ref: str
    acceptance: Acceptance = Field(default_factory=Acceptance)
    limits: Limits = Field(default_factory=Limits)
    prefer_background: bool = False
    conversation_id: str | None = None
    subject_ref: str | None = None
    source_task_id: str | None = None

    @model_validator(mode="after")
    def _check_refs(self) -> TaskRequest:
        split_versioned_ref(self.capability_ref)
        return self

    def request_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json", exclude={"request_key"}))


def _check_role_fields(
    role: AgentRole, lane: RunLane, subscription_id: str | None, profile_ref: str
) -> None:
    profile_role, _ = split_versioned_ref(profile_ref)
    if profile_role != role:
        raise ValueError(f"agent_profile_ref {profile_ref!r} does not match role {role}")
    if role is AgentRole.PROACTIVE:
        if lane is not RunLane.BACKGROUND:
            raise ValueError("proactive tasks run only on the background lane")
        if not subscription_id:
            raise ValueError("proactive tasks need a subscription_id")


class TaskOrigin(_Model):
    """Set by trusted code: the API entry (always main) or ProactiveController."""

    agent_role: AgentRole
    agent_profile_ref: str
    run_lane: RunLane
    trigger_type: TriggerType
    trigger_ref: str | None = None
    subscription_id: str | None = None
    priority: int = 0

    @model_validator(mode="after")
    def _check(self) -> TaskOrigin:
        _check_role_fields(self.agent_role, self.run_lane, self.subscription_id, self.agent_profile_ref)
        return self


class TaskError(_Model):
    code: ErrorCode
    message: str
    retryable: bool = False


# ---------------------------------------------------------------------------
# tasks and attempts (spec 3, 3.1)
# ---------------------------------------------------------------------------


class Task(_Model):
    id: str
    scope_id: str
    actor_id: str
    request_key: str
    request_hash: str
    goal: str
    input_refs: list[str] = Field(default_factory=list)
    capability_ref: str
    acceptance: Acceptance = Field(default_factory=Acceptance)
    limits: Limits = Field(default_factory=Limits)
    usage: Usage = Field(default_factory=Usage)
    status: TaskStatus = TaskStatus.QUEUED
    wait_condition: WaitCondition | None = None
    priority: int = 0
    deadline: datetime | None = None
    result_ref: str | None = None
    checkpoint_ref: str | None = None
    error: TaskError | None = None
    version: int = Field(1, ge=1)
    # DaraOS addition: the current lease generation and attempt, copied from
    # attempts, so a stale worker is rejected by checking one row.
    lease_epoch: int = Field(0, ge=0)
    current_attempt_id: str | None = None
    cancel_requested_at: datetime | None = None
    cancel_reason: str | None = None

    agent_role: AgentRole
    agent_profile_ref: str
    run_lane: RunLane
    trigger_type: TriggerType
    trigger_ref: str | None = None
    subscription_id: str | None = None
    conversation_id: str | None = None
    subject_ref: str | None = None
    source_task_id: str | None = None
    result_expires_at: datetime | None = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _check(self) -> Task:
        _check_role_fields(self.agent_role, self.run_lane, self.subscription_id, self.agent_profile_ref)
        split_versioned_ref(self.capability_ref)
        if (self.status is TaskStatus.WAITING) != (self.wait_condition is not None):
            raise ValueError("wait_condition is set exactly when status is waiting")
        return self

    @classmethod
    def new(
        cls,
        request: TaskRequest,
        actor: Actor,
        origin: TaskOrigin,
        *,
        now: datetime | None = None,
    ) -> Task:
        now = now or utcnow()
        return cls(
            id=new_id("task"),
            scope_id=actor.scope_id,
            actor_id=actor.actor_id,
            request_key=request.request_key,
            request_hash=request.request_hash(),
            goal=request.goal,
            input_refs=list(request.input_refs),
            capability_ref=request.capability_ref,
            acceptance=request.acceptance,
            limits=request.limits,
            priority=origin.priority,
            agent_role=origin.agent_role,
            agent_profile_ref=origin.agent_profile_ref,
            run_lane=origin.run_lane,
            trigger_type=origin.trigger_type,
            trigger_ref=origin.trigger_ref,
            subscription_id=origin.subscription_id,
            conversation_id=request.conversation_id,
            subject_ref=request.subject_ref,
            source_task_id=request.source_task_id,
            created_at=now,
            updated_at=now,
        )


class Attempt(_Model):
    id: str
    task_id: str
    worker_id: str
    lease_epoch: int = Field(ge=1)
    lease_expires_at: datetime
    heartbeat_at: datetime
    started_at: datetime
    ended_at: datetime | None = None
    agent_profile_version: str | None = None
    model_version: str | None = None
    capability_version: str | None = None
    context_ref: str | None = None
    exit_reason: str | None = None


class AttemptLease(_Model):
    task_id: str
    attempt_id: str
    scope_id: str
    worker_id: str
    lease_epoch: int = Field(ge=1)
    lease_expires_at: datetime
    deadline: datetime | None = None


class ExecutionContext(_Model):
    """What ToolGateway needs to authorise a call. Credentials are not here."""

    lease: AttemptLease
    actor: Actor
    agent_role: AgentRole
    run_lane: RunLane
    grant_refs: list[str] = Field(default_factory=list)


class Checkpoint(_Model):
    """Business checkpoint (spec 4.4)."""

    stage: str
    step_id: str
    completed_action_ids: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    pending_action_ids: list[str] = Field(default_factory=list)
    environment_ref: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# events (spec 3)
# ---------------------------------------------------------------------------


class Event(_Model):
    id: str = Field(default_factory=lambda: new_id("evt"))
    # Assigned by the repository, increasing; SSE uses it as Last-Event-ID.
    seq: int = 0
    scope_id: str
    task_id: str | None = None
    attempt_id: str | None = None
    type: EventType
    source: str = "harness"
    source_event_id: str | None = None
    subject_ref: str | None = None
    # Small JSON only (a status change, a reminder). Large content goes to
    # ArtifactStore and is referenced by payload_ref.
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_ref: str | None = None
    occurred_at: datetime = Field(default_factory=utcnow)
    received_at: datetime = Field(default_factory=utcnow)
    processed_at: datetime | None = None


class ModelCallRecord(_Model):
    """Payload of a `model_call` event.

    DaraOS addition (review comment 9): with the exact model input and output
    stored per call, a trajectory can be exported and turned into training
    data later without changing the tables.
    """

    call_id: str
    model: str
    input_ref: str
    output_ref: str
    usage: Usage
    step_kind: StepKind | None = None
    latency_ms: int | None = None


class BudgetReminderRecord(_Model):
    """Payload of a `budget_reminder` event (DaraOS addition, review comment 6)."""

    tier: str
    text: str
    remaining: RemainingBudget


# ---------------------------------------------------------------------------
# tools and actions (spec 3, 5.2, 6.2)
# ---------------------------------------------------------------------------


class ToolSpec(_Model):
    """Declared once per tool (spec 7 step 2)."""

    name: str = Field(min_length=1)
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    read_only: bool
    side_effects: bool
    idempotent: bool = False
    supports_reconcile: bool = False

    @model_validator(mode="after")
    def _check(self) -> ToolSpec:
        if self.read_only and self.side_effects:
            raise ValueError("a read-only tool cannot have side effects")
        return self


class ToolCall(_Model):
    """A tool request. The model proposes tool and args only; the Runner sets
    logical_key from the durable step ID (spec 5.2)."""

    tool: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    logical_key: str | None = None
    # Provider-side tool call ID, kept so the adapter can pair results.
    native_call_id: str | None = None

    def request_hash(self) -> str:
        return canonical_hash({"tool": self.tool, "args": self.args})


class ToolOutcome(_Model):
    # None when the call was refused before it was registered.
    action_id: str | None = None
    status: ToolStatus
    summary: str = ""
    artifact_refs: list[str] = Field(default_factory=list)
    receipt_ref: str | None = None
    error: TaskError | None = None


class Action(_Model):
    id: str = Field(default_factory=lambda: new_id("act"))
    task_id: str
    attempt_id: str
    logical_key: str
    tool: str
    request_hash: str
    args_ref: str | None = None
    # DaraOS addition (review comment 7): read-only calls can be replayed by
    # the Verifier to check that numbers in a conclusion match the source.
    read_only: bool
    grant_ref: str | None = None
    approval_ref: str | None = None
    status: ActionStatus = ActionStatus.PLANNED
    result_ref: str | None = None
    receipt_ref: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# artifacts and verification (spec 3, 6.3)
# ---------------------------------------------------------------------------


class ArtifactInput(_Model):
    scope_id: str
    task_id: str | None = None
    name: str | None = None
    media_type: str
    content: bytes
    source_refs: list[str] = Field(default_factory=list)


class Artifact(_Model):
    id: str = Field(default_factory=lambda: new_id("art"))
    scope_id: str
    task_id: str | None = None
    # DaraOS addition: CandidateResult names its artifacts ("draft").
    name: str | None = None
    uri: str
    media_type: str
    content_hash: str
    size_bytes: int = Field(ge=0)
    source_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class CheckResult(_Model):
    id: str
    status: VerificationStatus
    detail: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class VerificationResult(_Model):
    id: str = Field(default_factory=lambda: new_id("ver"))
    task_id: str
    result_ref: str
    checker_version: str
    status: VerificationStatus
    checks: list[CheckResult] = Field(default_factory=list)
    unverified_items: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _check(self) -> VerificationResult:
        if self.status is VerificationStatus.PASSED:
            not_passed = [c.id for c in self.checks if c.status is not VerificationStatus.PASSED]
            if not_passed:
                raise ValueError(f"status is passed but checks did not pass: {not_passed}")
        return self


# ---------------------------------------------------------------------------
# model steps and results (spec 5.2)
# ---------------------------------------------------------------------------


class CandidateResult(_Model):
    """What the model claims is done. Only the Verifier can make it a result."""

    summary: str
    artifact_refs: dict[str, str] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    required_action_receipts: list[str] = Field(default_factory=list)


class AssistantStep(_Model):
    kind: StepKind
    tool_calls: list[ToolCall] = Field(default_factory=list)
    candidate: CandidateResult | None = None
    usage: Usage = Field(default_factory=Usage)
    model: str
    native_ref: str | None = None
    # Provider message as returned, kept for tool-call pairing.
    message: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check(self) -> AssistantStep:
        if self.kind is StepKind.TOOL_CALLS:
            if not self.tool_calls or self.candidate is not None:
                raise ValueError("a tool_calls step has tool calls and no candidate")
        elif self.candidate is None or self.tool_calls:
            raise ValueError("a candidate_result step has a candidate and no tool calls")
        return self


_OUTCOME_TARGETS = frozenset(
    {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.WAITING,
        TaskStatus.QUEUED,
        TaskStatus.CANCELLED,
    }
)


class TaskOutcome(_Model):
    """How one attempt ends; TaskService commits it (spec 5.2)."""

    status: TaskStatus
    result_ref: str | None = None
    wait_condition: WaitCondition | None = None
    error: TaskError | None = None
    verification_ref: str | None = None
    checkpoint_ref: str | None = None
    usage_refs: list[str] = Field(default_factory=list)
    # Kept on failure too: "保存错误分类及已有产物，不伪装成成功" (spec 4.3).
    artifact_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> TaskOutcome:
        if self.status not in _OUTCOME_TARGETS:
            raise ValueError(f"an attempt cannot end in {self.status}")
        if self.status is TaskStatus.SUCCEEDED and not (self.result_ref and self.verification_ref):
            raise ValueError("succeeded needs result_ref and verification_ref")
        if (self.status is TaskStatus.WAITING) != (self.wait_condition is not None):
            raise ValueError("wait_condition is set exactly when status is waiting")
        if self.status is TaskStatus.FAILED and self.error is None:
            raise ValueError("failed needs an error")
        return self


# ---------------------------------------------------------------------------
# agent profiles and context (spec 5.2, 6.1)
# ---------------------------------------------------------------------------

DeliveryMode = Literal["reply", "silent", "digest", "notify", "ask"]


class AgentProfile(_Model):
    role: AgentRole
    version: str = Field(min_length=1)
    instructions_ref: str
    context_policy: dict[str, Any] = Field(default_factory=dict)
    tool_policy_ref: str
    default_lane: RunLane
    default_delivery_mode: DeliveryMode

    @property
    def ref(self) -> str:
        return f"{self.role}@{self.version}"

    @model_validator(mode="after")
    def _check(self) -> AgentProfile:
        if self.role is AgentRole.PROACTIVE and self.default_lane is not RunLane.BACKGROUND:
            raise ValueError("the proactive profile runs on the background lane")
        return self


class InjectedMaterial(_Model):
    """A short pointer to a background result, injected into Main (P2).

    DaraOS addition (review comment 4, "这个很重要"): at most about 100 tokens,
    saying how many results, about what, how important, and their IDs; never
    the conclusion text. The model pulls details with a tool when it needs
    them (review comment 2).
    """

    source_task_id: str
    count: int = Field(ge=1)
    topic: str = Field(min_length=1, max_length=120)
    importance: Literal["low", "medium", "high"]
    ref_ids: list[str] = Field(default_factory=list)


class ContextBundle(_Model):
    task_id: str
    goal: str
    acceptance: Acceptance
    constraints: list[str] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    tool_specs: list[ToolSpec] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    memory_refs: list[str] = Field(default_factory=list)
    checkpoint: Checkpoint | None = None
    remaining_budget: RemainingBudget
    # Empty until P2 hands background results to Main.
    injected_materials: list[InjectedMaterial] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# proactive admission (P2; type only)
# ---------------------------------------------------------------------------


class AdmissionDecision(_Model):
    """One ProactiveController decision on (subscription, event).

    DaraOS addition (review comment 1): policy version, an input snapshot and a
    later label make the decisions usable for training a "worth handling"
    model. Stored in P2 together with subscription_events; no table in P0.
    """

    subscription_id: str
    event_id: str
    decision: Literal["admit", "skip", "defer"]
    reason: str
    policy_version: str
    input_snapshot_ref: str | None = None
    task_id: str | None = None
    decided_at: datetime = Field(default_factory=utcnow)
    backfill_label: Literal["useful", "not_useful", "harmful"] | None = None
    backfilled_at: datetime | None = None


# ---------------------------------------------------------------------------
# API errors (spec 5.1)
# ---------------------------------------------------------------------------


class ErrorResponse(_Model):
    error_code: ErrorCode
    message: str
    retryable: bool = False
    request_id: str

    @classmethod
    def from_error(cls, error: HarnessError, request_id: str) -> ErrorResponse:
        return cls(
            error_code=error.code,
            message=error.message,
            retryable=error.retryable,
            request_id=request_id,
        )
