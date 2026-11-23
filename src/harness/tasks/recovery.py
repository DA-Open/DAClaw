"""Explicit recovery ticks; scheduler wiring belongs to S1."""

from collections.abc import Callable
from datetime import datetime

from ..config import HarnessConfig
from ..storage.repositories import TaskRepository


class Recovery:
    def __init__(self, tasks: TaskRepository, config: HarnessConfig, clock: Callable[[], datetime]) -> None:
        self._tasks = tasks
        self._config = config
        self._clock = clock

    async def tick(self, now: datetime) -> int:
        return await self._tasks.reclaim_expired(now, self._config.max_attempts)
