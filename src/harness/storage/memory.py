"""In-memory repositories with the same behaviour as the PostgreSQL ones.

One ``asyncio.Lock`` guards all tables, so every method is one atomic
"transaction". Good for unit tests and single-process development; state is
lost when the process ends.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, TypeVar

from pydantic import BaseModel

from ..contracts import (
    Action,
    ActionConflict,
    ActionStatus,
    Artifact,
    Attempt,
    AttemptLease,
    Event,
    EventType,
    HarnessError,
    IdempotencyConflict,
    NotFound,
    ReservationResult,
    ReservationStatus,
    RunLane,
    StaleLease,
    Task,
    TaskOutcome,
    TaskStatus,
    Usage,
    UsageKind,
    UsageReservation,
    VerificationRequired,
    VerificationResult,
    VerificationStatus,
    assert_transition,
    new_id,
)
from .repositories import ActionRegistration, CreateResult, Repositories

M = TypeVar("M", bound=BaseModel)

_LEASED = frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLING})


def _copy(model: M) -> M:
    return model.model_copy(deep=True)


def _replace(model: M, **updates: Any) -> M:
    """Copy with updates, re-running validation (model_copy does not)."""
    data = model.model_dump()
    data.update(updates)
    return type(model).model_validate(data)


class _Store:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.tasks: dict[str, Task] = {}
        self.task_keys: dict[tuple[str, str], str] = {}
        self.attempts: dict[str, Attempt] = {}
        self.events: list[Event] = []
        self.event_keys: dict[tuple[str, str, str], Event] = {}
        self.actions: dict[str, Action] = {}
        self.action_keys: dict[tuple[str, str], str] = {}
        self.artifacts: dict[str, Artifact] = {}
        self.verifications: dict[str, VerificationResult] = {}
        self.reservations: dict[str, UsageReservation] = {}
        self.reservation_results: dict[str, ReservationResult] = {}

    # -- helpers, call with the lock held ------------------------------------

    def append_event(self, event: Event) -> Event:
        if event.source_event_id is not None:
            key = (event.scope_id, event.source, event.source_event_id)
            if key in self.event_keys:
                return self.event_keys[key]
        stored = _replace(event, seq=len(self.events) + 1)
        self.events.append(stored)
        if stored.source_event_id is not None:
            self.event_keys[(stored.scope_id, stored.source, stored.source_event_id)] = stored
        return stored

    def status_event(
        self,
        task: Task,
        old: TaskStatus | None,
        now: datetime,
        attempt_id: str | None = None,
        **extra: Any,
    ) -> None:
        payload: dict[str, Any] = {"from": str(old) if old else None, "to": str(task.status)}
        payload.update(extra)
        self.append_event(
            Event(
                scope_id=task.scope_id,
                task_id=task.id,
                attempt_id=attempt_id or task.current_attempt_id,
                type=EventType.TASK_STATUS_CHANGED,
                payload=payload,
                occurred_at=now,
                received_at=now,
            )
        )

    def task(self, task_id: str) -> Task:
        try:
            return self.tasks[task_id]
        except KeyError:
            raise NotFound(f"task {task_id} not found") from None

    def check_lease(self, lease: AttemptLease, now: datetime) -> tuple[Task, Attempt]:
        task = self.task(lease.task_id)
        attempt = self.attempts.get(lease.attempt_id)
        if (
            attempt is None
            or task.current_attempt_id != lease.attempt_id
            or task.lease_epoch != lease.lease_epoch
            or task.status not in _LEASED
            or attempt.ended_at is not None
            or attempt.lease_expires_at <= now
        ):
            raise StaleLease(
                f"lease {lease.attempt_id}/{lease.lease_epoch} is not current for task {lease.task_id}",
                task_id=lease.task_id,
            )
        return task, attempt

    def open_usage(self, task_id: str) -> Usage:
        total = Usage()
        for r in self.reservations.values():
            if r.task_id == task_id and r.status is ReservationStatus.RESERVED:
                total = total + r.reserved
        return total

    def reserve(
        self,
        task_id: str,
        attempt_id: str,
        call_id: str,
        kind: UsageKind,
        amount: Usage,
        now: datetime,
    ) -> ReservationResult:
        if call_id in self.reservation_results:
            return self.reservation_results[call_id]
        task = self.task(task_id)
        projected = task.usage + self.open_usage(task_id) + amount
        exceeded = projected.exceeded(task.limits)
        if exceeded:
            result = ReservationResult(granted=False, exceeded=exceeded)
        else:
            reservation = UsageReservation(
                call_id=call_id,
                task_id=task_id,
                attempt_id=attempt_id,
                kind=kind,
                reserved=amount,
                created_at=now,
            )
            self.reservations[call_id] = reservation
            result = ReservationResult(granted=True, reservation=_copy(reservation))
        self.reservation_results[call_id] = result
        return result


class InMemoryTaskRepository:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def create(self, task: Task) -> CreateResult:
        async with self._s.lock:
            key = (task.scope_id, task.request_key)
            existing_id = self._s.task_keys.get(key)
            if existing_id is not None:
                existing = self._s.tasks[existing_id]
                if existing.request_hash != task.request_hash:
                    raise IdempotencyConflict(
                        "request_key already used with different content",
                        task_id=existing.id,
                    )
                return CreateResult(task=_copy(existing), created=False)
            if task.id in self._s.tasks:
                raise HarnessError(f"task id {task.id} already exists")
            stored = _copy(task)
            self._s.tasks[stored.id] = stored
            self._s.task_keys[key] = stored.id
            self._s.append_event(
                Event(
                    scope_id=stored.scope_id,
                    task_id=stored.id,
                    type=EventType.TASK_CREATED,
                    payload={
                        "agent_role": str(stored.agent_role),
                        "run_lane": str(stored.run_lane),
                        "status": str(stored.status),
                    },
                    occurred_at=stored.created_at,
                    received_at=stored.created_at,
                )
            )
            return CreateResult(task=_copy(stored), created=True)

    async def get(self, scope_id: str, task_id: str) -> Task:
        async with self._s.lock:
            task = self._s.tasks.get(task_id)
            if task is None or task.scope_id != scope_id:
                raise NotFound(f"task {task_id} not found")
            return _copy(task)

    async def claim(
        self,
        worker_id: str,
        lanes: list[RunLane],
        lease_ttl_sec: int,
        now: datetime,
    ) -> AttemptLease | None:
        async with self._s.lock:
            candidates = [
                t
                for t in self._s.tasks.values()
                if t.status is TaskStatus.QUEUED and t.run_lane in lanes
            ]
            if not candidates:
                return None
            candidates.sort(key=lambda t: (-t.priority, t.created_at, t.id))
            task = candidates[0]
            assert_transition(task.status, TaskStatus.RUNNING)
            epoch = task.lease_epoch + 1
            expires = now + timedelta(seconds=lease_ttl_sec)
            attempt = Attempt(
                id=new_id("att"),
                task_id=task.id,
                worker_id=worker_id,
                lease_epoch=epoch,
                lease_expires_at=expires,
                heartbeat_at=now,
                started_at=now,
                agent_profile_version=task.agent_profile_ref,
                capability_version=task.capability_ref,
            )
            self._s.attempts[attempt.id] = attempt
            updated = _replace(
                task,
                status=TaskStatus.RUNNING,
                lease_epoch=epoch,
                current_attempt_id=attempt.id,
                version=task.version + 1,
                updated_at=now,
            )
            self._s.tasks[task.id] = updated
            self._s.status_event(updated, TaskStatus.QUEUED, now, worker_id=worker_id)
            return AttemptLease(
                task_id=task.id,
                attempt_id=attempt.id,
                scope_id=task.scope_id,
                worker_id=worker_id,
                lease_epoch=epoch,
                lease_expires_at=expires,
                deadline=task.deadline,
            )

    async def heartbeat(self, lease: AttemptLease, lease_ttl_sec: int, now: datetime) -> Task:
        async with self._s.lock:
            task, attempt = self._s.check_lease(lease, now)
            self._s.attempts[attempt.id] = _replace(
                attempt,
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=lease_ttl_sec),
            )
            return _copy(task)

    async def commit(self, lease: AttemptLease, outcome: TaskOutcome, now: datetime) -> Task:
        async with self._s.lock:
            task, attempt = self._s.check_lease(lease, now)
            assert_transition(task.status, outcome.status)
            if outcome.status is TaskStatus.SUCCEEDED:
                verification = self._s.verifications.get(outcome.verification_ref or "")
                if (
                    verification is None
                    or verification.task_id != task.id
                    or verification.status is not VerificationStatus.PASSED
                    or verification.result_ref != outcome.result_ref
                ):
                    raise VerificationRequired(
                        "succeeded needs a passed verification of the result",
                        task_id=task.id,
                    )
            old = task.status
            updated = _replace(
                task,
                status=outcome.status,
                result_ref=outcome.result_ref if outcome.result_ref is not None else task.result_ref,
                wait_condition=outcome.wait_condition,
                error=outcome.error,
                checkpoint_ref=(
                    outcome.checkpoint_ref if outcome.checkpoint_ref is not None else task.checkpoint_ref
                ),
                current_attempt_id=None,
                version=task.version + 1,
                updated_at=now,
            )
            self._s.attempts[attempt.id] = _replace(
                attempt, ended_at=now, exit_reason=str(outcome.status)
            )
            self._s.tasks[task.id] = updated
            extra: dict[str, Any] = {}
            if outcome.error is not None:
                extra["error_code"] = str(outcome.error.code)
            if outcome.artifact_refs:
                extra["artifact_refs"] = list(outcome.artifact_refs)
            self._s.status_event(updated, old, now, attempt_id=attempt.id, **extra)
            return _copy(updated)

    async def request_cancel(self, scope_id: str, task_id: str, reason: str, now: datetime) -> Task:
        async with self._s.lock:
            task = self._s.tasks.get(task_id)
            if task is None or task.scope_id != scope_id:
                raise NotFound(f"task {task_id} not found")
            if task.status.terminal or task.status is TaskStatus.CANCELLING:
                return _copy(task)
            if task.status in (TaskStatus.QUEUED, TaskStatus.WAITING):
                target = TaskStatus.CANCELLED
            else:
                target = TaskStatus.CANCELLING
            assert_transition(task.status, target)
            old = task.status
            updated = _replace(
                task,
                status=target,
                wait_condition=None,
                cancel_requested_at=now,
                cancel_reason=reason,
                version=task.version + 1,
                updated_at=now,
            )
            self._s.tasks[task.id] = updated
            self._s.status_event(updated, old, now, reason=reason)
            return _copy(updated)


class InMemoryEventRepository:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def append(self, event: Event) -> Event:
        async with self._s.lock:
            return _copy(self._s.append_event(event))

    async def list(
        self,
        scope_id: str,
        task_id: str | None = None,
        after_seq: int = 0,
        limit: int = 100,
    ) -> list[Event]:
        async with self._s.lock:
            found = [
                e
                for e in self._s.events
                if e.scope_id == scope_id
                and e.seq > after_seq
                and (task_id is None or e.task_id == task_id)
            ]
            return [_copy(e) for e in found[:limit]]


class InMemoryActionRepository:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def register(self, action: Action, reserve: Usage | None = None) -> ActionRegistration:
        async with self._s.lock:
            self._s.task(action.task_id)
            key = (action.task_id, action.logical_key)
            existing_id = self._s.action_keys.get(key)
            if existing_id is not None:
                existing = self._s.actions[existing_id]
                if existing.request_hash != action.request_hash:
                    raise ActionConflict(
                        "logical_key already used with different arguments",
                        action_id=existing.id,
                    )
                return ActionRegistration(action=_copy(existing), created=False)
            budget = None
            if reserve is not None:
                budget = self._s.reserve(
                    action.task_id,
                    action.attempt_id,
                    action.id,
                    UsageKind.TOOL,
                    reserve,
                    action.created_at,
                )
                if not budget.granted:
                    return ActionRegistration(action=None, created=False, budget=budget)
            stored = _copy(action)
            self._s.actions[stored.id] = stored
            self._s.action_keys[key] = stored.id
            return ActionRegistration(action=_copy(stored), created=True, budget=budget)

    async def get(self, task_id: str, logical_key: str) -> Action | None:
        async with self._s.lock:
            action_id = self._s.action_keys.get((task_id, logical_key))
            return _copy(self._s.actions[action_id]) if action_id else None

    async def update(
        self,
        action_id: str,
        status: ActionStatus,
        *,
        result_ref: str | None = None,
        receipt_ref: str | None = None,
        now: datetime,
    ) -> Action:
        async with self._s.lock:
            action = self._s.actions.get(action_id)
            if action is None:
                raise NotFound(f"action {action_id} not found")
            updated = _replace(
                action,
                status=status,
                result_ref=result_ref if result_ref is not None else action.result_ref,
                receipt_ref=receipt_ref if receipt_ref is not None else action.receipt_ref,
                updated_at=now,
            )
            self._s.actions[action_id] = updated
            return _copy(updated)

    async def list_for_task(self, task_id: str) -> list[Action]:
        async with self._s.lock:
            found = [a for a in self._s.actions.values() if a.task_id == task_id]
            found.sort(key=lambda a: (a.created_at, a.id))
            return [_copy(a) for a in found]


class InMemoryArtifactRepository:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def add(self, artifact: Artifact) -> Artifact:
        async with self._s.lock:
            if artifact.id in self._s.artifacts:
                raise HarnessError(f"artifact id {artifact.id} already exists")
            self._s.artifacts[artifact.id] = _copy(artifact)
            return _copy(artifact)

    async def get(self, scope_id: str, artifact_id: str) -> Artifact:
        async with self._s.lock:
            artifact = self._s.artifacts.get(artifact_id)
            if artifact is None or artifact.scope_id != scope_id:
                raise NotFound(f"artifact {artifact_id} not found")
            return _copy(artifact)

    async def list_for_task(self, scope_id: str, task_id: str) -> list[Artifact]:
        async with self._s.lock:
            found = [
                a
                for a in self._s.artifacts.values()
                if a.scope_id == scope_id and a.task_id == task_id
            ]
            found.sort(key=lambda a: (a.created_at, a.id))
            return [_copy(a) for a in found]


class InMemoryVerificationRepository:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def add(self, verification: VerificationResult) -> VerificationResult:
        async with self._s.lock:
            self._s.task(verification.task_id)
            if verification.id in self._s.verifications:
                raise HarnessError(f"verification id {verification.id} already exists")
            self._s.verifications[verification.id] = _copy(verification)
            return _copy(verification)

    async def get(self, verification_id: str) -> VerificationResult:
        async with self._s.lock:
            verification = self._s.verifications.get(verification_id)
            if verification is None:
                raise NotFound(f"verification {verification_id} not found")
            return _copy(verification)

    async def list_for_task(self, task_id: str) -> list[VerificationResult]:
        async with self._s.lock:
            found = [v for v in self._s.verifications.values() if v.task_id == task_id]
            found.sort(key=lambda v: (v.created_at, v.id))
            return [_copy(v) for v in found]


class InMemoryUsageRepository:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def reserve(
        self,
        task_id: str,
        attempt_id: str,
        call_id: str,
        kind: UsageKind,
        amount: Usage,
        now: datetime,
    ) -> ReservationResult:
        async with self._s.lock:
            return _copy(self._s.reserve(task_id, attempt_id, call_id, kind, amount, now))

    async def settle(self, call_id: str, actual: Usage, now: datetime) -> UsageReservation:
        async with self._s.lock:
            reservation = self._s.reservations.get(call_id)
            if reservation is None:
                raise NotFound(f"no reservation for call {call_id}")
            if reservation.status is ReservationStatus.SETTLED:
                return _copy(reservation)
            if reservation.status is ReservationStatus.RELEASED:
                raise HarnessError(f"reservation {call_id} was released and cannot be settled")
            task = self._s.task(reservation.task_id)
            self._s.tasks[task.id] = _replace(task, usage=(task.usage + actual).model_dump(), updated_at=now)
            settled = _replace(
                reservation,
                settled=actual.model_dump(),
                status=ReservationStatus.SETTLED,
                settled_at=now,
            )
            self._s.reservations[call_id] = settled
            return _copy(settled)

    async def release(self, call_id: str, now: datetime) -> UsageReservation:
        async with self._s.lock:
            reservation = self._s.reservations.get(call_id)
            if reservation is None:
                raise NotFound(f"no reservation for call {call_id}")
            if reservation.status is ReservationStatus.SETTLED:
                raise HarnessError(f"reservation {call_id} is settled and cannot be released")
            if reservation.status is ReservationStatus.RELEASED:
                return _copy(reservation)
            released = _replace(reservation, status=ReservationStatus.RELEASED, settled_at=now)
            self._s.reservations[call_id] = released
            return _copy(released)

    async def open_reservations(self, task_id: str) -> list[UsageReservation]:
        async with self._s.lock:
            return [
                _copy(r)
                for r in self._s.reservations.values()
                if r.task_id == task_id and r.status is ReservationStatus.RESERVED
            ]


class InMemoryRepositories(Repositories):
    """All six repositories over one shared in-memory store."""

    def __init__(self) -> None:
        store = _Store()
        super().__init__(
            tasks=InMemoryTaskRepository(store),
            events=InMemoryEventRepository(store),
            actions=InMemoryActionRepository(store),
            artifacts=InMemoryArtifactRepository(store),
            verifications=InMemoryVerificationRepository(store),
            usage=InMemoryUsageRepository(store),
        )
