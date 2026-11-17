"""The P0 HTTP API (spec 5.1), over one ``HarnessSystem``.

| Route | Does |
|---|---|
| ``POST /tasks`` | create a Main task; ``Idempotency-Key`` header; 202 |
| ``GET /tasks/{id}`` | status, wait reason, usage, result, error |
| ``GET /tasks/{id}/events`` | SSE progress; ``Last-Event-ID`` resumes; ``final`` once the task stops |
| ``POST /tasks/{id}/cancel`` | idempotent cancel request |
| ``GET /tasks/{id}/trace`` | the task's timeline (``trace.build_trace``) |
| ``GET /artifacts/{id}`` | an artifact's content, by byte range |

Not here (P1/P2): resume, approvals, POST /events, subscriptions.

The API never runs model or tool calls in a request (spec 2): it only talks
to TaskService and the stores. Workers run tasks, in this process
(``--with-worker``) or in separate processes sharing PostgreSQL and artifacts.

Identity comes from the bearer token (``api.auth``), never from the body:
``agent_role`` is always ``main`` and ``prefer_background`` only picks the
lane. Another scope's task or artifact is 404, the same as a missing one.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..app import HarnessSystem
from ..contracts import (
    Acceptance,
    Actor,
    AgentRole,
    ErrorCode,
    HarnessError,
    Limits,
    NotFound,
    RemainingBudget,
    RunLane,
    Task,
    TaskOrigin,
    TaskRequest,
    TaskStatus,
    TriggerType,
    utcnow,
)
from ..trace import build_trace
from . import errors
from .auth import TokenAuth
from .errors import ApiError

MAIN_PROFILE = "main@1"
# Statuses after which the task does nothing until someone acts.
STOPPED = frozenset({TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.WAITING})
MAX_ARTIFACT_BYTES = 5 * 1024 * 1024


class CreateTask(BaseModel):
    """POST /tasks body (spec 5.1). Unknown fields are refused, so a caller
    cannot slip in agent_role, scope_id or run_lane."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1)
    input_refs: list[str] = Field(default_factory=list)
    capability_ref: str
    acceptance: Acceptance = Field(default_factory=Acceptance)
    limits: Limits | None = None
    prefer_background: bool = False
    conversation_id: str | None = None
    subject_ref: str | None = None
    source_task_id: str | None = None


class CancelTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field("user", min_length=1, max_length=500)


def task_view(task: Task) -> dict[str, Any]:
    """GET /tasks/{id}: what a caller may see of a task."""
    remaining = RemainingBudget.for_task(task, utcnow())
    return {
        "task_id": task.id,
        "status": str(task.status),
        "agent_role": str(task.agent_role),
        "run_lane": str(task.run_lane),
        "goal": task.goal,
        "capability_ref": task.capability_ref,
        "wait_condition": str(task.wait_condition) if task.wait_condition else None,
        "result_ref": task.result_ref,
        "error": task.error.model_dump(mode="json") if task.error else None,
        "usage": {**task.usage.model_dump(mode="json"), "total_tokens": task.usage.total_tokens},
        "limits": task.limits.model_dump(mode="json"),
        "remaining": remaining.model_dump(mode="json"),
        "cancel_requested": task.cancel_requested_at is not None,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
        "deadline": task.deadline.isoformat() if task.deadline else None,
        "events_url": f"/tasks/{task.id}/events",
    }


def _sse(event_id: int | None, event: str, data: dict[str, Any]) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(data, ensure_ascii=False, default=str))
    return "\n".join(lines) + "\n\n"


def create_app(
    system: HarnessSystem,
    auth: TokenAuth,
    *,
    poll_interval_sec: float = 0.5,
    keepalive_sec: float = 15.0,
    lifespan=None,
) -> FastAPI:
    """``lifespan`` (an async context manager factory) runs around the
    server, e.g. to start a worker in this process."""
    app = FastAPI(
        title="DaraOS harness", version="0.1", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    errors.install(app)

    @app.middleware("http")
    async def _request_id(request: Request, call_next):
        # Authentication comes before body validation, so a caller without a
        # token learns nothing about the request format (401, not 400).
        if request.url.path != "/healthz":
            try:
                request.state.actor = auth.actor(request.headers.get("authorization"))
            except HarnessError as exc:
                response = JSONResponse(errors.body(request, exc.code, exc.message), status_code=401)
                response.headers["X-Request-ID"] = errors.request_id(request)
                return response
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - last resort; no details to the client
            errors.log.exception("request %s failed", errors.request_id(request))
            response = JSONResponse(errors.body(request, ErrorCode.INTERNAL, "internal error"), status_code=500)
        response.headers["X-Request-ID"] = errors.request_id(request)
        return response

    def actor(request: Request) -> Actor:
        return request.state.actor

    async def own_task(who: Actor, task_id: str) -> Task:
        try:
            return await system.tasks.get(who.scope_id, task_id)
        except NotFound:
            raise ApiError(404, ErrorCode.NOT_FOUND, f"task {task_id} not found") from None

    def check_inputs(who: Actor, refs: list[str]) -> None:
        """Files named in input_refs must sit in the caller's directories
        (the gateway refuses them at run time anyway; this answers 400 now)."""
        from pathlib import Path

        for ref in refs:
            if not ref.startswith("file:"):
                continue
            raw = ref[len("file:"):]
            if raw.startswith("//"):
                raw = raw[2:]
            try:
                path = Path(raw).expanduser().resolve()
            except (ValueError, RuntimeError):
                raise ApiError(400, ErrorCode.INVALID_REQUEST, f"input {ref!r}: invalid file path") from None
            decision = system.policy.check_path(
                path, scope_id=who.scope_id, role=AgentRole.MAIN
            )
            if not decision.allowed:
                raise ApiError(400, ErrorCode.FORBIDDEN, f"input {ref!r}: {decision.reason}")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/tasks", status_code=202)
    async def create_task(
        payload: CreateTask,
        request: Request,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        who = actor(request)
        if not idempotency_key or not idempotency_key.strip() or len(idempotency_key) > 200:
            raise ApiError(400, ErrorCode.INVALID_REQUEST, "Idempotency-Key header is required (1-200 characters)")
        check_inputs(who, payload.input_refs)
        try:
            task_request = TaskRequest(
                request_key=idempotency_key.strip(),
                limits=payload.limits or system.config.default_limits,
                **payload.model_dump(exclude={"limits"}),
            )
        except ValueError as exc:
            raise ApiError(400, ErrorCode.INVALID_REQUEST, str(exc)) from None
        origin = TaskOrigin(
            agent_role=AgentRole.MAIN,
            agent_profile_ref=MAIN_PROFILE,
            run_lane=RunLane.BACKGROUND if payload.prefer_background else RunLane.FOREGROUND,
            trigger_type=TriggerType.USER,
            trigger_ref=idempotency_key.strip(),
        )
        try:
            task = await system.tasks.create(task_request, who, origin)
        except NotFound as exc:
            # An unknown capability or profile is a bad request, not a missing task.
            raise ApiError(400, ErrorCode.INVALID_REQUEST, exc.message) from None
        return JSONResponse(
            {
                "task_id": task.id,
                "agent_role": str(task.agent_role),
                "run_lane": str(task.run_lane),
                "status": str(task.status),
                "events_url": f"/tasks/{task.id}/events",
            },
            status_code=202,
        )

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str, request: Request) -> dict[str, Any]:
        who = actor(request)
        return task_view(await own_task(who, task_id))

    @app.post("/tasks/{task_id}/cancel")
    async def cancel_task(
        task_id: str, request: Request, payload: CancelTask | None = None
    ) -> dict[str, Any]:
        who = actor(request)
        await own_task(who, task_id)
        task = await system.tasks.request_cancel(who.scope_id, task_id, (payload or CancelTask()).reason)
        return task_view(task)

    @app.get("/tasks/{task_id}/trace")
    async def get_trace(task_id: str, request: Request) -> dict[str, Any]:
        who = actor(request)
        await own_task(who, task_id)
        return await build_trace(system.repos, system.artifacts, who.scope_id, task_id)

    @app.get("/tasks/{task_id}/events")
    async def task_events(
        task_id: str,
        request: Request,
        last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        who = actor(request)
        await own_task(who, task_id)
        try:
            after = int(last_event_id) if last_event_id else 0
        except ValueError:
            raise ApiError(400, ErrorCode.INVALID_REQUEST, "Last-Event-ID must be an event id from this stream") from None

        async def stream() -> AsyncIterator[str]:
            cursor = after
            idle = 0.0
            while True:
                if await request.is_disconnected():
                    return  # a dropped client does not cancel the task (spec 5.1)
                events = await system.repos.events.list(who.scope_id, task_id=task_id, after_seq=cursor, limit=200)
                for e in events:
                    cursor = e.seq
                    yield _sse(e.seq, "progress", {"type": str(e.type), **e.payload, "occurred_at": e.occurred_at.isoformat()})
                if events:
                    idle = 0.0
                    continue
                # Caught up. "final" goes out only once the result is committed
                # (spec 5.1) and every event up to it has been sent.
                task = await system.tasks.get(who.scope_id, task_id)
                if task.status in STOPPED:
                    tail = await system.repos.events.list(who.scope_id, task_id=task_id, after_seq=cursor, limit=1)
                    if not tail:
                        yield _sse(None, "final", task_view(task))
                        return
                    continue
                await asyncio.sleep(poll_interval_sec)
                idle += poll_interval_sec
                if idle >= keepalive_sec:
                    idle = 0.0
                    yield ": keep-alive\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/artifacts/{artifact_id}")
    async def get_artifact(
        artifact_id: str,
        request: Request,
        offset: int = Query(0, ge=0),
        length: int | None = Query(None, ge=0, le=MAX_ARTIFACT_BYTES),
    ) -> Response:
        who = actor(request)
        try:
            content = await system.artifacts.read(
                artifact_id, who, offset=offset, length=length if length is not None else MAX_ARTIFACT_BYTES
            )
        except NotFound:
            raise ApiError(404, ErrorCode.NOT_FOUND, f"artifact {artifact_id} not found") from None
        ref = content.ref
        return Response(
            content=content.content,
            media_type=ref.media_type,
            headers={
                "X-Artifact-Id": ref.id,
                "X-Artifact-Size": str(ref.size_bytes),
                "X-Artifact-Offset": str(content.offset),
                "X-Content-Hash": ref.content_hash,
                "Cache-Control": "private, no-store",
            },
        )

    return app


__all__ = ["CreateTask", "create_app", "task_view"]
