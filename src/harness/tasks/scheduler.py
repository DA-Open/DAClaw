"""UTC cron triggers, durable deferred events and the shared recovery tick."""
import asyncio
from datetime import timedelta

from ..contracts import Event, EventType, utcnow
from .proactive_controller import active
from .recovery import Recovery


class Scheduler:
    def __init__(self, repos, controller, recovery: Recovery, *, clock=utcnow):
        self.repos, self.controller, self.recovery, self.clock = repos, controller, recovery, clock

    async def tick(self):
        now = self.clock()
        recovered = await self.recovery.tick(now)
        for sub in await self.repos.subscriptions.list_all():
            if not sub.schedule or not active(sub, now):
                continue
            from croniter import croniter
            due = croniter(sub.schedule, now + timedelta(microseconds=1)).get_prev(type(now))
            if due < sub.created_at:
                continue
            source = await self.repos.subscriptions.latest_event(sub.scope_id, sub.subject_ref)
            if source is None:
                continue
            await self.controller.on_event(Event(scope_id=sub.scope_id, type=EventType.EXTERNAL,
                source="scheduler", source_event_id=f"{sub.id}:{due.isoformat()}", subject_ref=sub.subject_ref,
                payload={**source.payload, "kind": "schedule", "subscription_id": sub.id}, occurred_at=due, received_at=now))
        for event in await self.repos.subscriptions.pending_events():
            await self.controller.on_event(event)
        return recovered

    async def run_forever(self, *, stop, interval=5):
        while not stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop.wait(), interval)
            except asyncio.TimeoutError:
                continue
