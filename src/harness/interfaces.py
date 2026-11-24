"""Interfaces between harness modules (spec section 5.2).

Each module implements one of these Protocols and uses the other modules only
through them, so modules can be written in parallel and tested with fakes.
Signatures follow the spec; where one differs, its docstring says why.

P0 modules first; the P1/P2 entry points (SubscriptionRepository,
OutboxRepository, MemoryStore, DeliveryService, ProactiveController) are here
as types so their implementations can be written and faked independently.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from .contracts import (
    ActionRef,
    AdmissionDecision,
    Actor,
    AgentProfile,
    AgentRole,
    ArtifactContent,
    ArtifactInput,
    ArtifactRef,
    AssistantStep,
    DeliveryOutcome,
    Event,
    MemoryKind,
    MemoryRecord,
    MemoryStatus,
    NoticeRequest,
    NoticeReservation,
    OutboxItem,
    Subscription,
    SubscriptionStatus,
    AttemptLease,
    AuthorizedCall,
    CandidateResult,
    Checkpoint,
    ContextBundle,
    ExecutionContext,
    ReconcileResult,
    RunLane,
    Task,
    TaskOrigin,
    TaskOutcome,
    TaskRef,
    TaskRequest,
    ToolCall,
    ToolOutcome,
    ToolResult,
    ToolSpec,
    VerificationResult,
)


class TaskService(Protocol):
    """The only writer of task state (spec 2). Built on storage.TaskRepository."""

    async def create(self, request: TaskRequest, actor: Actor, origin: TaskOrigin) -> Task:
        """Spec: create(request, actor). ``origin`` is added because both the
        API (always main) and ProactiveController (proactive) create tasks
        here, and the role must come from the caller, not from the request."""

    async def get(self, scope_id: str, task_id: str) -> Task:
        """Not in spec 5.2; needed by GET /tasks/{id}."""

    async def claim(self, worker_id: str, allowed_lanes: list[RunLane]) -> AttemptLease | None: ...

    async def heartbeat(self, lease: AttemptLease) -> Task:
        """Spec returns None. Returning the task lets the worker see a cancel
        request without a second read."""

    async def checkpoint(self, lease: AttemptLease, state: Checkpoint) -> None:
        """Store ``state`` in ArtifactStore, then point the task at it (P1)."""

    async def commit(self, lease: AttemptLease, outcome: TaskOutcome) -> Task: ...

    async def request_cancel(self, scope_id: str, task_id: str, reason: str) -> Task:
        """Not in spec 5.2; needed by POST /tasks/{id}/cancel."""


class AgentRegistry(Protocol):
    def resolve(self, role: AgentRole, version: str) -> AgentProfile:
        """Raise NotFound for an unknown role or version."""


class ContextBuilder(Protocol):
    async def build(self, task: Task, profile: AgentProfile, lease: AttemptLease) -> ContextBundle:
        """Order material as goal and constraints, progress and pending
        actions, artifacts, memories, needed history; filter by grants first,
        then cut to the token budget (spec 6.1). Fills remaining_budget."""


class ModelAdapter(Protocol):
    async def next_step(self, context: ContextBundle) -> AssistantStep:
        """One model call. Reports real usage in ``AssistantStep.usage``; the
        Runner, not the adapter, charges the budget."""


class AgentRunner(Protocol):
    async def run(
        self,
        context: ContextBundle,
        profile: AgentProfile,
        execution: ExecutionContext,
    ) -> TaskOutcome:
        """Spec passes ``lease``. ExecutionContext holds the lease together
        with the actor and grants, which the Runner must give to ToolGateway."""


class ToolGateway(Protocol):
    async def invoke(self, execution: ExecutionContext, call: ToolCall) -> ToolOutcome:
        """The steps of spec 6.2. ``call.logical_key`` is set by the Runner and
        never changed here. Registering the action and reserving the tool
        budget are one step (``ActionRepository.register`` with ``reserve``)."""


class Connector(Protocol):
    spec: ToolSpec

    async def execute(self, call: AuthorizedCall) -> ToolResult: ...

    async def reconcile(self, action: ActionRef) -> ReconcileResult:
        """Return status "unsupported" unless the service can be asked
        reliably whether the call happened (spec 7 step 3)."""


class ArtifactStore(Protocol):
    async def put(self, artifact: ArtifactInput) -> ArtifactRef:
        """Write the content first, then the artifacts row."""

    async def read(
        self,
        reference: ArtifactRef | str,
        actor: Actor,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> ArtifactContent:
        """Check the actor's scope before reading. Spec: read(reference,
        actor); ``offset`` / ``length`` are the paging GET /artifacts allows."""


class Verifier(Protocol):
    async def verify(self, result: CandidateResult, task: Task) -> VerificationResult:
        """Run the capability's checkers against the acceptance fixed at task
        start, and store the result through VerificationRepository."""


class SubscriptionRepository(Protocol):
    """Standing goals and what each one already handled (P2).

    `record_event` is unique on (subscription_id, event_id): a redelivered
    event is a no-op, and one event may still produce tasks for other
    subscriptions. Revoking goes through `update`; it stops new triggers
    without touching tasks already created.
    """

    async def create(self, subscription: Subscription) -> Subscription: ...

    async def get(self, scope_id: str, subscription_id: str) -> Subscription:
        """Raise NotFound when it does not exist in this scope."""

    async def list_for_scope(
        self, scope_id: str, *, status: SubscriptionStatus | None = None
    ) -> list[Subscription]: ...

    async def update(self, subscription: Subscription) -> Subscription:
        """Bump `version`; reject a write based on an older version."""

    async def record_event(self, record: AdmissionDecision) -> AdmissionDecision:
        """Insert, or return the earlier row for the same (subscription, event).

        `AdmissionDecision` already carries the policy version and the later
        training label, so there is no separate SubscriptionEvent type.
        """

    async def consumed(self, subscription_id: str, event_id: str) -> AdmissionDecision | None: ...


class OutboxRepository(Protocol):
    """Queued deliveries (P1/P2). One row per (scope_id, dedup_key)."""

    async def enqueue(self, item: OutboxItem) -> OutboxItem:
        """Return the existing row when the dedup key was already used."""

    async def get(self, scope_id: str, item_id: str) -> OutboxItem: ...

    async def list_due(self, now: datetime, *, limit: int = 50) -> list[OutboxItem]:
        """Pending (or retryable failed) items that have not expired, oldest first."""

    async def update(self, item: OutboxItem) -> OutboxItem: ...

    async def cancel_for_task(self, scope_id: str, task_id: str, reason: str) -> int:
        """Cancel pending items of one task; returns how many changed."""


class MemoryStore(Protocol):
    """Scope-owned, versioned memory records (spec 6).

    Full text lives in ArtifactStore. Model inferences are CANDIDATE and may
    not be presented as confirmed facts; superseding keeps the old row.
    A record is current when ended_at is unset and expires_at is unset or later than now.
    """

    async def put(self, record: MemoryRecord) -> MemoryRecord: ...

    async def get(self, scope_id: str, memory_id: str) -> MemoryRecord:
        """Read a record in this scope, including ended and expired versions."""

    async def search(
        self, scope_id: str, query: str, *, now: datetime,
        kinds: list[MemoryKind] | None = None, limit: int = 20,
    ) -> list[MemoryRecord]:
        """Return current records matching any whitespace-separated query term.

        A term matches a summary substring or a whole tag, case-insensitively.
        Sort by matched term count, then updated_at, both descending.
        Raise ValueError if the query contains no terms.
        """

    async def current(
        self, scope_id: str, *, now: datetime, kinds: list[MemoryKind],
        subject_ref: str | None = None, limit: int = 50,
    ) -> list[MemoryRecord]:
        """Return current records of these kinds, newest updated_at first.
        When supplied, subject_ref limits results to that subject.
        """

    async def list(
        self, scope_id: str, *, now: datetime, kinds: list[MemoryKind] | None = None,
        statuses: list[MemoryStatus] | None = None, include_ended: bool = False,
        end_reasons: list[str] | None = None, source_ref: str | None = None, limit: int = 200,
    ) -> list[MemoryRecord]:
        """List records for the user API and deduplication, newest updated_at first.
        Only current records qualify unless include_ended is set. Omitted filters impose no restriction.
        """

    async def supersede(
        self, scope_id: str, memory_id: str, replacement: MemoryRecord, now: datetime,
    ) -> MemoryRecord:
        """Atomically end the old record at now as superseded and return its replacement.
        Set supersedes_id to memory_id and increment the old version by one for the new record.
        Raise InvalidTransition unless the old record is current in this scope.
        """

    async def retract(
        self, scope_id: str, memory_id: str, reason: Literal["retracted", "rejected"], now: datetime,
    ) -> MemoryRecord:
        """Set only ended_at and end_reason on the record and return it, retaining its content.
        Raise InvalidTransition unless the record is current in this scope.
        """


class DeliveryService(Protocol):
    """Decide whether to tell the user, then send (spec 5, 交付)."""

    async def reserve_notice(self, request: NoticeRequest) -> NoticeReservation:
        """Atomically check the user's notice quota and the cross-channel dedup
        key. A suppressed request must not reach the outbox."""

    async def dispatch(self, item: OutboxItem) -> DeliveryOutcome:
        """Re-check cancel, expiry and grants, send through the channel, store
        the receipt. Never record queued as delivered."""


class ProactiveController(Protocol):
    """Rules-only entry for events and timers (P2).

    It does not call a model: it checks the subscription, dedup key, grants and
    background budget, records a decision, and creates a proactive task through
    TaskService only when the decision is to admit.
    """

    async def on_event(self, event: Event) -> list[TaskRef]: ...

