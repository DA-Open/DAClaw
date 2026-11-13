"""TaskService: the only writer of task state (spec 2, 5.2).

Every method is a thin, explicit call into ``storage.repositories``: the
transitions, idempotency and lease rules live there, so the in-memory and the
PostgreSQL implementations stay interchangeable. This class adds what the
repository must not know about:

* deployment caps on the limits a caller asks for (spec 5.1);
* ``origin`` from the trusted caller, via ``Task.new`` (spec 3.1);
* the checkpoint payload, which is content: it goes to ArtifactStore first and
  only its reference is written to the task (spec 4.4).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from ..config import HarnessConfig
from ..contracts import (
    Actor,
    ArtifactInput,
    AttemptLease,
    Checkpoint,
    RunLane,
    Task,
    TaskOrigin,
    TaskOutcome,
    TaskRequest,
    canonical_json,
    utcnow,
)
from ..interfaces import ArtifactStore
from ..storage.artifacts import InMemoryArtifactStore
from ..storage.repositories import Repositories


class TaskService:
    def __init__(
        self,
        repos: Repositories,
        config: HarnessConfig,
        *,
        artifacts: ArtifactStore | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._repos = repos
        self._config = config
        self._artifacts: ArtifactStore = artifacts or InMemoryArtifactStore(repos)
        self._clock = clock

    # -- creation -------------------------------------------------------------

    async def create(self, request: TaskRequest, actor: Actor, origin: TaskOrigin) -> Task:
        """Create a task, or return the one already stored under the same
        ``(scope_id, request_key)`` and request hash (idempotent, spec 3.2).

        ``limits`` come from the request, bounded by the deployment caps;
        ``origin`` (role, lane, trigger) comes from the caller, never from the
        request body (spec 3.1).
        """
        self._config.caps.check(request.limits)
        task = Task.new(request, actor, origin, now=self._clock())
        created = await self._repos.tasks.create(task)
        return created.task

    # -- reads ----------------------------------------------------------------

    async def get(self, scope_id: str, task_id: str) -> Task:
        return await self._repos.tasks.get(scope_id, task_id)

    # -- attempts -------------------------------------------------------------

    async def claim(self, worker_id: str, allowed_lanes: list[RunLane]) -> AttemptLease | None:
        return await self._repos.tasks.claim(
            worker_id,
            allowed_lanes,
            self._config.lease_ttl_sec,
            self._clock(),
        )

    async def heartbeat(self, lease: AttemptLease) -> Task:
        return await self._repos.tasks.heartbeat(
            lease,
            self._config.lease_ttl_sec,
            self._clock(),
        )

    async def checkpoint(self, lease: AttemptLease, state: Checkpoint) -> None:
        """Store the checkpoint content, then point the task at it.

        The artifact is not listed among the task's work artifacts
        (``task_id`` empty), the same rule the Runner follows for trajectory
        content; ``Task.checkpoint_ref`` is how it is found again.
        """
        ref = await self._artifacts.put(
            ArtifactInput(
                scope_id=lease.scope_id,
                task_id=None,
                name=f"{lease.task_id}/checkpoint",
                media_type="application/json",
                content=canonical_json(state).encode("utf-8"),
            )
        )
        await self._repos.tasks.checkpoint(lease, ref.id, self._clock())

    async def commit(self, lease: AttemptLease, outcome: TaskOutcome) -> Task:
        """Commit the outcome verbatim; verification and transitions are
        checked by the repository (spec 4.3, 6.3)."""
        return await self._repos.tasks.commit(lease, outcome, self._clock())

    # -- cancellation ---------------------------------------------------------

    async def request_cancel(self, scope_id: str, task_id: str, reason: str) -> Task:
        return await self._repos.tasks.request_cancel(
            scope_id,
            task_id,
            reason,
            self._clock(),
        )
