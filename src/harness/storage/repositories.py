"""Repository interfaces: the only way harness code reads or writes state.

Spec rule (section 2): "任务状态只能由 TaskService 更新，数据库访问统一放在
repositories". Two implementations must behave the same:

* ``storage.memory.InMemoryRepositories`` (this PR), for unit tests and for
  writing Runner / ToolGateway / Verifier before the database exists;
* ``storage.postgres.PgRepositories``, checked by the same test suite in
  ``tests/harness_runtime/unit/test_repositories.py``.

Rules every implementation follows:

* Every read that starts from an ID also takes ``scope_id`` and raises
  ``NotFound`` for another scope (spec 3.2).
* A status change and its ``task_status_changed`` event are written in one
  transaction. Model, tool and network calls never run inside one.
* ``Task.version`` goes up on status changes and cancel requests only.
  Heartbeats and usage settlement do not change it.
* Returned objects are copies; changing them changes nothing stored.

Recovery reclaims expired leases through the same repository.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Protocol

from ..contracts import (
    Action,
    ActionStatus,
    AdmissionDecision,
    Artifact,
    Attempt,
    AttemptLease,
    ErrorCode,
    Event,
    NoticeReservation,
    ReservationResult,
    RunLane,
    Subscription,
    SubscriptionStatus,
    Task,
    TaskError,
    TaskOutcome,
    TaskStatus,
    Usage,
    UsageKind,
    UsageReservation,
    VerificationResult,
)
from ..proactive_types import InboxItem


def round_attempts(statuses: Iterable[str]) -> int:
    """Count one action's chronological events since its last terminal decision."""
    count = 0
    for status in statuses:
        count = 0 if status in ("succeeded", "failed") else count + 1
    return count


@dataclass(frozen=True)
class CreateResult:
    task: Task
    created: bool


@dataclass(frozen=True)
class ActionRegistration:
    """Result of ``ActionRepository.register``.

    * existing action, same arguments: ``action`` set, ``created`` False,
      ``budget`` None (reuse, no new charge);
    * new action, budget refused: ``action`` None, ``budget.granted`` False;
    * new action registered: ``action`` set, ``created`` True.
    """

    action: Action | None
    created: bool
    budget: ReservationResult | None = None


def recovery_status(
    task: Task, now: datetime, max_attempts: int, unsettled: bool,
) -> tuple[TaskStatus, TaskError | None]:
    if task.status is TaskStatus.CANCELLING:
        return TaskStatus.CANCELLED, None
    # Preserve reconciliation even when the task can no longer run.
    if unsettled:
        return TaskStatus.WAITING, None
    if task.deadline is not None and task.deadline <= now:
        return TaskStatus.FAILED, TaskError(
            code=ErrorCode.DEADLINE_EXCEEDED, message="the task deadline has passed",
        )
    if task.lease_epoch >= max_attempts:
        return TaskStatus.FAILED, TaskError(code=ErrorCode.LEASE_LOST, message="maximum attempts reached")
    return TaskStatus.QUEUED, None


class TaskRepository(Protocol):
    async def recent_conversation(self, scope_id: str, conversation_id: str) -> list[Task]: ...

    async def revoke_subscription(self, scope_id: str, subscription_id: str, now: datetime) -> Subscription: ...

    async def create(self, task: Task) -> CreateResult:
        """Insert a task and its ``task_created`` event.

        If ``(scope_id, request_key)`` exists: same ``request_hash`` returns
        the stored task with ``created=False``; a different hash raises
        ``IdempotencyConflict``.
        """

    async def get(self, scope_id: str, task_id: str) -> Task:
        """Raise ``NotFound`` when absent or in another scope."""

    async def claim(
        self,
        worker_id: str,
        lanes: list[RunLane],
        lease_ttl_sec: int,
        now: datetime,
        *,
        background_scope_limit: int = 2,
    ) -> AttemptLease | None:
        """Take one queued task on ``lanes``: highest priority, then oldest.

        Atomically: status -> running, ``lease_epoch`` + 1, new attempt row,
        status event. Two concurrent claims never return the same task.

        The first claim sets ``deadline`` to ``now + limits.timeout_sec``;
        later claims keep it, so time counts over the whole task.
        """

    async def heartbeat(self, lease: AttemptLease, lease_ttl_sec: int, now: datetime) -> Task:
        """Extend the lease; return the task so the worker sees cancel requests.

        Raise ``StaleLease`` if the lease is no longer the task's current one
        or has expired.
        """

    async def checkpoint(self, lease: AttemptLease, checkpoint_ref: str, now: datetime) -> Task:
        """Point the task at a checkpoint already stored in ArtifactStore.

        Raise ``StaleLease`` for an old lease. ``version`` does not change.
        """

    async def reclaim_expired(self, now: datetime, max_attempts: int) -> int:
        """Recover expired or missing leases, parking uncertain side effects before retrying."""

    async def list_reconciliation_candidates(self) -> list[Task]:
        """Cross-scope waiting/reconciliation and dirty cancelled tasks, sorted by scope/id."""

    async def release_reconciliation(self, scope_id: str, task_id: str, now: datetime) -> Task:
        """Requeue a still-waiting reconciliation task only after all side effects settle."""

    async def record_run(
        self,
        lease: AttemptLease,
        now: datetime,
        *,
        model_version: str | None = None,
        context_ref: str | None = None,
    ) -> Attempt:
        """Write the Runner's run record on the current attempt (spec 3:
        "TaskService 管生命周期；Runner 提交运行记录").

        Only the given fields change; ``None`` leaves a field as it is. Raise
        ``StaleLease`` for an old lease. The task row and its ``version`` do
        not change.
        """

    async def get_attempt(self, scope_id: str, attempt_id: str) -> Attempt:
        """Raise ``NotFound`` when absent or the task is in another scope."""

    async def commit(
        self, lease: AttemptLease, outcome: TaskOutcome, now: datetime, *, inbox: InboxItem | None = None
    ) -> Task:
        """End the attempt and move the task to ``outcome.status``.

        If cancellation was accepted first, atomically finish as ``cancelled``
        instead. Keep partial artifacts, settled usage and the checkpoint;
        do not publish the late outcome's result or wait condition.

        Raise ``StaleLease`` for an old lease, ``InvalidTransition`` for a
        move the state machine forbids, and ``VerificationRequired`` when
        ``succeeded`` lacks a passed verification of ``outcome.result_ref``.
        """

    async def request_cancel(self, scope_id: str, task_id: str, reason: str, now: datetime) -> Task:
        """Idempotent. queued/waiting -> cancelled; running -> cancelling;
        cancelling and terminal states are returned unchanged."""


class EventRepository(Protocol):
    async def append(self, event: Event) -> Event:
        """Store and return the event with ``seq`` set.

        With ``source_event_id`` set, ``(scope_id, source, source_event_id)``
        is unique: a repeat returns the stored event.
        """

    async def list(
        self,
        scope_id: str,
        task_id: str | None = None,
        after_seq: int = 0,
        limit: int = 100,
    ) -> list[Event]:
        """Events with ``seq > after_seq`` in ascending order."""


class ActionRepository(Protocol):
    async def register(self, action: Action, reserve: Usage | None = None) -> ActionRegistration:
        """Register a planned tool call, unique on ``(task_id, logical_key)``.

        A different ``request_hash`` under the same key raises
        ``ActionConflict``. With ``reserve``, the budget reservation (call ID =
        action ID) and the insert are one transaction (spec 6.2 step 3).
        """

    async def get(self, task_id: str, logical_key: str) -> Action | None: ...

    async def update(
        self,
        action_id: str,
        status: ActionStatus,
        *,
        result_ref: str | None = None,
        receipt_ref: str | None = None,
        now: datetime,
    ) -> Action: ...

    async def list_for_task(self, task_id: str) -> list[Action]: ...

    async def list_unsettled(self, task_id: str) -> list[Action]: ...

    async def decide(
        self, scope_id: str, task_id: str, action_id: str, status: ActionStatus, *,
        result_ref: str | None, receipt_ref: str | None, usage: Usage, event: Event, now: datetime, expected_attempt: int,
    ) -> Action | None:
        """Atomically decide an unsettled action, account usage, record its event and release the task."""


class ArtifactRepository(Protocol):
    async def add(self, artifact: Artifact) -> Artifact: ...

    async def get(self, scope_id: str, artifact_id: str) -> Artifact:
        """Raise ``NotFound`` when absent or in another scope."""

    async def list_for_task(self, scope_id: str, task_id: str) -> list[Artifact]: ...


class VerificationRepository(Protocol):
    async def add(self, verification: VerificationResult) -> VerificationResult: ...

    async def get(self, verification_id: str) -> VerificationResult: ...

    async def list_for_task(self, task_id: str) -> list[VerificationResult]: ...


class UsageRepository(Protocol):
    """Budget accounting (spec 6.2), one row per ``call_id``.

    Available budget = limits - settled usage - open reservations. A
    reservation is granted only if it fits; the check and the write are
    atomic, so parallel callers cannot both spend the last unit.
    """

    async def reserve(
        self,
        task_id: str,
        attempt_id: str,
        call_id: str,
        kind: UsageKind,
        amount: Usage,
        now: datetime,
    ) -> ReservationResult:
        """Idempotent per ``call_id``: a repeat returns the first result."""

    async def settle(self, call_id: str, actual: Usage, now: datetime) -> UsageReservation:
        """Replace the reservation with real usage and add it to ``Task.usage``.

        Real usage may exceed what was reserved; it is recorded anyway and
        the caller checks ``Usage.exceeded``. Settling twice changes nothing.
        """

    async def release(self, call_id: str, now: datetime) -> UsageReservation:
        """Drop an unsettled reservation, only when the call surely did not
        happen. Unknown outcomes keep their reservation (spec 6.2 step 4)."""

    async def open_reservations(self, task_id: str) -> list[UsageReservation]: ...


class ProactiveSubscriptionRepository(Protocol):
    """Subscription reads and admission that records a decision and task together."""

    async def create(self, subscription: Subscription) -> Subscription: ...

    async def get(self, scope_id: str, subscription_id: str) -> Subscription: ...

    async def list_for_scope(
        self, scope_id: str, *, status: SubscriptionStatus | None = None
    ) -> list[Subscription]: ...

    async def list_all(self) -> list[Subscription]: ...

    async def decisions(self, scope_id: str, subscription_id: str) -> list[AdmissionDecision]: ...

    async def event(self, scope_id: str, event_id: str) -> Event: ...

    async def latest_event(self, scope_id: str, subject_ref: str) -> Event | None: ...

    async def pending_events(self) -> list[Event]: ...

    async def mark_processed(self, scope_id: str, event_id: str, now: datetime) -> None: ...

    async def admit(
        self, event: Event, subscription_id: str,
        evaluate: Callable[[Subscription, list[Task], list[AdmissionDecision], Usage], tuple[AdmissionDecision, Task | None]],
        now: datetime,
    ) -> AdmissionDecision: ...


class DeliveryRepository(Protocol):
    async def list(self, scope_id: str) -> list[InboxItem]: ...
    async def get(self, scope_id: str, item_id: str) -> InboxItem: ...
    async def reserve_notice(self, item: InboxItem, channel: str, now: datetime) -> NoticeReservation: ...
    async def ack(self, scope_id: str, item_id: str, status: str, label: str | None, now: datetime) -> InboxItem: ...


@dataclass(frozen=True)
class Repositories:
    tasks: TaskRepository
    events: EventRepository
    actions: ActionRepository
    artifacts: ArtifactRepository
    verifications: VerificationRepository
    usage: UsageRepository
    subscriptions: ProactiveSubscriptionRepository | None = None
    delivery: DeliveryRepository | None = None
