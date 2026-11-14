"""Interfaces between harness modules (spec section 5.2).

Each module implements one of these Protocols and uses the other modules only
through them, so modules can be written in parallel and tested with fakes.
Signatures follow the spec; where one differs, its docstring says why.

Not here yet: ProactiveController and DeliveryService (P2). Their request
types (NoticeRequest, OutboxItem, ...) will be added together with them.
"""

from __future__ import annotations

from typing import Protocol

from .contracts import (
    ActionRef,
    Actor,
    AgentProfile,
    AgentRole,
    ArtifactContent,
    ArtifactInput,
    ArtifactRef,
    AssistantStep,
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
