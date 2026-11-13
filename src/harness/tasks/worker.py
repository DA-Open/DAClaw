"""Worker: claim a queued task, run one attempt, commit the outcome.

The order is fixed (dev log, item 3): ``claim`` → ``ContextBuilder.build`` →
``AgentRunner.run`` → ``commit``. A separate heartbeat task keeps the lease
alive while the model works; its return value is the task, so the worker also
sees a cancel request. The Runner reads the task every round and stops with a
``cancelled`` outcome at the next one, which the worker then commits.

Two rules from the spec shape the error path:

* Runner raises ``StaleLease``: this attempt is void and must not commit
  (spec 4.4); the task stays as it is.
* A heartbeat that gets ``StaleLease`` marks the lease lost, so a run that
  finishes afterwards is not committed either.

Cancellation that arrives after the Runner's last round is left to
``TaskRepository.commit``: a ``CANCELLING`` task refuses ``succeeded`` with
``InvalidTransition``, so a cancel is never silently overwritten.

``python -m harness.tasks.worker --once`` runs one poll on in-memory storage.
That smoke checks the wiring (claim, heartbeat, policy, builder); a real model
comes from ``DARAOS_HARNESS_MODEL_*`` and a real verifier from the results
module (PR-F), which this module still has to be given.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import uuid
from collections.abc import Iterable

from ..config import HarnessConfig
from ..contracts import (
    Actor,
    AgentProfile,
    AgentRole,
    AttemptLease,
    ExecutionContext,
    HarnessError,
    NotFound,
    RunLane,
    StaleLease,
    Task,
    TaskStatus,
    split_versioned_ref,
)
from ..interfaces import AgentRegistry, AgentRunner, ContextBuilder
from ..storage.artifacts import InMemoryArtifactStore
from ..storage.memory import InMemoryRepositories
from ..tools.catalog import PolicyToolCatalog
from ..tools.connectors.files import ReadFileConnector, WriteFileConnector
from ..tools.gateway import ToolGateway
from ..tools.policy import ToolPolicy
from .service import TaskService

log = logging.getLogger(__name__)

DEFAULT_PROFILES = (
    AgentProfile(
        role=AgentRole.MAIN,
        version="1",
        instructions_ref="main@1",
        tool_policy_ref="main_tools@1",
        default_lane=RunLane.FOREGROUND,
        default_delivery_mode="reply",
    ),
    AgentProfile(
        role=AgentRole.PROACTIVE,
        version="1",
        instructions_ref="proactive@1",
        tool_policy_ref="proactive_tools@1",
        default_lane=RunLane.BACKGROUND,
        default_delivery_mode="silent",
    ),
)


class StaticAgentRegistry:
    """``AgentRegistry`` over a fixed set of profiles.

    Profiles and capability packs are PR-F; until they are loaded from disk
    this is what the worker resolves ``task.agent_profile_ref`` with.
    """

    def __init__(self, profiles: Iterable[AgentProfile] = DEFAULT_PROFILES) -> None:
        self._profiles = {(p.role, p.version): p for p in profiles}

    def resolve(self, role: AgentRole, version: str) -> AgentProfile:
        try:
            return self._profiles[(role, version)]
        except KeyError:
            raise NotFound(f"no agent profile {role}@{version}") from None


class Worker:
    def __init__(
        self,
        tasks: TaskService,
        builder: ContextBuilder,
        runner: AgentRunner,
        registry: AgentRegistry,
        *,
        worker_id: str,
        lanes: Iterable[RunLane] | None = None,
        heartbeat_interval_sec: float | None = None,
    ) -> None:
        self._tasks = tasks
        self._builder = builder
        self._runner = runner
        self._registry = registry
        self._worker_id = worker_id
        self._lanes = list(lanes) if lanes is not None else [RunLane.FOREGROUND]
        self._heartbeat_interval = 15.0 if heartbeat_interval_sec is None else heartbeat_interval_sec
        self._lease_lost = False
        self._cancel_requested = False
        self._last_task: Task | None = None

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def cancel_requested(self) -> bool:
        """True once a heartbeat saw a cancel request for the running task."""
        return self._cancel_requested

    async def run_once(self) -> Task | None:
        """Claim at most one task and run it; None when the queue is empty."""
        lease = await self._tasks.claim(self._worker_id, self._lanes)
        if lease is None:
            return None
        return await self.run_lease(lease)

    async def run_lease(self, lease: AttemptLease) -> Task | None:
        """Run one claimed attempt. None means "nothing committed"."""
        self._lease_lost = False
        self._cancel_requested = False
        self._last_task = None
        task = await self._tasks.get(lease.scope_id, lease.task_id)
        execution = ExecutionContext(
            lease=lease,
            actor=Actor(actor_id=task.actor_id, scope_id=task.scope_id),
            agent_role=task.agent_role,
            run_lane=task.run_lane,
        )
        profile = self._profile_for(task)
        heartbeat = asyncio.create_task(self._beat(lease))
        try:
            context = await self._builder.build(task, profile, lease)
            outcome = await self._runner.run(
                context,
                profile,
                execution,
                lease_lost=self._lease_is_lost,
            )
        except StaleLease:
            log.warning("task %s: lease lost while running; not committing", lease.task_id)
            return None
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        if self._lease_lost:
            log.warning("task %s: heartbeat lost the lease; not committing", lease.task_id)
            return None
        try:
            return await self._tasks.commit(lease, outcome)
        except StaleLease:
            log.warning("task %s: lease lost before commit; not committing", lease.task_id)
            return None

    async def serve_forever(self, interval: float = 2.0) -> None:
        while True:
            try:
                handled = await self.run_once()
            except Exception:  # noqa: BLE001 - one bad attempt must not stop the worker
                log.exception("worker %s: attempt failed", self._worker_id)
                handled = None
            if handled is None:
                await asyncio.sleep(interval)

    # -- internals ------------------------------------------------------------

    def _profile_for(self, task: Task) -> AgentProfile:
        _, version = split_versioned_ref(task.agent_profile_ref)
        return self._registry.resolve(task.agent_role, version)

    def _lease_is_lost(self) -> bool:
        return self._lease_lost

    async def _beat(self, lease: AttemptLease) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            try:
                task = await self._tasks.heartbeat(lease)
            except HarnessError:
                self._lease_lost = True
                return
            self._last_task = task
            if task.status is TaskStatus.CANCELLING or task.cancel_requested_at is not None:
                self._cancel_requested = True


# -- process entry point ------------------------------------------------------


def _model_from_env():
    """The model named by ``DARAOS_HARNESS_MODEL_*``, else an empty script."""
    from ..runtime.models import ModelConfig, ScriptedModelAdapter, create_adapter

    if os.environ.get("DARAOS_HARNESS_MODEL_NAME"):
        return create_adapter(ModelConfig.from_env())
    return ScriptedModelAdapter([])


class _UnconfiguredVerifier:
    """Stand-in for PR-F's Verifier, so the smoke run can be wired."""

    async def verify(self, result, task):
        raise HarnessError("no verifier is configured yet (results module, PR-F)")


def build_worker(
    config: HarnessConfig,
    *,
    worker_id: str,
    lanes: Iterable[RunLane] | None = None,
) -> Worker:
    """In-memory runtime for the ``--once`` smoke and local development."""
    from ..context.builder import SimpleContextBuilder, StaticInstructions
    from ..runtime.runner import AgentRunner

    repos = InMemoryRepositories()
    store = InMemoryArtifactStore(repos)
    tasks = TaskService(repos, config, artifacts=store)
    policy = ToolPolicy(
        role_tools={
            AgentRole.MAIN.value: {"files.read", "files.write"},
            AgentRole.PROACTIVE.value: {"files.read"},
        }
    )
    connectors = {
        "files.read": ReadFileConnector(config.artifact_root, artifacts=store),
        "files.write": WriteFileConnector(config.artifact_root, store),
    }
    gateway = ToolGateway(repos, policy, connectors, store)
    builder = SimpleContextBuilder(
        repos,
        instructions=StaticInstructions({"main@1": "", "proactive@1": ""}),
        tools=PolicyToolCatalog.from_connectors(connectors, policy),
    )
    runner = AgentRunner(
        repos,
        _model_from_env(),
        gateway,
        _UnconfiguredVerifier(),
        store,
        reminders=config.budget_reminder,
    )
    return Worker(
        tasks,
        builder,
        runner,
        StaticAgentRegistry(),
        worker_id=worker_id,
        lanes=lanes,
        heartbeat_interval_sec=config.heartbeat_interval_sec,
    )


async def _main(args: argparse.Namespace) -> None:
    config = HarnessConfig.from_env()
    worker = build_worker(
        config,
        worker_id=args.worker_id or f"worker-{uuid.uuid4().hex[:8]}",
        lanes=[RunLane(args.lane)],
    )
    if args.once:
        task = await worker.run_once()
        if task is None:
            print(f"worker {worker.worker_id}: no queued task in the {args.lane} lane")
        else:
            print(f"worker {worker.worker_id}: task {task.id} is {task.status}")
        return
    await worker.serve_forever(interval=args.interval)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Harness worker")
    parser.add_argument("--once", action="store_true", help="claim and run at most one task")
    parser.add_argument("--lane", default="foreground", choices=[str(lane) for lane in RunLane])
    parser.add_argument("--worker-id", default="")
    parser.add_argument("--interval", type=float, default=2.0)
    asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    main()
