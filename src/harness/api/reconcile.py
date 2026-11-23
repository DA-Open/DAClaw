"""Manual decisions for uncertain side effects by the original task actor.

CLI and web tokens can both resolve the action when they identify the same scope and actor.
Repository access here only reads state; the gateway delegates mutations to atomic decide.
"""

from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ..app import HarnessSystem
from ..contracts import ActionStatus, ErrorCode, NotFound, TaskStatus, WaitCondition
from ..storage.repositories import round_attempts
from ..tools.gateway import reconciliation_history
from .errors import ApiError, body


class ResolveAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["happened", "not_happened"]


def _conflict(request: Request, status: ActionStatus, task_status: TaskStatus) -> JSONResponse:
    content = body(request, ErrorCode.INVALID_TRANSITION, "task or action is not awaiting reconciliation")
    return JSONResponse(content | {"current_status": status.value, "task_status": task_status.value}, status_code=409)


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
        if task.status is not TaskStatus.CANCELLED and (
            task.status is not TaskStatus.WAITING or task.wait_condition is not WaitCondition.RECONCILIATION
        ):
            return _conflict(request, action.status, task.status)
        if action.read_only or action.status not in (ActionStatus.PLANNED, ActionStatus.RUNNING, ActionStatus.UNKNOWN):
            return _conflict(request, action.status, task.status)
        history = await reconciliation_history(system.repos.events, task)
        expected_attempt = round_attempts(
            event.payload["status"] for event in history if event.payload["action_id"] == action.id
        ) + 1
        status = await system.gateway.resolve(task, action, payload.outcome == "happened", actor, expected_attempt)
        if status is None:
            current = await system.repos.actions.get(task.id, action.logical_key)
            current_task = await system.repos.tasks.get(task.scope_id, task.id)
            return _conflict(request, current.status, current_task.status)
        return {"action_id": action.id, "status": status.value}

    return router
