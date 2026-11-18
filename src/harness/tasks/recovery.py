"""Explicit recovery ticks; scheduler wiring belongs to S1."""

from collections.abc import Callable
from datetime import datetime, timedelta

from ..config import HarnessConfig
from ..contracts import Event
from ..storage.repositories import ActionRepository, EventRepository, TaskRepository
from ..tools.gateway import ToolGateway, reconciliation_history


def needs_manual_confirmation(history: list[Event]) -> bool:
    return len(history) >= 5 or any(event.payload["status"] == "unsupported" for event in history)


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
                if needs_manual_confirmation(attempts):
                    continue
                if attempts and now < attempts[-1].occurred_at + timedelta(minutes=2 ** (len(attempts) - 1)):
                    continue
                await self._gateway.reconcile(task, action)
        return reclaimed
