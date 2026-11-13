"""Minimal HTTP API: create / read / cancel a task (spec 5.1, item 7).

The app is built around an injected ``TaskService``, so it owns no storage and
no lifecycle logic: every endpoint is one service call. The entry point builds
the ``TaskOrigin`` itself (main role, foreground lane, user trigger); a request
body can never choose the role, lane or trigger (spec 3.1).

Identity is the development stand-in agreed for P0: ``X-Actor-Id`` and
``X-Scope-Id`` headers, defaulting to ``dev``. Replacing ``current_actor`` with
real authentication is the only change needed later; nothing here reads the
actor from the body.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..contracts import (
    Actor,
    AgentRole,
    ErrorCode,
    ErrorResponse,
    HarnessError,
    RunLane,
    Task,
    TaskOrigin,
    TaskRef,
    TaskRequest,
    TriggerType,
)
from ..interfaces import TaskService

_HTTP_STATUS = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.LIMITS_EXCEEDED: 400,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.INVALID_TRANSITION: 409,
    ErrorCode.LEASE_LOST: 409,
    ErrorCode.CANCELLED: 409,
    ErrorCode.VERIFICATION_FAILED: 422,
}


class CancelRequest(BaseModel):
    reason: str = ""


def create_app(tasks: TaskService, *, profile_ref: str = "main@1") -> FastAPI:
    app = FastAPI(title="DaraOS harness API", version="0.1.0")
    app.state.tasks = tasks

    @app.middleware("http")
    async def _request_id(request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.exception_handler(HarnessError)
    async def _harness_error(request: Request, exc: HarnessError):
        request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
        return JSONResponse(
            status_code=_HTTP_STATUS.get(exc.code, 500),
            content=ErrorResponse.from_error(exc, request_id).model_dump(mode="json"),
        )

    def service(request: Request):
        return request.app.state.tasks

    def current_actor(
        x_actor_id: str | None = Header(default=None, alias="X-Actor-Id"),
        x_scope_id: str | None = Header(default=None, alias="X-Scope-Id"),
    ) -> Actor:
        return Actor(actor_id=x_actor_id or "dev", scope_id=x_scope_id or "dev")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/tasks", status_code=202, response_model=TaskRef)
    async def create_task(
        body: TaskRequest,
        response: Response,
        actor: Actor = Depends(current_actor),
        service=Depends(service),
    ) -> TaskRef:
        origin = TaskOrigin(
            agent_role=AgentRole.MAIN,
            agent_profile_ref=profile_ref,
            run_lane=RunLane.FOREGROUND,
            trigger_type=TriggerType.USER,
            trigger_ref=actor.actor_id,
        )
        task = await service.create(body, actor, origin)
        response.headers["Location"] = f"/tasks/{task.id}"
        return TaskRef.of(task)

    @app.get("/tasks/{task_id}", response_model=Task)
    async def get_task(
        task_id: str,
        actor: Actor = Depends(current_actor),
        service=Depends(service),
    ) -> Task:
        return await service.get(actor.scope_id, task_id)

    @app.post("/tasks/{task_id}/cancel", response_model=Task)
    async def cancel_task(
        task_id: str,
        body: CancelRequest | None = None,
        actor: Actor = Depends(current_actor),
        service=Depends(service),
    ) -> Task:
        return await service.request_cancel(
            actor.scope_id,
            task_id,
            body.reason if body is not None else "",
        )

    return app
