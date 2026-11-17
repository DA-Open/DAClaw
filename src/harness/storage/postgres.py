"""PostgreSQL repositories (asyncpg + SQLAlchemy 2.0 Core, async).

Same behaviour as ``storage.memory``; ``tests/harness_runtime/unit/
test_repositories.py`` runs the same tests on both. What the in-memory store
gets from one lock, this gets from constraints and short transactions:

* each repository method is one transaction (``engine.begin()``); no model,
  tool or network call ever runs inside one;
* claim: ``SELECT ... FOR UPDATE SKIP LOCKED`` on queued rows, so two workers
  never take the same task;
* anything done under a lease locks the task row and checks
  ``current_attempt_id``, ``lease_epoch``, status and the attempt's expiry in
  the same transaction, so a stale worker cannot write;
* budget: the task row is locked before "settled + open reservations +
  this one" is checked against the limits and the reservation inserted, so
  parallel callers cannot both take the last unit;
* idempotency (tasks per request key, actions per logical key, reservations
  per call ID, outside events per source ID) rests on unique constraints.

Rows map to and from the pydantic models in ``contracts.py`` field by field;
``jsonb`` columns hold nested models as JSON.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from ..contracts import (
    Action,
    ActionConflict,
    ActionStatus,
    Artifact,
    Attempt,
    AttemptLease,
    ErrorCode,
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
    TaskError,
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
from . import schema as s
from .repositories import ActionRegistration, CreateResult, Repositories

_LEASED = (TaskStatus.RUNNING.value, TaskStatus.CANCELLING.value)


def async_url(dsn: str) -> str:
    """``postgresql://...`` -> ``postgresql+asyncpg://...`` (other forms kept)."""
    for prefix in ("postgresql://", "postgres://"):
        if dsn.startswith(prefix):
            return "postgresql+asyncpg://" + dsn[len(prefix):]
    return dsn


# -- row <-> model --------------------------------------------------------------------


def _dump(model) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _task_row(task: Task) -> dict[str, Any]:
    row = task.model_dump()
    for key in ("acceptance", "limits", "usage"):
        row[key] = _dump(getattr(task, key))
    row["error"] = _dump(task.error) if task.error is not None else None
    row["input_refs"] = list(task.input_refs)
    for key in ("status", "wait_condition", "agent_role", "run_lane", "trigger_type"):
        value = getattr(task, key)
        row[key] = value.value if value is not None else None
    return row


def _task(row: RowMapping) -> Task:
    return Task.model_validate(dict(row))


def _attempt(row: RowMapping) -> Attempt:
    return Attempt.model_validate(dict(row))


def _event_row(event: Event) -> dict[str, Any]:
    row = event.model_dump(mode="json", exclude={"seq"})
    for key in ("occurred_at", "received_at", "processed_at"):
        row[key] = getattr(event, key)
    return row


def _event(row: RowMapping) -> Event:
    return Event.model_validate(dict(row))


def _action_row(action: Action) -> dict[str, Any]:
    row = action.model_dump()
    row["status"] = action.status.value
    return row


def _action(row: RowMapping) -> Action:
    return Action.model_validate(dict(row))


def _artifact(row: RowMapping) -> Artifact:
    return Artifact.model_validate(dict(row))


def _verification(row: RowMapping) -> VerificationResult:
    return VerificationResult.model_validate(dict(row))


def _reservation(row: RowMapping) -> UsageReservation:
    data = {k: row[k] for k in ("call_id", "task_id", "attempt_id", "kind", "reserved", "settled", "status", "created_at", "settled_at")}
    return UsageReservation.model_validate(data)


def _reservation_result(row: RowMapping) -> ReservationResult:
    if not row["granted"]:
        return ReservationResult(granted=False, exceeded=list(row["exceeded"]))
    return ReservationResult(granted=True, reservation=_reservation(row))


# -- shared helpers (run inside the caller's transaction) -----------------------------


class _Db:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def append_event(self, conn: AsyncConnection, event: Event) -> Event:
        # Identity values are allocated before commit. Serialize publication
        # within a scope so an after_seq reader cannot skip an earlier insert
        # that commits later, including events without a task_id. Take this
        # lock after task/usage locks; callers must not acquire those after
        # appending an event. It is released when the whole transaction ends.
        await conn.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(f"harness.events:{event.scope_id}", 0)))
        )
        row = _event_row(event)
        if event.source_event_id is not None:
            stmt = (
                pg_insert(s.events)
                .values(**row)
                .on_conflict_do_nothing(
                    index_elements=["scope_id", "source", "source_event_id"],
                    index_where=s.events.c.source_event_id.isnot(None),
                )
                .returning(*s.events.c)
            )
            inserted = (await conn.execute(stmt)).mappings().first()
            if inserted is not None:
                return _event(inserted)
            existing = (
                await conn.execute(
                    select(s.events).where(
                        s.events.c.scope_id == event.scope_id,
                        s.events.c.source == event.source,
                        s.events.c.source_event_id == event.source_event_id,
                    )
                )
            ).mappings().one()
            return _event(existing)
        inserted = (await conn.execute(s.events.insert().values(**row).returning(*s.events.c))).mappings().one()
        return _event(inserted)

    async def status_event(
        self,
        conn: AsyncConnection,
        task: Task,
        old: TaskStatus | None,
        now: datetime,
        attempt_id: str | None = None,
        **extra: Any,
    ) -> None:
        payload: dict[str, Any] = {"from": str(old) if old else None, "to": str(task.status)}
        payload.update(extra)
        await self.append_event(
            conn,
            Event(
                scope_id=task.scope_id,
                task_id=task.id,
                attempt_id=attempt_id or task.current_attempt_id,
                type=EventType.TASK_STATUS_CHANGED,
                payload=payload,
                occurred_at=now,
                received_at=now,
            ),
        )

    async def lock_task(self, conn: AsyncConnection, task_id: str) -> Task:
        row = (
            await conn.execute(select(s.tasks).where(s.tasks.c.id == task_id).with_for_update())
        ).mappings().first()
        if row is None:
            raise NotFound(f"task {task_id} not found")
        return _task(row)

    async def check_lease(self, conn: AsyncConnection, lease: AttemptLease, now: datetime) -> tuple[Task, Attempt]:
        """Lock the task row and check the lease is the current, live one."""
        stale = StaleLease(
            f"lease {lease.attempt_id}/{lease.lease_epoch} is not current for task {lease.task_id}",
            task_id=lease.task_id,
        )
        try:
            task = await self.lock_task(conn, lease.task_id)
        except NotFound:
            raise stale from None
        attempt_row = (
            await conn.execute(select(s.attempts).where(s.attempts.c.id == lease.attempt_id))
        ).mappings().first()
        if (
            attempt_row is None
            or task.current_attempt_id != lease.attempt_id
            or task.lease_epoch != lease.lease_epoch
            or task.status.value not in _LEASED
            or attempt_row["ended_at"] is not None
            or attempt_row["lease_expires_at"] <= now
        ):
            raise stale
        return task, _attempt(attempt_row)

    async def open_usage(self, conn: AsyncConnection, task_id: str) -> Usage:
        rows = (
            await conn.execute(
                select(s.usage_reservations.c.reserved).where(
                    s.usage_reservations.c.task_id == task_id,
                    s.usage_reservations.c.status == ReservationStatus.RESERVED.value,
                    s.usage_reservations.c.granted.is_(True),
                )
            )
        ).scalars()
        total = Usage()
        for reserved in rows:
            total = total + Usage.model_validate(reserved)
        return total

    async def reserve(
        self,
        conn: AsyncConnection,
        task_id: str,
        attempt_id: str,
        call_id: str,
        kind: UsageKind,
        amount: Usage,
        now: datetime,
    ) -> ReservationResult:
        task = await self.lock_task(conn, task_id)
        existing = (
            await conn.execute(select(s.usage_reservations).where(s.usage_reservations.c.call_id == call_id))
        ).mappings().first()
        if existing is not None:
            return _reservation_result(existing)
        projected = task.usage + await self.open_usage(conn, task_id) + amount
        exceeded = projected.exceeded(task.limits)
        row = (
            await conn.execute(
                s.usage_reservations.insert()
                .values(
                    call_id=call_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    kind=kind.value,
                    reserved=_dump(amount),
                    settled=None,
                    status=ReservationStatus.RESERVED.value,
                    granted=not exceeded,
                    exceeded=exceeded,
                    created_at=now,
                    settled_at=None,
                )
                .returning(*s.usage_reservations.c)
            )
        ).mappings().one()
        return _reservation_result(row)


# -- repositories --------------------------------------------------------------------


class PgTaskRepository:
    def __init__(self, db: _Db) -> None:
        self._db = db

    async def create(self, task: Task) -> CreateResult:
        async with self._db.engine.begin() as conn:
            inserted = (
                await conn.execute(
                    pg_insert(s.tasks)
                    .values(**_task_row(task))
                    .on_conflict_do_nothing(index_elements=["scope_id", "request_key"])
                    .returning(*s.tasks.c)
                )
            ).mappings().first()
            if inserted is None:
                existing = (
                    await conn.execute(
                        select(s.tasks).where(
                            s.tasks.c.scope_id == task.scope_id, s.tasks.c.request_key == task.request_key
                        )
                    )
                ).mappings().first()
                if existing is None:  # the id itself collided
                    raise HarnessError(f"task id {task.id} already exists")
                stored = _task(existing)
                if stored.request_hash != task.request_hash:
                    raise IdempotencyConflict("request_key already used with different content", task_id=stored.id)
                return CreateResult(task=stored, created=False)
            stored = _task(inserted)
            await self._db.append_event(
                conn,
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
                ),
            )
            return CreateResult(task=stored, created=True)

    async def get(self, scope_id: str, task_id: str) -> Task:
        async with self._db.engine.connect() as conn:
            row = (
                await conn.execute(select(s.tasks).where(s.tasks.c.id == task_id, s.tasks.c.scope_id == scope_id))
            ).mappings().first()
        if row is None:
            raise NotFound(f"task {task_id} not found")
        return _task(row)

    async def claim(
        self,
        worker_id: str,
        lanes: list[RunLane],
        lease_ttl_sec: int,
        now: datetime,
    ) -> AttemptLease | None:
        async with self._db.engine.begin() as conn:
            row = (
                await conn.execute(
                    select(s.tasks)
                    .where(
                        s.tasks.c.status == TaskStatus.QUEUED.value,
                        s.tasks.c.run_lane.in_([RunLane(lane).value for lane in lanes]),
                    )
                    .order_by(s.tasks.c.priority.desc(), s.tasks.c.created_at, s.tasks.c.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).mappings().first()
            if row is None:
                return None
            task = _task(row)
            assert_transition(task.status, TaskStatus.RUNNING)
            epoch = task.lease_epoch + 1
            expires = now + timedelta(seconds=lease_ttl_sec)
            deadline = task.deadline or now + timedelta(seconds=task.limits.timeout_sec)
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
            await conn.execute(s.attempts.insert().values(**attempt.model_dump()))
            updated_row = (
                await conn.execute(
                    update(s.tasks)
                    .where(s.tasks.c.id == task.id)
                    .values(
                        status=TaskStatus.RUNNING.value,
                        lease_epoch=epoch,
                        current_attempt_id=attempt.id,
                        deadline=deadline,
                        version=task.version + 1,
                        updated_at=now,
                    )
                    .returning(*s.tasks.c)
                )
            ).mappings().one()
            await self._db.status_event(conn, _task(updated_row), TaskStatus.QUEUED, now, worker_id=worker_id)
            return AttemptLease(
                task_id=task.id,
                attempt_id=attempt.id,
                scope_id=task.scope_id,
                worker_id=worker_id,
                lease_epoch=epoch,
                lease_expires_at=expires,
                deadline=deadline,
            )

    async def heartbeat(self, lease: AttemptLease, lease_ttl_sec: int, now: datetime) -> Task:
        async with self._db.engine.begin() as conn:
            task, attempt = await self._db.check_lease(conn, lease, now)
            await conn.execute(
                update(s.attempts)
                .where(s.attempts.c.id == attempt.id)
                .values(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=lease_ttl_sec))
            )
            return task

    async def checkpoint(self, lease: AttemptLease, checkpoint_ref: str, now: datetime) -> Task:
        async with self._db.engine.begin() as conn:
            task, _ = await self._db.check_lease(conn, lease, now)
            row = (
                await conn.execute(
                    update(s.tasks)
                    .where(s.tasks.c.id == task.id)
                    .values(checkpoint_ref=checkpoint_ref, updated_at=now)
                    .returning(*s.tasks.c)
                )
            ).mappings().one()
            return _task(row)

    async def record_run(
        self,
        lease: AttemptLease,
        now: datetime,
        *,
        model_version: str | None = None,
        context_ref: str | None = None,
    ) -> Attempt:
        async with self._db.engine.begin() as conn:
            _, attempt = await self._db.check_lease(conn, lease, now)
            updates = {k: v for k, v in (("model_version", model_version), ("context_ref", context_ref)) if v is not None}
            if not updates:
                return attempt
            row = (
                await conn.execute(
                    update(s.attempts).where(s.attempts.c.id == attempt.id).values(**updates).returning(*s.attempts.c)
                )
            ).mappings().one()
            return _attempt(row)

    async def get_attempt(self, scope_id: str, attempt_id: str) -> Attempt:
        async with self._db.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(s.attempts)
                    .join(s.tasks, s.tasks.c.id == s.attempts.c.task_id)
                    .where(s.attempts.c.id == attempt_id, s.tasks.c.scope_id == scope_id)
                )
            ).mappings().first()
        if row is None:
            raise NotFound(f"attempt {attempt_id} not found")
        return _attempt(row)

    async def commit(self, lease: AttemptLease, outcome: TaskOutcome, now: datetime) -> Task:
        async with self._db.engine.begin() as conn:
            task, attempt = await self._db.check_lease(conn, lease, now)
            # The task lock orders cancellation against completion. If cancel
            # won, finish this attempt as cancelled instead of stranding it.
            if task.status is TaskStatus.CANCELLING and outcome.status is not TaskStatus.CANCELLED:
                outcome = TaskOutcome.model_validate(
                    outcome.model_dump() | {
                        "status": TaskStatus.CANCELLED,
                        "result_ref": None,
                        "verification_ref": None,
                        "wait_condition": None,
                        "error": TaskError(code=ErrorCode.CANCELLED, message=task.cancel_reason or "cancel requested"),
                    }
                )
            assert_transition(task.status, outcome.status)
            if outcome.status is TaskStatus.SUCCEEDED:
                verification = (
                    await conn.execute(
                        select(s.verifications).where(s.verifications.c.id == (outcome.verification_ref or ""))
                    )
                ).mappings().first()
                if (
                    verification is None
                    or verification["task_id"] != task.id
                    or verification["status"] != VerificationStatus.PASSED.value
                    or verification["result_ref"] != outcome.result_ref
                ):
                    raise VerificationRequired("succeeded needs a passed verification of the result", task_id=task.id)
            old = task.status
            row = (
                await conn.execute(
                    update(s.tasks)
                    .where(s.tasks.c.id == task.id)
                    .values(
                        status=outcome.status.value,
                        result_ref=(
                            None if outcome.status is TaskStatus.CANCELLED
                            else outcome.result_ref if outcome.result_ref is not None else task.result_ref
                        ),
                        wait_condition=outcome.wait_condition.value if outcome.wait_condition else None,
                        error=_dump(outcome.error) if outcome.error is not None else None,
                        checkpoint_ref=(
                            outcome.checkpoint_ref if outcome.checkpoint_ref is not None else task.checkpoint_ref
                        ),
                        current_attempt_id=None,
                        version=task.version + 1,
                        updated_at=now,
                    )
                    .returning(*s.tasks.c)
                )
            ).mappings().one()
            await conn.execute(
                update(s.attempts)
                .where(s.attempts.c.id == attempt.id)
                .values(ended_at=now, exit_reason=str(outcome.status))
            )
            updated = _task(row)
            extra: dict[str, Any] = {}
            if outcome.error is not None:
                extra["error_code"] = str(outcome.error.code)
            if outcome.artifact_refs:
                extra["artifact_refs"] = list(outcome.artifact_refs)
            await self._db.status_event(conn, updated, old, now, attempt_id=attempt.id, **extra)
            return updated

    async def request_cancel(self, scope_id: str, task_id: str, reason: str, now: datetime) -> Task:
        async with self._db.engine.begin() as conn:
            row = (
                await conn.execute(
                    select(s.tasks).where(s.tasks.c.id == task_id, s.tasks.c.scope_id == scope_id).with_for_update()
                )
            ).mappings().first()
            if row is None:
                raise NotFound(f"task {task_id} not found")
            task = _task(row)
            if task.status.terminal or task.status is TaskStatus.CANCELLING:
                return task
            target = TaskStatus.CANCELLED if task.status in (TaskStatus.QUEUED, TaskStatus.WAITING) else TaskStatus.CANCELLING
            assert_transition(task.status, target)
            updated_row = (
                await conn.execute(
                    update(s.tasks)
                    .where(s.tasks.c.id == task.id)
                    .values(
                        status=target.value,
                        wait_condition=None,
                        cancel_requested_at=now,
                        cancel_reason=reason,
                        version=task.version + 1,
                        updated_at=now,
                    )
                    .returning(*s.tasks.c)
                )
            ).mappings().one()
            updated = _task(updated_row)
            await self._db.status_event(conn, updated, task.status, now, reason=reason)
            return updated


class PgEventRepository:
    def __init__(self, db: _Db) -> None:
        self._db = db

    async def append(self, event: Event) -> Event:
        async with self._db.engine.begin() as conn:
            return await self._db.append_event(conn, event)

    async def list(
        self,
        scope_id: str,
        task_id: str | None = None,
        after_seq: int = 0,
        limit: int = 100,
    ) -> list[Event]:
        query = select(s.events).where(s.events.c.scope_id == scope_id, s.events.c.seq > after_seq)
        if task_id is not None:
            query = query.where(s.events.c.task_id == task_id)
        async with self._db.engine.connect() as conn:
            rows = (await conn.execute(query.order_by(s.events.c.seq).limit(limit))).mappings().all()
        return [_event(r) for r in rows]


class PgActionRepository:
    def __init__(self, db: _Db) -> None:
        self._db = db

    async def register(self, action: Action, reserve: Usage | None = None) -> ActionRegistration:
        async with self._db.engine.begin() as conn:
            # Locking the task row orders registrations of one task, so the
            # key lookup, the budget check and the insert see one state.
            await self._db.lock_task(conn, action.task_id)
            existing = (
                await conn.execute(
                    select(s.actions).where(
                        s.actions.c.task_id == action.task_id, s.actions.c.logical_key == action.logical_key
                    )
                )
            ).mappings().first()
            if existing is not None:
                if existing["request_hash"] != action.request_hash:
                    raise ActionConflict("logical_key already used with different arguments", action_id=existing["id"])
                return ActionRegistration(action=_action(existing), created=False)
            budget = None
            if reserve is not None:
                budget = await self._db.reserve(
                    conn, action.task_id, action.attempt_id, action.id, UsageKind.TOOL, reserve, action.created_at
                )
                if not budget.granted:
                    return ActionRegistration(action=None, created=False, budget=budget)
            row = (await conn.execute(s.actions.insert().values(**_action_row(action)).returning(*s.actions.c))).mappings().one()
            return ActionRegistration(action=_action(row), created=True, budget=budget)

    async def get(self, task_id: str, logical_key: str) -> Action | None:
        async with self._db.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(s.actions).where(s.actions.c.task_id == task_id, s.actions.c.logical_key == logical_key)
                )
            ).mappings().first()
        return _action(row) if row is not None else None

    async def update(
        self,
        action_id: str,
        status: ActionStatus,
        *,
        result_ref: str | None = None,
        receipt_ref: str | None = None,
        now: datetime,
    ) -> Action:
        values: dict[str, Any] = {"status": status.value, "updated_at": now}
        if result_ref is not None:
            values["result_ref"] = result_ref
        if receipt_ref is not None:
            values["receipt_ref"] = receipt_ref
        async with self._db.engine.begin() as conn:
            row = (
                await conn.execute(
                    update(s.actions).where(s.actions.c.id == action_id).values(**values).returning(*s.actions.c)
                )
            ).mappings().first()
        if row is None:
            raise NotFound(f"action {action_id} not found")
        return _action(row)

    async def list_for_task(self, task_id: str) -> list[Action]:
        async with self._db.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(s.actions).where(s.actions.c.task_id == task_id).order_by(s.actions.c.created_at, s.actions.c.id)
                )
            ).mappings().all()
        return [_action(r) for r in rows]


class PgArtifactRepository:
    def __init__(self, db: _Db) -> None:
        self._db = db

    async def add(self, artifact: Artifact) -> Artifact:
        async with self._db.engine.begin() as conn:
            row = (
                await conn.execute(
                    pg_insert(s.artifacts)
                    .values(**artifact.model_dump())
                    .on_conflict_do_nothing(index_elements=["id"])
                    .returning(*s.artifacts.c)
                )
            ).mappings().first()
        if row is None:
            raise HarnessError(f"artifact id {artifact.id} already exists")
        return _artifact(row)

    async def get(self, scope_id: str, artifact_id: str) -> Artifact:
        async with self._db.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(s.artifacts).where(s.artifacts.c.id == artifact_id, s.artifacts.c.scope_id == scope_id)
                )
            ).mappings().first()
        if row is None:
            raise NotFound(f"artifact {artifact_id} not found")
        return _artifact(row)

    async def list_for_task(self, scope_id: str, task_id: str) -> list[Artifact]:
        async with self._db.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(s.artifacts)
                    .where(s.artifacts.c.scope_id == scope_id, s.artifacts.c.task_id == task_id)
                    .order_by(s.artifacts.c.created_at, s.artifacts.c.id)
                )
            ).mappings().all()
        return [_artifact(r) for r in rows]


class PgVerificationRepository:
    def __init__(self, db: _Db) -> None:
        self._db = db

    async def add(self, verification: VerificationResult) -> VerificationResult:
        async with self._db.engine.begin() as conn:
            exists = (await conn.execute(select(s.tasks.c.id).where(s.tasks.c.id == verification.task_id))).first()
            if exists is None:
                raise NotFound(f"task {verification.task_id} not found")
            row = verification.model_dump(mode="json")
            row["created_at"] = verification.created_at
            inserted = (
                await conn.execute(
                    pg_insert(s.verifications)
                    .values(**row)
                    .on_conflict_do_nothing(index_elements=["id"])
                    .returning(*s.verifications.c)
                )
            ).mappings().first()
        if inserted is None:
            raise HarnessError(f"verification id {verification.id} already exists")
        return _verification(inserted)

    async def get(self, verification_id: str) -> VerificationResult:
        async with self._db.engine.connect() as conn:
            row = (
                await conn.execute(select(s.verifications).where(s.verifications.c.id == verification_id))
            ).mappings().first()
        if row is None:
            raise NotFound(f"verification {verification_id} not found")
        return _verification(row)

    async def list_for_task(self, task_id: str) -> list[VerificationResult]:
        async with self._db.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(s.verifications)
                    .where(s.verifications.c.task_id == task_id)
                    .order_by(s.verifications.c.created_at, s.verifications.c.id)
                )
            ).mappings().all()
        return [_verification(r) for r in rows]


class PgUsageRepository:
    def __init__(self, db: _Db) -> None:
        self._db = db

    async def reserve(
        self,
        task_id: str,
        attempt_id: str,
        call_id: str,
        kind: UsageKind,
        amount: Usage,
        now: datetime,
    ) -> ReservationResult:
        async with self._db.engine.begin() as conn:
            return await self._db.reserve(conn, task_id, attempt_id, call_id, kind, amount, now)

    async def _granted(self, conn: AsyncConnection, call_id: str) -> RowMapping:
        row = (
            await conn.execute(
                select(s.usage_reservations)
                .where(s.usage_reservations.c.call_id == call_id, s.usage_reservations.c.granted.is_(True))
                .with_for_update()
            )
        ).mappings().first()
        if row is None:
            raise NotFound(f"no reservation for call {call_id}")
        return row

    async def settle(self, call_id: str, actual: Usage, now: datetime) -> UsageReservation:
        async with self._db.engine.begin() as conn:
            # Lock the task first (same order as reserve) so settling and
            # reserving on one task never deadlock.
            task_id = (
                await conn.execute(
                    select(s.usage_reservations.c.task_id).where(
                        s.usage_reservations.c.call_id == call_id, s.usage_reservations.c.granted.is_(True)
                    )
                )
            ).scalar()
            if task_id is None:
                raise NotFound(f"no reservation for call {call_id}")
            task = await self._db.lock_task(conn, task_id)
            reservation = await self._granted(conn, call_id)
            if reservation["status"] == ReservationStatus.SETTLED.value:
                return _reservation(reservation)
            if reservation["status"] == ReservationStatus.RELEASED.value:
                raise HarnessError(f"reservation {call_id} was released and cannot be settled")
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == task_id)
                .values(usage=_dump(task.usage + actual), updated_at=now)
            )
            row = (
                await conn.execute(
                    update(s.usage_reservations)
                    .where(s.usage_reservations.c.call_id == call_id)
                    .values(settled=_dump(actual), status=ReservationStatus.SETTLED.value, settled_at=now)
                    .returning(*s.usage_reservations.c)
                )
            ).mappings().one()
            return _reservation(row)

    async def release(self, call_id: str, now: datetime) -> UsageReservation:
        async with self._db.engine.begin() as conn:
            reservation = await self._granted(conn, call_id)
            if reservation["status"] == ReservationStatus.SETTLED.value:
                raise HarnessError(f"reservation {call_id} is settled and cannot be released")
            if reservation["status"] == ReservationStatus.RELEASED.value:
                return _reservation(reservation)
            row = (
                await conn.execute(
                    update(s.usage_reservations)
                    .where(s.usage_reservations.c.call_id == call_id)
                    .values(status=ReservationStatus.RELEASED.value, settled_at=now)
                    .returning(*s.usage_reservations.c)
                )
            ).mappings().one()
            return _reservation(row)

    async def open_reservations(self, task_id: str) -> list[UsageReservation]:
        async with self._db.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(s.usage_reservations)
                    .where(
                        s.usage_reservations.c.task_id == task_id,
                        s.usage_reservations.c.status == ReservationStatus.RESERVED.value,
                        s.usage_reservations.c.granted.is_(True),
                    )
                    .order_by(s.usage_reservations.c.created_at, s.usage_reservations.c.call_id)
                )
            ).mappings().all()
        return [_reservation(r) for r in rows]


class PgRepositories(Repositories):
    """All six repositories over one async engine.

    ``engine`` is owned by the caller (``from_dsn`` creates one); call
    ``close()`` when done. Tables must exist: run the migrations first
    (``python -m harness.storage.migrate upgrade``).
    """

    def __init__(self, engine: AsyncEngine) -> None:
        db = _Db(engine)
        super().__init__(
            tasks=PgTaskRepository(db),
            events=PgEventRepository(db),
            actions=PgActionRepository(db),
            artifacts=PgArtifactRepository(db),
            verifications=PgVerificationRepository(db),
            usage=PgUsageRepository(db),
        )
        object.__setattr__(self, "engine", engine)

    @classmethod
    def from_dsn(cls, dsn: str, **engine_kwargs: Any) -> PgRepositories:
        engine_kwargs.setdefault("pool_pre_ping", True)
        return cls(create_async_engine(async_url(dsn), **engine_kwargs))

    async def close(self) -> None:
        await self.engine.dispose()


__all__ = ["PgRepositories", "async_url"]
