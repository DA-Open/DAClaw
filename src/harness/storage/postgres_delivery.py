"""Atomic inbox publication and cross-channel notice reservations."""
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from ..contracts import AdmissionDecision, Event, EventType, NoticeReservation, NotFound
from ..proactive_types import InboxItem, NoticeRecord
from . import schema as s
from .postgres import _dump
from .postgres_proactive import lock_scope, PgSubscriptionRepository
from ..tasks.proactive_controller import active


def _item(row):
    return InboxItem.model_validate({**row["body"], "status": row["status"]})


class PgDeliveryRepository:
    def __init__(self, db):
        self._db = db

    async def list(self, scope_id):
        async with self._db.engine.connect() as conn:
            return [_item(r) for r in (await conn.execute(select(s.inbox_items).where(
                    s.inbox_items.c.scope_id == scope_id))).mappings()]

    async def _get(self, conn, scope_id, item_id):
        row = (await conn.execute(select(s.inbox_items).where(s.inbox_items.c.id == item_id,
               s.inbox_items.c.scope_id == scope_id))).mappings().first()
        if row is None:
            raise NotFound("inbox item not found")
        return _item(row)

    async def get(self, scope_id, item_id):
        async with self._db.engine.connect() as conn:
            return await self._get(conn, scope_id, item_id)

    async def reserve_notice(self, item, channel, now):
        async with self._db.engine.begin() as conn:
            await lock_scope(conn, item.scope_id)
            item = await self._get(conn, item.scope_id, item.id)
            sub = await PgSubscriptionRepository(self._db)._get(conn, item.scope_id, item.subscription_id)
            if not active(sub, now) or item.valid_until <= now or item.status == "cancelled":
                return NoticeReservation(outcome="suppressed", dedup_key=item.dedup_key, reason="inactive or expired")
            body = (await conn.execute(select(s.delivery_counters.c.body).where(
                s.delivery_counters.c.scope_id == item.scope_id, s.delivery_counters.c.dedup_key == item.dedup_key))).scalar_one_or_none()
            previous = NoticeRecord.model_validate(body) if body else None
            if previous and previous.valid_until > now and previous.severity >= item.score:
                return NoticeReservation(outcome="already_reserved", dedup_key=item.dedup_key)
            notice = NoticeRecord(scope_id=item.scope_id, dedup_key=item.dedup_key, task_id=item.task_id,
                                  channel=channel, severity=item.score, valid_until=item.valid_until)
            await conn.execute(insert(s.delivery_counters).values(scope_id=item.scope_id, dedup_key=item.dedup_key,
                body=_dump(notice)).on_conflict_do_update(index_elements=["scope_id", "dedup_key"], set_={"body": _dump(notice)}))
            item.notice_reserved = True
            if item.mode == "notify":
                item.mode = "digest"
            await conn.execute(update(s.inbox_items).where(s.inbox_items.c.id == item.id).values(body=_dump(item)))
            return NoticeReservation(outcome="reserved", dedup_key=item.dedup_key, notice_id=item.id, expires_at=item.valid_until)

    async def ack(self, scope_id, item_id, status, label, label_source, now):
        async with self._db.engine.begin() as conn:
            await lock_scope(conn, scope_id)
            item = await self._get(conn, scope_id, item_id)
            item.status = status
            await conn.execute(update(s.inbox_items).where(s.inbox_items.c.id == item.id).values(status=status, body=_dump(item)))
            notice = NoticeRecord(scope_id=scope_id, dedup_key=item.dedup_key, task_id=item.task_id,
                                  channel="inbox", severity=item.score, valid_until=item.valid_until)
            await conn.execute(insert(s.delivery_counters).values(scope_id=scope_id, dedup_key=item.dedup_key,
                body=_dump(notice)).on_conflict_do_update(index_elements=["scope_id", "dedup_key"], set_={"body": _dump(notice)}))
            if label:
                rows = (await conn.execute(select(s.subscription_events).where(s.subscription_events.c.task_id == item.task_id))).mappings().all()
                for row in rows:
                    record = AdmissionDecision.model_validate(row["body"])
                    record.backfill_label, record.label_source, record.backfilled_at = label, label_source, now
                    await conn.execute(update(s.subscription_events).where(
                        s.subscription_events.c.subscription_id == record.subscription_id,
                        s.subscription_events.c.event_id == record.event_id).values(body=_dump(record)))
            return item


async def publish(db, conn, item, now):
    sub = await PgSubscriptionRepository(db)._get(conn, item.scope_id, item.subscription_id)
    if not active(sub, now):
        return
    body = (await conn.execute(select(s.delivery_counters.c.body).where(s.delivery_counters.c.scope_id == item.scope_id,
            s.delivery_counters.c.dedup_key == item.dedup_key))).scalar_one_or_none()
    previous = NoticeRecord.model_validate(body) if body else None
    if previous and previous.valid_until > now and previous.severity >= item.score:
        item.mode = "silent"
    await conn.execute(s.inbox_items.insert().values(id=item.id, scope_id=item.scope_id,
        subscription_id=item.subscription_id, task_id=item.task_id, status=item.status, body=_dump(item)))
    await db.append_event(conn, Event(scope_id=item.scope_id, task_id=item.task_id, type=EventType.BACKGROUND_RESULT_READY,
        subject_ref=item.subject_ref, payload={"inbox_id": item.id, "result_ref": item.result_ref,
        "mode": item.mode, "score": item.score, "policy_version": "proactive-delivery@1",
        "valid_until": item.valid_until.isoformat()}, occurred_at=now, received_at=now))
