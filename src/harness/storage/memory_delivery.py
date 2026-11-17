"""Atomic inbox, notice deduplication and feedback in the memory store."""
from ..contracts import Event, EventType, NoticeRecord, NoticeReservation, NotFound
from .memory import _copy, _replace


class InMemoryDeliveryRepository:
    def __init__(self, store):
        self._s = store
        store.inbox, store.notices = {}, {}

    async def list(self, scope_id):
        async with self._s.lock:
            return [_copy(i) for i in self._s.inbox.values() if i.scope_id == scope_id]

    async def get(self, scope_id, item_id):
        async with self._s.lock:
            item = self._s.inbox.get(item_id)
            if item is None or item.scope_id != scope_id:
                raise NotFound("inbox item not found")
            return _copy(item)

    async def reserve_notice(self, item, channel, now):
        async with self._s.lock:
            item = self._s.inbox[item.id]
            sub = self._s.subscriptions[item.subscription_id]
            from ..tasks.proactive_controller import active
            if not active(sub, now) or item.valid_until <= now or item.status == "cancelled":
                return NoticeReservation(outcome="suppressed", dedup_key=item.dedup_key, reason="inactive or expired")
            key = (item.scope_id, item.dedup_key)
            previous = self._s.notices.get(key)
            if previous and previous.valid_until > now and previous.severity >= item.score:
                return NoticeReservation(outcome="already_reserved", dedup_key=item.dedup_key)
            self._s.notices[key] = NoticeRecord(scope_id=item.scope_id, dedup_key=item.dedup_key,
                task_id=item.task_id, channel=channel, severity=item.score, valid_until=item.valid_until)
            self._s.inbox[item.id] = _replace(item, notice_reserved=True, mode="digest" if item.mode == "notify" else item.mode)
            return NoticeReservation(outcome="reserved", dedup_key=item.dedup_key, notice_id=item.id, expires_at=item.valid_until)

    async def ack(self, scope_id, item_id, status, label, now):
        async with self._s.lock:
            item = self._s.inbox.get(item_id)
            if item is None or item.scope_id != scope_id:
                raise NotFound("inbox item not found")
            self._s.inbox[item.id] = _replace(item, status=status)
            self._s.notices[(scope_id, item.dedup_key)] = NoticeRecord(scope_id=scope_id,
                dedup_key=item.dedup_key, task_id=item.task_id, channel="inbox",
                severity=item.score, valid_until=item.valid_until)
            if label:
                for key, record in self._s.subscription_events.items():
                    if record.task_id == item.task_id:
                        self._s.subscription_events[key] = _replace(record, backfill_label=label, backfilled_at=now)
            return _copy(self._s.inbox[item.id])


def publish(store, item, now):
    from ..tasks.proactive_controller import active
    sub = store.subscriptions[item.subscription_id]
    if not active(sub, now):
        return
    previous = store.notices.get((item.scope_id, item.dedup_key))
    if previous and previous.valid_until > now and previous.severity >= item.score:
        item = _replace(item, mode="silent")
    store.inbox[item.id] = _copy(item)
    store.append_event(Event(scope_id=item.scope_id, task_id=item.task_id, type=EventType.BACKGROUND_RESULT_READY,
        subject_ref=item.subject_ref, payload={"inbox_id": item.id, "result_ref": item.result_ref,
        "mode": item.mode, "score": item.score, "policy_version": "proactive-delivery@1",
        "valid_until": item.valid_until.isoformat()}, occurred_at=now, received_at=now))
