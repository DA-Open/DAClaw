"""TaskService: the only writer of task state (spec 2, 4.1, 5.2).

A thin layer over ``storage.TaskRepository`` that adds what the repository
does not know: deployment limits, which capability packs and profiles this
deployment has, the lease length, the clock, and where checkpoints are
stored. API and ProactiveController create tasks here; Workers claim,
heartbeat, checkpoint and commit here. Nothing else changes task status.

Checks on ``create`` (spec 5.1 POST /tasks, 3.1):

* ``limits`` above the deployment caps -> ``LimitsExceeded`` (HTTP 400);
* an unknown ``capability_ref`` or ``agent_profile_ref`` -> ``NotFound``
  (HTTP 400): a task must not be created for something this deployment
  cannot run;
* a check in ``acceptance`` that the pack does not offer -> ``ValueError``
  (HTTP 400) instead of a task that can never pass verification;
* the same ``request_key`` with the same content returns the original task;
  with other content raises ``IdempotencyConflict`` (HTTP 409).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol
from datetime import datetime

import logging

from ..config import HarnessConfig
from ..contracts import (
    HarnessError,
    Actor,
    ArtifactInput,
    AttemptLease,
    Checkpoint,
    NotFound,
    RunLane,
    Task,
    TaskOrigin,
    TaskOutcome,
    TaskRequest,
    canonical_json,
    utcnow,
)
from ..interfaces import ArtifactStore
from ..storage.repositories import TaskRepository

log = logging.getLogger(__name__)


class UnknownCheck(ValueError):
    """The acceptance asks for a check the capability pack does not offer."""


class AfterCommit(Protocol):
    """Runs after a successful commit; never inside the storage transaction."""

    async def __call__(self, task: Task, outcome: TaskOutcome) -> None: ...


class TaskService:
    """``interfaces.TaskService``."""

    def __init__(
        self,
        tasks: TaskRepository,
        config: HarnessConfig,
        *,
        artifacts: ArtifactStore | None = None,
        capabilities=None,
        agents=None,
        clock: Callable[[], datetime] = utcnow,
        delivery=None,
        after_commit: list[AfterCommit],
    ) -> None:
        self._tasks = tasks
        self._config = config
        self._artifacts = artifacts
        self._capabilities = capabilities
        self._agents = agents
        self._clock = clock
        self._delivery = delivery
        self._after_commit = list(after_commit)

    # -- create --------------------------------------------------------------

    async def create(self, request: TaskRequest, actor: Actor, origin: TaskOrigin) -> Task:
        result = await self._tasks.create(self.prepare(request, actor, origin))
        return result.task

    def prepare(self, request: TaskRequest, actor: Actor, origin: TaskOrigin) -> Task:
        """Validate a task without IO; admission inserts it with its decision."""
        self._config.caps.check(request.limits)
        self._check_capability(request)
        self._check_profile(origin)
        return Task.new(request, actor, origin, now=self._clock())

    def _check_capability(self, request: TaskRequest) -> None:
        if self._capabilities is None:
            return
        pack = self._capabilities.resolve(request.capability_ref)  # NotFound if unknown
        offered = set(pack.verifiers)
        unknown = [c.id for c in request.acceptance.checks if c.id not in offered]
        if unknown:
            raise UnknownCheck(
                f"{pack.ref} does not offer the check(s) {', '.join(unknown)}; "
                f"it offers {', '.join(pack.verifiers) or 'none'}"
            )

    def _check_profile(self, origin: TaskOrigin) -> None:
        if self._agents is None:
            return
        self._agents.resolve_ref(origin.agent_profile_ref)  # NotFound if unknown

    async def get(self, scope_id: str, task_id: str) -> Task:
        return await self._tasks.get(scope_id, task_id)

    # -- worker side -------------------------------------------------------------

    async def claim(self, worker_id: str, allowed_lanes: Iterable[RunLane]) -> AttemptLease | None:
        lanes = [RunLane(l) for l in allowed_lanes]
        if not lanes:
            raise ValueError("a worker must serve at least one lane")
        return await self._tasks.claim(worker_id, lanes, self._config.lease_ttl_sec, self._clock(),
                                       background_scope_limit=self._config.background_scope_limit)

    async def heartbeat(self, lease: AttemptLease) -> Task:
        return await self._tasks.heartbeat(lease, self._config.lease_ttl_sec, self._clock())

    async def checkpoint(self, lease: AttemptLease, state: Checkpoint) -> None:
        """Store the checkpoint as an artifact, then point the task at it."""
        if self._artifacts is None:
            raise NotFound("this TaskService has no ArtifactStore for checkpoints")
        ref = await self._artifacts.put(
            ArtifactInput(
                scope_id=lease.scope_id,
                task_id=None,
                name=f"{lease.task_id}/checkpoint/{state.step_id}",
                media_type="application/json",
                content=canonical_json(state.model_dump(mode="json")).encode("utf-8"),
            )
        )
        await self._tasks.checkpoint(lease, ref.id, self._clock())

    async def commit(self, lease: AttemptLease, outcome: TaskOutcome) -> Task:
        if self._delivery is not None:
            task = await self._tasks.get(lease.scope_id, lease.task_id)
            inbox = await self._delivery.prepare(task, outcome)
            if inbox is not None:
                committed = await self._tasks.commit(lease, outcome, self._clock(), inbox=inbox)
                await self._after_commit_hooks(committed, outcome)
                return committed
        committed = await self._tasks.commit(lease, outcome, self._clock())
        await self._after_commit_hooks(committed, outcome)
        return committed

    async def _after_commit_hooks(self, task: Task, outcome: TaskOutcome) -> None:
        """Hooks run outside the transaction; a failing hook never fails the commit."""
        for hook in self._after_commit:
            try:
                await hook(task, outcome)
            except (HarnessError, OSError, ValueError) as exc:
                log.warning("after-commit hook %r failed for task %s: %s", hook, task.id, exc)

    async def request_cancel(self, scope_id: str, task_id: str, reason: str) -> Task:
        return await self._tasks.request_cancel(scope_id, task_id, reason, self._clock())

    async def revoke_subscription(self, scope_id: str, subscription_id: str):
        return await self._tasks.revoke_subscription(scope_id, subscription_id, self._clock())
