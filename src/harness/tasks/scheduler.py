"""UTC cron triggers, durable deferred events and the shared recovery tick."""
import asyncio
import logging
from datetime import datetime, timedelta

from ..contracts import Event, EventType, utcnow
from ..personal.calendar_source import MockCalendarSource
from .proactive_controller import active, resources_allowed
from .recovery import Recovery

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, repos, controller, recovery: Recovery, calendar: MockCalendarSource, *, clock=utcnow):
        self.repos, self.controller, self.recovery, self.clock = repos, controller, recovery, clock
        self.calendar = calendar

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
            if sub.capability_ref == "personal_watch@1":
                await self._personal_window(sub, due, now)
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

    async def _personal_window(self, sub, due, now):
        records = await self.repos.subscriptions.decisions(sub.scope_id, sub.id)
        since = max((datetime.fromisoformat(r.input_snapshot["until"]) for r in records
                     if r.input_snapshot.get("kind") == "personal_window"), default=sub.created_at)
        if since >= due:
            return
        if resources_allowed(sub, self.controller.policy):
            try:
                changes = self.calendar.changes(sub.subject_ref, since, due)
            except ValueError as exc:
                log.warning("Calendar subscription %s skipped: %s", sub.id, exc)
                return
            for change in changes:
                appended = await self.repos.events.append(Event(
                    scope_id=sub.scope_id, type=EventType.EXTERNAL, source="mock_calendar",
                    source_event_id=f"{change.item_id}@{change.version}", subject_ref=sub.subject_ref,
                    payload=change.model_dump(mode="json"), occurred_at=change.occurred_at, received_at=now))
                await self.repos.subscriptions.mark_processed(sub.scope_id, appended.id, now)
        await self.controller.on_event(Event(
            scope_id=sub.scope_id, type=EventType.EXTERNAL, source="scheduler",
            source_event_id=f"{sub.id}:{due.isoformat()}", subject_ref=sub.subject_ref,
            payload={"kind": "personal_window", "subscription_id": sub.id,
                     "since": since.isoformat(), "until": due.isoformat()}, occurred_at=due, received_at=now))

    async def run_forever(self, *, stop, interval=5):
        while not stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop.wait(), interval)
            except asyncio.TimeoutError:
                continue
