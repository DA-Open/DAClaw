"""Worker: claim a task, run one attempt, commit how it ended (spec 1.2, 4.1).

One attempt, in order (spec 4.1 steps 2-8):

1. ``TaskService.claim`` takes a queued task on this worker's lanes, under a
   lease.
2. The task's profile (``agent_profile_ref``, fixed at creation) is resolved.
3. ContextBuilder assembles the input.
4. AgentRunner runs the bounded loop, calling tools only through the
   gateway and asking the verifier at the end.
5. ``TaskService.commit`` stores the outcome. Usage was already settled by
   the Runner and gateway; commit never charges again.

While the attempt runs, a heartbeat task extends the lease every
``heartbeat_interval_sec``. If a heartbeat is refused (another worker took
over, or the lease ran out), the Runner is told through ``lease_lost`` and
stops at its next check by raising ``StaleLease``; this worker then commits
nothing (spec 4.4: 租约失效后拒绝旧执行者的提交). A cancel request seen by the
heartbeat is picked up by the Runner at its next round.

What the worker does NOT do (spec 1.2): share mutable sessions across tasks,
change grants, or keep task state only in memory. Putting tasks with an
expired lease back in the queue is the Scheduler's job (P1).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from ..contracts import (
    AgentRole,
    AttemptLease,
    ErrorCode,
    ExecutionContext,
    HarnessError,
    StaleLease,
    Task,
    TaskError,
    TaskOutcome,
    TaskStatus,
    Actor,
    RunLane,
    utcnow,
)
from ..interfaces import AgentRunner, ContextBuilder
from .service import TaskService

log = logging.getLogger(__name__)


@dataclass
class AttemptReport:
    """What one ``run_once`` did, for logs, tests and the CLI."""

    task_id: str
    attempt_id: str
    status: TaskStatus | None
    committed: bool
    scope_id: str = ""
    error: str | None = None
    result_ref: str | None = None
    heartbeats: int = 0
    lease_lost: bool = False


@dataclass
class _LeaseWatch:
    lost: bool = False
    beats: int = 0
    cancel_seen: bool = False
    errors: list[str] = field(default_factory=list)


def _infra_failure(message: str, *, retryable: bool = False) -> TaskOutcome:
    return TaskOutcome(
        status=TaskStatus.FAILED,
        error=TaskError(code=ErrorCode.INTERNAL, message=message, retryable=retryable),
    )


class Worker:
    """One worker process; it runs one attempt at a time."""

    def __init__(
        self,
        tasks: TaskService,
        builder: ContextBuilder,
        runner: AgentRunner,
        agents,
        *,
        lanes: Iterable[RunLane] = (RunLane.FOREGROUND,),
        heartbeat_interval_sec: float = 15.0,
        worker_id: str | None = None,
        grant_refs: Callable[[Task], list[str]] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.tasks = tasks
        self.builder = builder
        self.runner = runner
        self.agents = agents
        self.lanes = [RunLane(l) for l in lanes]
        if not self.lanes:
            raise ValueError("a worker must serve at least one lane")
        self.heartbeat_interval_sec = heartbeat_interval_sec
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self._grant_refs = grant_refs or (lambda task: [])
        self._clock = clock

    async def run_once(self) -> AttemptReport | None:
        """Claim one task and run one attempt. ``None`` when nothing is queued."""
        lease = await self.tasks.claim(self.worker_id, self.lanes)
        if lease is None:
            return None
        return await self._run(lease)

    async def run_forever(self, *, idle_sleep_sec: float = 1.0, stop: asyncio.Event | None = None) -> None:
        """Keep claiming until ``stop`` is set. Errors in one attempt do not
        stop the worker; they are logged and the task is left to its lease."""
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                report = await self.run_once()
            except Exception:  # noqa: BLE001 - keep serving other tasks
                log.exception("worker %s: attempt failed outside the runner", self.worker_id)
                report = None
            if report is None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=idle_sleep_sec)
                except asyncio.TimeoutError:
                    pass

    # -- one attempt -------------------------------------------------------------

    async def _run(self, lease: AttemptLease) -> AttemptReport:
        task = await self.tasks.get(lease.scope_id, lease.task_id)
        report = AttemptReport(
            task_id=task.id, attempt_id=lease.attempt_id, status=None, committed=False, scope_id=task.scope_id
        )
        watch = _LeaseWatch()
        beat = asyncio.create_task(self._heartbeat(lease, watch))
        try:
            outcome = await self._attempt(task, lease, watch)
        except StaleLease as exc:
            # Another executor owns the task now; committing would be refused
            # anyway and must not be tried (spec 4.4).
            log.warning("worker %s lost the lease on %s: %s", self.worker_id, task.id, exc.message)
            report.lease_lost = True
            report.error = exc.message
            return report
        finally:
            beat.cancel()
            try:
                await beat
            except asyncio.CancelledError:
                pass
            report.heartbeats = watch.beats

        if watch.lost:
            report.lease_lost = True
            report.error = "lease lost while running"
            return report
        try:
            committed = await self.tasks.commit(lease, outcome)
        except StaleLease as exc:
            report.lease_lost = True
            report.error = exc.message
            return report
        report.committed = True
        report.status = committed.status
        report.result_ref = committed.result_ref
        if outcome.error is not None:
            report.error = f"{outcome.error.code}: {outcome.error.message}"
        return report

    async def _attempt(self, task: Task, lease: AttemptLease, watch: _LeaseWatch) -> TaskOutcome:
        """Build and run; turn setup errors into a failed outcome so the task
        does not sit in ``running`` until its lease runs out."""
        try:
            profile = self.agents.resolve_ref(task.agent_profile_ref)
        except HarnessError as exc:
            return _infra_failure(f"profile {task.agent_profile_ref}: {exc.message}")
        execution = ExecutionContext(
            lease=lease,
            actor=Actor(actor_id=task.actor_id, scope_id=task.scope_id),
            agent_role=AgentRole(task.agent_role),
            run_lane=RunLane(task.run_lane),
            grant_refs=self._grant_refs(task),
        )
        try:
            context = await self.builder.build(task, profile, lease)
        except HarnessError as exc:
            return _infra_failure(f"could not build the context: {exc.message}")
        except Exception as exc:  # noqa: BLE001 - e.g. missing instructions file
            log.exception("worker %s: context build failed for %s", self.worker_id, task.id)
            return _infra_failure(f"could not build the context: {type(exc).__name__}: {exc}")
        try:
            return await self.runner.run(context, profile, execution, lease_lost=lambda: watch.lost)
        except StaleLease:
            raise
        except Exception as exc:  # noqa: BLE001 - review of #18: a Runner bug must not leave the task running
            # The Runner normally turns every error into an outcome; reaching
            # here means a bug. Without a Scheduler (P1) nothing would requeue
            # the task, so it is committed as failed. Partial artifacts stay.
            log.exception("worker %s: runner raised for %s", self.worker_id, task.id)
            return _infra_failure(f"runner raised {type(exc).__name__}: {exc}")

    async def _heartbeat(self, lease: AttemptLease, watch: _LeaseWatch) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval_sec)
            try:
                task = await self.tasks.heartbeat(lease)
            except StaleLease as exc:
                watch.lost = True
                watch.errors.append(exc.message)
                return
            except Exception as exc:  # noqa: BLE001 - a flaky store must not kill the attempt
                watch.errors.append(f"{type(exc).__name__}: {exc}")
                log.warning("worker %s: heartbeat failed for %s: %s", self.worker_id, lease.task_id, exc)
                continue
            watch.beats += 1
            if task.cancel_requested_at is not None:
                watch.cancel_seen = True
