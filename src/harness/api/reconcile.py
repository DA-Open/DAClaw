"""Manual decisions for uncertain side effects."""

from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ..app import HarnessSystem
from ..contracts import ActionStatus, ErrorCode, NotFound
from .errors import ApiError, body


class ResolveAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["happened", "not_happened"]


def _conflict(request: Request, status: ActionStatus) -> JSONResponse:
    content = body(request, ErrorCode.INVALID_TRANSITION, "action is not an unsettled side effect")
    return JSONResponse(content | {"current_status": status.value}, status_code=409)


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
            return _conflict(request, action.status)
        status = await system.gateway.resolve(task, action, payload.outcome == "happened", actor)
        if status is None:
            current = await system.repos.actions.get(task.id, action.logical_key)
            return _conflict(request, current.status)
        return {"action_id": action.id, "status": status.value}

    return router
