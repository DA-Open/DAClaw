"""Human decisions on side-effecting actions (spec 5.2, 批准).

An approval is bound to (action_id, request_hash), so a decision taken for one
set of arguments can never authorise another. The whole decision - checking the
task and action, writing the approval, recording the event and moving the task
back to queued - is one repository transaction, so two racing requests cannot
both succeed and a reject can never overwrite an approve.

Rejecting also requeues: the next attempt re-runs the step, the gateway sees the
rejected decision, marks the action failed and refuses with the reason, and the
model can choose another way instead of being asked again.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from ..contracts import (
    Actor,
    Approval,
    ApprovalConflict,
    ApprovalDecision,
    HarnessError,
    InvalidTransition,
    NotFound,
    Task,
    utcnow,
)
from ..storage.repositories import Repositories


class ApprovalRefused(HarnessError):
    """The actor may not decide this approval."""


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
        """Record the decision; the task is queued again either way."""
        if self._repos.approvals is None:
            raise HarnessError("this deployment has no approvals store")
        now = self._clock()
        task = await self._repos.tasks.get(scope_id, task_id)
        if task.actor_id != actor.actor_id:
            raise ApprovalRefused("only the task actor may decide this approval")
        return await self._repos.tasks.decide_approval(
            scope_id,
            task_id,
            action_id,
            request_hash=request_hash,
            approver=actor.actor_id,
            decision=str(decision),
            reason=reason,
            expires_at=now + timedelta(seconds=expires_in_sec) if expires_in_sec else None,
            now=now,
        )


__all__ = ["Approvals", "ApprovalRefused", "ApprovalConflict", "InvalidTransition", "NotFound"]
