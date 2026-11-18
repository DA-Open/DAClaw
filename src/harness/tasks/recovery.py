"""Explicit recovery ticks; scheduler wiring belongs to S1."""

from collections.abc import Callable
from datetime import datetime, timedelta

from ..config import HarnessConfig
from ..contracts import Event
from ..storage.repositories import ActionRepository, EventRepository, TaskRepository, round_attempts
from ..tools.gateway import ToolGateway, reconciliation_history

RECONCILE_BACKOFF_MINUTES = (1, 2, 4, 8, 16)


def needs_manual_confirmation(history: list[Event]) -> bool:
    count = round_attempts(event.payload["status"] for event in history)
    return count >= 6 or any(event.payload["status"] == "unsupported" for event in history[len(history) - count:])


class Recovery:
    def __init__(
        self, tasks: TaskRepository, config: HarnessConfig, clock: Callable[[], datetime], *,
        gateway: ToolGateway, actions: ActionRepository, events: EventRepository,
    ) -> None:
        self._tasks = tasks
        self._config = config
        self._clock = clock
        self._gateway = gateway
        self._actions = actions
        self._events = events

    async def tick(self, now: datetime) -> int:
        reclaimed = await self._tasks.reclaim_expired(now, self._config.max_attempts)
        for task in await self._tasks.list_reconciliation_candidates():
            history = await reconciliation_history(self._events, task)
            actions = await self._actions.list_unsettled(task.id)
            if not actions:
                await self._tasks.release_reconciliation(task.scope_id, task.id, now)
            for action in actions:
                attempts = [event for event in history if event.payload["action_id"] == action.id]
                count = round_attempts(event.payload["status"] for event in attempts)
                if needs_manual_confirmation(attempts):
                    continue
                due = (
                    attempts[-1].occurred_at + timedelta(minutes=RECONCILE_BACKOFF_MINUTES[count - 1])
                    if count else action.updated_at + timedelta(seconds=self._config.reconcile_grace_sec)
                )
                if now < due:
                    continue
                await self._gateway.reconcile(task, action, count + 1)
        return reclaimed
