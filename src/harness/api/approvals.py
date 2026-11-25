"""POST /tasks/{task_id}/approvals/{action_id}: decide one side-effecting action.

The decision is bound to the action's request_hash, so a client that names an
old hash cannot approve different arguments. Only the task's own actor may
decide; another user in the same scope gets 403.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from ..contracts import ApprovalConflict, ApprovalDecision, HarnessError, InvalidTransition, NotFound
from ..tasks.approvals import ApprovalRefused, Approvals
from .errors import ApiError


class DecideApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    request_hash: str = Field(min_length=1, max_length=200)
    reason: str = Field("", max_length=500)
    expires_in_sec: int | None = Field(None, gt=0, le=7 * 24 * 3600)


def approval_router(system) -> APIRouter:
    router = APIRouter()
    approvals = Approvals(system.repos)

    @router.post("/tasks/{task_id}/approvals/{action_id}")
    async def decide(task_id: str, action_id: str, payload: DecideApproval, request: Request):
        actor = request.state.actor
        try:
            task = await system.repos.tasks.get(actor.scope_id, task_id)
        except NotFound:
            raise ApiError(404, "not_found", f"task {task_id} not found") from None
        if task.actor_id != actor.actor_id:
            raise ApiError(403, "forbidden", "only the task actor may decide this approval")
        try:
            decided, approval = await approvals.decide(
                actor.scope_id,
                task_id,
                action_id,
                actor=actor,
                decision=ApprovalDecision(payload.decision),
                request_hash=payload.request_hash,
                reason=payload.reason,
                expires_in_sec=payload.expires_in_sec,
            )
        except NotFound as exc:
            raise ApiError(404, "not_found", exc.message) from None
        except (ApprovalRefused, InvalidTransition, ApprovalConflict) as exc:
            raise ApiError(409, "invalid_transition", exc.message) from None
        except HarnessError as exc:
            raise ApiError(400, exc.code, exc.message) from exc
        return {
            "task_id": decided.id,
            "action_id": action_id,
            "decision": str(approval.decision),
            "status": str(decided.status),
            "wait_condition": str(decided.wait_condition) if decided.wait_condition else None,
        }

    return router


__all__ = ["DecideApproval", "approval_router"]
