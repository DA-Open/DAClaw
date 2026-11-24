"""Human decisions on side-effecting actions (spec 5.2, 批准).

An approval is bound to (action_id, request_hash), so a decision taken for one
set of arguments can never authorise another. Deciding writes the approval,
records an event, and moves a task that was waiting for this decision back to
queued; the next attempt re-runs the same step and the gateway finds the
approval. Nothing here executes the tool: the action is still marked
needs_approval until the gateway runs it.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from ..contracts import (
    Actor,
    Approval,
    ApprovalDecision,
    Event,
    EventType,
    HarnessError,
    InvalidTransition,
    NotFound,
    Task,
    TaskStatus,
    WaitCondition,
    utcnow,
)
from ..storage.repositories import Repositories


class ApprovalRefused(HarnessError):
    """The task or action is not in a state where a decision applies."""


class Approvals:
    def __init__(self, repos: Repositories, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._repos = repos
        self._clock = clock

    async def decide(
        self,
        scope_id: str,
        task_id: str,
        action_id: str,
        *,
        actor: Actor,
        decision: ApprovalDecision,
        request_hash: str,
        reason: str = "",
        expires_in_sec: int | None = None,
    ) -> tuple[Task, Approval]:
        """Record the decision; on approve the task is queued again."""
        if self._repos.approvals is None:
            raise HarnessError("this deployment has no approvals store")
        now = self._clock()
        task = await self._repos.tasks.get(scope_id, task_id)
        if task.actor_id != actor.actor_id:
            raise ApprovalRefused("only the task actor may decide this approval")
        if task.status is not TaskStatus.WAITING or task.wait_condition is not WaitCondition.APPROVAL:
            raise InvalidTransition(f"task {task_id} is {task.status}, not waiting for an approval")
        actions = await self._repos.actions.list_for_task(task.id)
        action = next((a for a in actions if a.id == action_id), None)
        if action is None:
            raise NotFound(f"action {action_id} not found on task {task_id}")
        if action.request_hash != request_hash:
            raise ApprovalRefused("request_hash does not match the action; a decision is bound to its arguments")
        approval = Approval(
            action_id=action.id,
            request_hash=action.request_hash,
            approver=actor.actor_id,
            decision=decision,
            reason=reason,
            expires_at=now + timedelta(seconds=expires_in_sec) if expires_in_sec else None,
            decided_at=now,
        )
        stored = await self._repos.approvals.add(approval)
        await self._repos.events.append(
            Event(
                scope_id=task.scope_id,
                task_id=task.id,
                type=EventType.APPROVAL_DECIDED,
                payload={
                    "action_id": action.id,
                    "tool": action.tool,
                    "decision": str(decision),
                    "approver": actor.actor_id,
                },
                occurred_at=now,
                received_at=now,
            )
        )
        if decision is ApprovalDecision.APPROVE:
            task = await self._repos.tasks.requeue(scope_id, task_id, now)
        return task, stored


__all__ = ["Approvals", "ApprovalRefused"]
