"""Repository interfaces: the only way harness code reads or writes state.

Spec rule (section 2): "任务状态只能由 TaskService 更新，数据库访问统一放在
repositories". Two implementations must behave the same:

* ``storage.memory.InMemoryRepositories`` (this PR), for unit tests and for
  writing Runner / ToolGateway / Verifier before the database exists;
* a PostgreSQL implementation (next PR), checked by the same test suite in
  ``tests/harness/unit/test_repositories.py``.

Rules every implementation follows:

* Every read that starts from an ID also takes ``scope_id`` and raises
  ``NotFound`` for another scope (spec 3.2).
* A status change and its ``task_status_changed`` event are written in one
  transaction. Model, tool and network calls never run inside one.
* ``Task.version`` goes up on status changes and cancel requests only.
  Heartbeats and usage settlement do not change it.
* Returned objects are copies; changing them changes nothing stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..contracts import (
    Action,
    ActionStatus,
    Artifact,
    AttemptLease,
    Event,
    ReservationResult,
    RunLane,
    Task,
    TaskOutcome,
    Usage,
    UsageKind,
    UsageReservation,
    VerificationResult,
)


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


class TaskRepository(Protocol):
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
    ) -> AttemptLease | None:
        """Take one queued task on ``lanes``: highest priority, then oldest.

        Atomically: status -> running, ``lease_epoch`` + 1, new attempt row,
        status event. Two concurrent claims never return the same task.
        """

    async def heartbeat(self, lease: AttemptLease, lease_ttl_sec: int, now: datetime) -> Task:
        """Extend the lease; return the task so the worker sees cancel requests.

        Raise ``StaleLease`` if the lease is no longer the task's current one
        or has expired.
        """

    async def commit(self, lease: AttemptLease, outcome: TaskOutcome, now: datetime) -> Task:
        """End the attempt and move the task to ``outcome.status``.

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


@dataclass(frozen=True)
class Repositories:
    tasks: TaskRepository
    events: EventRepository
    actions: ActionRepository
    artifacts: ArtifactRepository
    verifications: VerificationRepository
    usage: UsageRepository
