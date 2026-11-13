"""ToolGateway: policy, action ledger and tool budget for every tool call.

The steps of spec 6.2:

1. A call that already has an action under ``(task_id, logical_key)`` is not
   run again: same arguments return the recorded outcome, different arguments
   raise ``ActionConflict``.
2. The role whitelist ∩ user whitelist decides allowed / denied / needs
   approval. A denied call runs nothing and spends nothing.
3. Registering the action and reserving one tool call are one repository
   ``register(..., reserve=...)`` call, so parallel callers cannot spend the
   last tool call twice.
4. The connector gets an ``AuthorizedCall`` (argument reference, never a
   secret) and returns a ``ToolResult``; unexpected connector errors become
   ``unknown`` for tools with side effects and ``failed`` for read-only ones.
5. Small results go back in ``ToolOutcome.summary``; the full result, the
   arguments, and any long content live in ArtifactStore and are referenced.

A tool that returns ``now succeeded`` for an action the ledger has as
``running`` (a worker died mid-call) is answered with ``unknown``, so the
Runner saves a checkpoint and waits for reconciliation instead of running a
possibly-repeated side effect.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from ..contracts import (
    Action,
    ActionConflict,
    ActionStatus,
    ArtifactInput,
    AuthorizedCall,
    ErrorCode,
    Event,
    EventType,
    ExecutionContext,
    HarnessError,
    StaleLease,
    Task,
    TaskError,
    TaskStatus,
    ToolCall,
    ToolOutcome,
    ToolResult,
    ToolStatus,
    Usage,
    canonical_json,
    utcnow,
)
from ..interfaces import ArtifactStore, Connector
from ..storage.repositories import Repositories
from .policy import PolicyDecision, ToolPolicy

log = logging.getLogger(__name__)


class ToolError(HarnessError):
    """The gateway cannot run the call at all (unknown tool, bad call).

    ``HarnessError.__init__`` takes details, not a code, so a code needs a
    subclass until contracts grows one.
    """

    code = ErrorCode.TOOL_ERROR


# Longest summary handed back to the model; larger results are in ArtifactStore.
MAX_SUMMARY_CHARS = 2000

_ACTION_STATUS = {
    ToolStatus.SUCCEEDED: ActionStatus.SUCCEEDED,
    ToolStatus.FAILED: ActionStatus.FAILED,
    ToolStatus.UNKNOWN: ActionStatus.UNKNOWN,
}

# What a stored action means when its result artifact is gone or unreadable.
_STATUS_FROM_ACTION = {
    ActionStatus.PLANNED: ToolStatus.NEEDS_APPROVAL,
    ActionStatus.RUNNING: ToolStatus.UNKNOWN,
    ActionStatus.SUCCEEDED: ToolStatus.SUCCEEDED,
    ActionStatus.FAILED: ToolStatus.FAILED,
    ActionStatus.UNKNOWN: ToolStatus.UNKNOWN,
}


class ToolGateway:
    def __init__(
        self,
        repos: Repositories,
        policy: ToolPolicy,
        connectors: Mapping[str, Connector],
        artifacts: ArtifactStore,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._repos = repos
        self._policy = policy
        self._connectors = dict(connectors)
        self._artifacts = artifacts
        self._clock = clock

    async def invoke(self, execution: ExecutionContext, call: ToolCall) -> ToolOutcome:
        if not call.logical_key:
            raise ToolError("tool calls need a logical_key, which the Runner sets")
        lease = execution.lease
        task = await self._ensure_current(execution)

        existing = await self._repos.actions.get(lease.task_id, call.logical_key)
        if existing is not None:
            if existing.request_hash != call.request_hash():
                raise ActionConflict(
                    f"logical key {call.logical_key} reused with different arguments",
                    action_id=existing.id,
                )
            return await self._reuse(existing, execution)

        decision = self._policy.authorize(execution.agent_role, execution.actor.scope_id, call.tool)
        if decision is PolicyDecision.DENIED:
            message = (
                f"tool {call.tool!r} is not allowed for role "
                f"{execution.agent_role} in scope {execution.actor.scope_id}"
            )
            await self._event(execution, call, None, ToolStatus.DENIED)
            return ToolOutcome(
                status=ToolStatus.DENIED,
                summary=message,
                error=TaskError(code=ErrorCode.FORBIDDEN, message=message),
            )

        connector = self._connectors.get(call.tool)
        if connector is None:
            raise ToolError(f"no connector registered for tool {call.tool!r}")

        args_ref = await self._store_json(
            task, execution, f"tool_args/{call.logical_key}", {"tool": call.tool, "args": call.args}
        )

        if decision is PolicyDecision.NEEDS_APPROVAL:
            registration = await self._repos.actions.register(
                self._action(execution, call, connector, args_ref, ActionStatus.PLANNED)
            )
            assert registration.action is not None  # no reserve, nothing to refuse
            await self._event(execution, call, registration.action.id, ToolStatus.NEEDS_APPROVAL)
            return ToolOutcome(
                action_id=registration.action.id,
                status=ToolStatus.NEEDS_APPROVAL,
                summary=f"tool {call.tool!r} needs approval before it can run",
            )

        registration = await self._repos.actions.register(
            self._action(execution, call, connector, args_ref, ActionStatus.RUNNING),
            reserve=Usage(tool_calls=1),
        )
        if registration.action is None:
            exceeded = ", ".join(registration.budget.exceeded) if registration.budget else "max_tool_calls"
            message = f"no tool call budget left ({exceeded})"
            await self._event(execution, call, None, ToolStatus.DENIED)
            return ToolOutcome(
                status=ToolStatus.DENIED,
                summary=message,
                error=TaskError(code=ErrorCode.BUDGET_EXHAUSTED, message=message),
            )
        if not registration.created:
            return await self._reuse(registration.action, execution)

        action = registration.action
        result = await self._execute(connector, execution, call, action)
        return await self._record(task, execution, call, action, result)

    # -- execution ------------------------------------------------------------

    async def _execute(
        self,
        connector: Connector,
        execution: ExecutionContext,
        call: ToolCall,
        action: Action,
    ) -> ToolResult:
        lease = execution.lease
        authorized = AuthorizedCall(
            action_id=action.id,
            task_id=lease.task_id,
            attempt_id=lease.attempt_id,
            scope_id=execution.actor.scope_id,
            tool=call.tool,
            args=dict(call.args),
            logical_key=call.logical_key or "",
            deadline=lease.deadline,
        )
        try:
            return await connector.execute(authorized)
        except StaleLease:
            raise
        except Exception as exc:  # noqa: BLE001 - a connector bug must not kill the worker
            log.exception("connector %s raised %s on action %s", call.tool, type(exc).__name__, action.id)
            message = f"{type(exc).__name__}: {exc}"
            # A tool with side effects may have run before it failed.
            status = ToolStatus.UNKNOWN if connector.spec.side_effects else ToolStatus.FAILED
            return ToolResult(
                status=status,
                summary=message,
                error=TaskError(code=ErrorCode.TOOL_ERROR, message=message, retryable=True),
            )

    async def _record(
        self,
        task: Task,
        execution: ExecutionContext,
        call: ToolCall,
        action: Action,
        result: ToolResult,
    ) -> ToolOutcome:
        now = self._clock()
        summary = result.summary[:MAX_SUMMARY_CHARS]
        stored = {
            "status": str(result.status),
            "summary": summary,
            "artifact_refs": list(result.artifact_refs),
            "receipt_ref": result.receipt_ref,
            "error": result.error.model_dump(mode="json") if result.error is not None else None,
        }
        result_ref = await self._store_json(task, execution, f"tool_result/{action.id}", stored)
        await self._repos.actions.update(
            action.id,
            _ACTION_STATUS[result.status],
            result_ref=result_ref,
            receipt_ref=result.receipt_ref,
            now=now,
        )
        if result.status is not ToolStatus.UNKNOWN:
            # The call happened, whatever it returned; only an unknown outcome
            # keeps its reservation (spec 6.2 step 4).
            await self._repos.usage.settle(action.id, Usage(tool_calls=1), now)
        await self._event(execution, call, action.id, result.status)
        return ToolOutcome(
            action_id=action.id,
            status=result.status,
            summary=summary,
            artifact_refs=list(result.artifact_refs),
            receipt_ref=result.receipt_ref,
            error=result.error,
        )

    async def _reuse(self, action: Action, execution: ExecutionContext) -> ToolOutcome:
        """Outcome of a call already recorded under this logical key."""
        stored: dict[str, Any] | None = None
        if action.result_ref:
            try:
                content = await self._artifacts.read(action.result_ref, execution.actor)
                stored = json.loads(content.content)
            except Exception:  # noqa: BLE001 - a missing artifact only loses the summary
                stored = None
        if stored is None:
            status = _STATUS_FROM_ACTION[action.status]
            error = None
            if status is ToolStatus.UNKNOWN:
                error = TaskError(
                    code=ErrorCode.TOOL_ERROR,
                    message="the earlier call of this action has no recorded result",
                    retryable=True,
                )
            return ToolOutcome(
                action_id=action.id,
                status=status,
                summary="",
                receipt_ref=action.receipt_ref,
                error=error,
            )
        error = TaskError.model_validate(stored["error"]) if stored.get("error") else None
        return ToolOutcome(
            action_id=action.id,
            status=ToolStatus(stored["status"]),
            summary=stored.get("summary", ""),
            artifact_refs=list(stored.get("artifact_refs") or []),
            receipt_ref=stored.get("receipt_ref") or action.receipt_ref,
            error=error,
        )

    # -- helpers --------------------------------------------------------------

    async def _ensure_current(self, execution: ExecutionContext) -> Task:
        """The lease must still be the task's current one; the gateway never
        changes task state, it only refuses work for a worker that lost it."""
        lease = execution.lease
        task = await self._repos.tasks.get(lease.scope_id, lease.task_id)
        if (
            task.current_attempt_id != lease.attempt_id
            or task.lease_epoch != lease.lease_epoch
            or task.status not in (TaskStatus.RUNNING, TaskStatus.CANCELLING)
        ):
            raise StaleLease(
                f"lease {lease.attempt_id}/{lease.lease_epoch} is not current for task {lease.task_id}",
                task_id=lease.task_id,
            )
        return task

    def _action(
        self,
        execution: ExecutionContext,
        call: ToolCall,
        connector: Connector,
        args_ref: str | None,
        status: ActionStatus,
    ) -> Action:
        lease = execution.lease
        return Action(
            task_id=lease.task_id,
            attempt_id=lease.attempt_id,
            logical_key=call.logical_key or "",
            tool=call.tool,
            request_hash=call.request_hash(),
            args_ref=args_ref,
            read_only=connector.spec.read_only,
            status=status,
            created_at=self._clock(),
            updated_at=self._clock(),
        )

    async def _event(
        self,
        execution: ExecutionContext,
        call: ToolCall,
        action_id: str | None,
        status: ToolStatus,
    ) -> None:
        lease = execution.lease
        await self._repos.events.append(
            Event(
                scope_id=execution.actor.scope_id,
                task_id=lease.task_id,
                attempt_id=lease.attempt_id,
                type=EventType.TOOL_ACTION,
                payload={
                    "action_id": action_id,
                    "tool": call.tool,
                    "logical_key": call.logical_key,
                    "status": str(status),
                },
                occurred_at=self._clock(),
            )
        )

    async def _store_json(
        self,
        task: Task,
        execution: ExecutionContext,
        name: str,
        value: Any,
    ) -> str:
        """Trajectory content of one call; ``task_id`` empty so it is not
        listed among the task's work artifacts."""
        ref = await self._artifacts.put(
            ArtifactInput(
                scope_id=execution.actor.scope_id,
                task_id=None,
                name=f"{task.id}/{name}",
                media_type="application/json",
                content=canonical_json(value).encode("utf-8"),
            )
        )
        return ref.id
