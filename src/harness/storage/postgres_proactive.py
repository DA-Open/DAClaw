"""PostgreSQL subscription transactions; no tools or model calls under locks."""
from sqlalchemy import select, update, func
from sqlalchemy.dialects.postgresql import insert

from ..contracts import (
    AdmissionDecision, Event, EventType, Limits, NotFound, Subscription,
    SubscriptionStatus, TaskStatus, Usage,
)
from . import schema as s
from .postgres import _dump, _event, _task, _task_row


def _subscription(body):
    """Read subscriptions persisted by the earlier data-card draft."""
    values = dict(body)
    old_limits = values.pop("daily_limits", None)
    if old_limits is not None and "daily_budget" not in values:
        limits = Limits.model_validate(old_limits)
        values["daily_budget"] = Usage(
            model_calls=limits.max_model_calls,
            tool_calls=limits.max_tool_calls,
            input_tokens=limits.max_total_tokens,
        )
    old_ttl = values.pop("result_ttl_sec", None)
    if old_ttl is not None:
        values.setdefault("result_ttl_seconds", old_ttl)
    return Subscription.model_validate(values)


async def lock_scope(conn, scope_id):
    # A scope lock serializes admission, completion and revocation across processes.
    await conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(scope_id, 0))))


class PgSubscriptionRepository:
    def __init__(self, db):
        self._db = db

    async def create(self, subscription):
        async with self._db.engine.begin() as conn:
            await conn.execute(s.subscriptions.insert().values(id=subscription.id, scope_id=subscription.scope_id,
                               status=subscription.status.value, body=_dump(subscription)))
        return subscription.model_copy(deep=True)

    async def _get(self, conn, scope_id, subscription_id):
        body = (await conn.execute(select(s.subscriptions.c.body).where(
            s.subscriptions.c.id == subscription_id, s.subscriptions.c.scope_id == scope_id))).scalar_one_or_none()
        if body is None:
            raise NotFound("subscription not found")
        return _subscription(body)

    async def get(self, scope_id, subscription_id):
        async with self._db.engine.connect() as conn:
            return await self._get(conn, scope_id, subscription_id)

    async def list_for_scope(self, scope_id, *, status=None):
        query = select(s.subscriptions.c.body).where(s.subscriptions.c.scope_id == scope_id)
        if status is not None:
            query = query.where(s.subscriptions.c.status == str(status))
        async with self._db.engine.connect() as conn:
            return [_subscription(b) for b in (await conn.execute(query)).scalars()]

    async def list_all(self):
        async with self._db.engine.connect() as conn:
            return [_subscription(b) for b in (await conn.execute(select(s.subscriptions.c.body))).scalars()]

    async def decisions(self, scope_id, subscription_id):
        async with self._db.engine.connect() as conn:
            await self._get(conn, scope_id, subscription_id)
            return [AdmissionDecision.model_validate(b) for b in (await conn.execute(select(s.subscription_events.c.body)
                    .where(s.subscription_events.c.subscription_id == subscription_id))).scalars()]

    async def event(self, scope_id, event_id):
        async with self._db.engine.connect() as conn:
            row = (await conn.execute(select(s.events).where(s.events.c.id == event_id,
                   s.events.c.scope_id == scope_id))).mappings().first()
            if row is None:
                raise NotFound("event not found")
            return _event(row)

    async def latest_event(self, scope_id, subject_ref):
        async with self._db.engine.connect() as conn:
            row = (await conn.execute(select(s.events).where(s.events.c.scope_id == scope_id,
                s.events.c.subject_ref == subject_ref, s.events.c.type == "external", s.events.c.source != "scheduler")
                .order_by(s.events.c.seq.desc()).limit(1))).mappings().first()
            return _event(row) if row else None

    async def pending_events(self):
        async with self._db.engine.connect() as conn:
            deferred = select(s.subscription_events.c.event_id).where(s.subscription_events.c.decision == "defer")
            rows = (await conn.execute(select(s.events).where(s.events.c.type == "external",
                    s.events.c.processed_at.is_(None) | s.events.c.id.in_(deferred)).order_by(s.events.c.seq))).mappings()
            return [_event(r) for r in rows]

    async def mark_processed(self, scope_id, event_id, now):
        async with self._db.engine.begin() as conn:
            await conn.execute(update(s.events).where(s.events.c.id == event_id, s.events.c.scope_id == scope_id)
                               .values(processed_at=now))

    async def admit(self, event, subscription_id, evaluate, now):
        async with self._db.engine.begin() as conn:
            await lock_scope(conn, event.scope_id)
            sub = await self._get(conn, event.scope_id, subscription_id)
            bodies = (await conn.execute(select(s.subscription_events.c.body).where(
                      s.subscription_events.c.subscription_id == sub.id))).scalars().all()
            records = [AdmissionDecision.model_validate(b) for b in bodies]
            prior = next((r for r in records if r.event_id == event.id), None)
            if prior is not None and (prior.decision != "defer" or prior.task_id is not None):
                return prior
            tasks = [_task(r) for r in (await conn.execute(select(s.tasks).where(
                     s.tasks.c.scope_id == event.scope_id))).mappings()]
            own = [t.id for t in tasks if t.subscription_id == sub.id]
            usage = Usage()
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            for body in (await conn.execute(select(s.usage_reservations.c.settled).where(
                s.usage_reservations.c.task_id.in_(own), s.usage_reservations.c.settled_at >= start,
                s.usage_reservations.c.settled.is_not(None)))).scalars():
                usage += Usage.model_validate(body)
            record, task = evaluate(sub, tasks, records, usage)
            from ..tasks.proactive_controller import retain_evaluations
            record = retain_evaluations(record, prior)
            if task is not None:
                await conn.execute(s.tasks.insert().values(**_task_row(task)))
                await self._db.append_event(conn, Event(scope_id=task.scope_id, task_id=task.id,
                    type=EventType.TASK_CREATED, payload={"agent_role": str(task.agent_role),
                    "run_lane": str(task.run_lane), "status": str(task.status)}, occurred_at=now, received_at=now))
            values = dict(subscription_id=sub.id, event_id=event.id, scope_id=sub.scope_id,
                          decision=record.decision, task_id=record.task_id, body=_dump(record))
            await conn.execute(insert(s.subscription_events).values(**values).on_conflict_do_update(
                               index_elements=["subscription_id", "event_id"], set_=values))
            return record

    async def results(self, scope_id):
        async with self._db.engine.connect() as conn:
            return [_task(r) for r in (await conn.execute(select(s.tasks).where(s.tasks.c.scope_id == scope_id,
                    s.tasks.c.agent_role == "proactive", s.tasks.c.status == "succeeded"))).mappings()]
