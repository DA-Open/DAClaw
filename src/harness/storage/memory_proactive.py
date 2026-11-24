"""Subscription transactions over the existing in-memory store."""
from datetime import timedelta

from ..contracts import (
    AgentRole, Event, EventType, NotFound, SubscriptionStatus, TaskStatus, Usage,
)
from .memory import _copy, _replace


class InMemorySubscriptionRepository:
    def __init__(self, store):
        self._s = store
        store.subscriptions = {}
        store.subscription_events = {}

    async def create(self, subscription):
        async with self._s.lock:
            if subscription.id in self._s.subscriptions:
                raise ValueError("subscription ID already exists")
            self._s.subscriptions[subscription.id] = _copy(subscription)
            return _copy(subscription)

    def _get(self, scope_id, subscription_id):
        sub = self._s.subscriptions.get(subscription_id)
        if sub is None or sub.scope_id != scope_id:
            raise NotFound("subscription not found")
        return sub

    async def get(self, scope_id, subscription_id):
        async with self._s.lock:
            return _copy(self._get(scope_id, subscription_id))

    async def list_for_scope(self, scope_id, *, status=None):
        async with self._s.lock:
            return [_copy(s) for s in self._s.subscriptions.values()
                    if s.scope_id == scope_id and (status is None or s.status == status)]

    async def list_all(self):
        async with self._s.lock:
            return [_copy(s) for s in self._s.subscriptions.values()]

    async def decisions(self, scope_id, subscription_id):
        async with self._s.lock:
            self._get(scope_id, subscription_id)
            return [_copy(r) for r in self._s.subscription_events.values() if r.subscription_id == subscription_id]

    async def label_decision(self, scope_id, subscription_id, task_id, label, now):
        async with self._s.lock:
            self._get(scope_id, subscription_id)
            for key, record in self._s.subscription_events.items():
                if record.subscription_id == subscription_id and record.task_id == task_id:
                    updated = _replace(record, backfill_label=label, label_source="explicit")
                    self._s.subscription_events[key] = updated
                    return _copy(updated)
            raise NotFound("admission decision not found")

    async def event(self, scope_id, event_id):
        async with self._s.lock:
            for event in self._s.events:
                if event.id == event_id and event.scope_id == scope_id:
                    return _copy(event)
            raise NotFound("event not found")

    async def latest_event(self, scope_id, subject_ref):
        async with self._s.lock:
            return next((_copy(e) for e in reversed(self._s.events)
                         if e.scope_id == scope_id and e.subject_ref == subject_ref
                         and e.type == EventType.EXTERNAL and e.source != "scheduler"), None)

    async def changes(self, scope_id, subject_ref, since, until, *, source):
        async with self._s.lock:
            events = [e for e in self._s.events if e.scope_id == scope_id and e.subject_ref == subject_ref
                      and e.type == EventType.EXTERNAL and e.source == source and since < e.occurred_at <= until]
            return [_copy(e) for e in sorted(events, key=lambda e: (e.occurred_at, e.seq))]

    async def pending_events(self):
        async with self._s.lock:
            deferred = {r.event_id for r in self._s.subscription_events.values() if r.decision == "defer"}
            return [_copy(e) for e in self._s.events if e.type == EventType.EXTERNAL
                    and (e.processed_at is None or e.id in deferred)]

    async def mark_processed(self, scope_id, event_id, now):
        async with self._s.lock:
            for i, event in enumerate(self._s.events):
                if event.id == event_id and event.scope_id == scope_id:
                    self._s.events[i] = _replace(event, processed_at=now)
                    return
            raise NotFound("event not found")

    async def admit(self, event, subscription_id, evaluate, now):
        async with self._s.lock:
            sub = self._get(event.scope_id, subscription_id)
            key = (subscription_id, event.id)
            prior = self._s.subscription_events.get(key)
            if prior is not None and (prior.decision != "defer" or prior.task_id is not None):
                return _copy(prior)
            tasks = [t for t in self._s.tasks.values() if t.scope_id == event.scope_id]
            records = [r for r in self._s.subscription_events.values() if r.subscription_id == sub.id]
            own = {t.id for t in tasks if t.subscription_id == sub.id}
            usage = Usage()
            for r in self._s.reservations.values():
                if r.task_id in own and r.settled is not None and r.settled_at.date() == now.date():
                    usage += r.settled
            record, task = evaluate(_copy(sub), [_copy(t) for t in tasks], [_copy(r) for r in records], usage)
            from ..tasks.proactive_controller import retain_evaluations
            record = retain_evaluations(record, prior)
            if task is not None:
                self._s.tasks[task.id] = _copy(task)
                self._s.task_keys[(task.scope_id, task.request_key)] = task.id
                self._s.append_event(Event(scope_id=task.scope_id, task_id=task.id,
                    type=EventType.TASK_CREATED, payload={"agent_role": str(task.agent_role),
                    "run_lane": str(task.run_lane), "status": str(task.status)}, occurred_at=now, received_at=now))
            self._s.subscription_events[key] = _copy(record)
            return _copy(record)

    async def results(self, scope_id):
        async with self._s.lock:
            return [_copy(t) for t in self._s.tasks.values() if t.scope_id == scope_id
                    and t.agent_role == AgentRole.PROACTIVE and t.status == TaskStatus.SUCCEEDED]
