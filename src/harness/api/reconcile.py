"""Manual decisions for uncertain side effects."""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from ..app import HarnessSystem
from ..contracts import ActionStatus, ErrorCode, NotFound, utcnow
from .errors import ApiError


class ResolveAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["happened", "not_happened"]


def reconciliation_router(system: HarnessSystem) -> APIRouter:
    router = APIRouter()

    @router.post("/tasks/{task_id}/actions/{action_id}/resolve")
    async def resolve_action(task_id: str, action_id: str, payload: ResolveAction, request: Request):
        actor = request.state.actor
        task = await system.repos.tasks.get(actor.scope_id, task_id)
        if actor.actor_id != task.actor_id:
            raise ApiError(403, ErrorCode.FORBIDDEN, "only the task actor may resolve an action")
        actions = await system.repos.actions.list_for_task(task.id)
        action = next((action for action in actions if action.id == action_id), None)
        if action is None:
            raise NotFound(f"action {action_id} not found")
        if action.read_only or action.status not in (ActionStatus.PLANNED, ActionStatus.RUNNING, ActionStatus.UNKNOWN):
            raise ApiError(409, ErrorCode.INVALID_TRANSITION, "action is not an unsettled side effect")
        status = await system.gateway.resolve(task, action, payload.outcome == "happened", actor)
        await system.repos.tasks.release_reconciliation(task.scope_id, task.id, utcnow())
        return {"action_id": action.id, "status": status.value}

    return router
