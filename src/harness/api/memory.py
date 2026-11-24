"""Owner-authenticated memory lifecycle; identity comes from the bearer token."""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from ..contracts import ErrorCode, InvalidTransition, MemoryKind, MemoryStatus
from ..personal.memory import ALL_RECORDS, MemoryRevision
from ..storage.memory_store import is_current
from .errors import ApiError


class CreateMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["preference", "fact"]
    text: str = Field(max_length=4000)
    summary: str = Field(min_length=1, max_length=300)
    tags: list[str] = Field(default_factory=list, max_length=16)
    status: MemoryStatus = MemoryStatus.CONFIRMED
    expires_at: datetime | None = None
    pinned: bool = False


def routes(system):
    router = APIRouter()
    service = system.memory

    async def check_pin(scope_id, tags, previous_tags):
        if "pinned" in tags and "pinned" not in previous_tags:
            current = await service.memories.list(scope_id, now=service.clock(), limit=ALL_RECORDS)
            if sum("pinned" in r.tags for r in current) >= 5:
                raise ApiError(400, ErrorCode.INVALID_REQUEST, "at most 5 current memories may be pinned")

    @router.post("/memories", status_code=201)
    async def create(payload: CreateMemory, request: Request):
        actor = request.state.actor
        tags = list(payload.tags)
        if payload.pinned and "pinned" not in tags:
            tags.append("pinned")
        await check_pin(actor.scope_id, tags, [])
        try:
            return await service.remember(actor.scope_id, actor.actor_id, payload.kind, payload.text,
                status=payload.status, summary=payload.summary, tags=tags, source_ref=f"user:{actor.actor_id}",
                subject_ref=None, expires_at=payload.expires_at)
        except ValueError as exc:
            raise ApiError(400, ErrorCode.INVALID_REQUEST, str(exc)) from exc

    @router.get("/memories")
    async def listing(request: Request, kind: MemoryKind | None = None,
                      status: MemoryStatus | None = None, include_ended: bool = False):
        scope = request.state.actor.scope_id
        items = await service.memories.list(scope, now=service.clock(),
            kinds=[kind] if kind is not None else None, statuses=[status] if status is not None else None,
            include_ended=include_ended)
        candidates = await service.memories.list(scope, now=service.clock(),
            statuses=[MemoryStatus.CANDIDATE], limit=ALL_RECORDS)
        return {"items": items, "candidates": {"count": len(candidates), "limit": 10}}

    @router.get("/memories/{memory_id}")
    async def read(memory_id: str, request: Request):
        record, text = await service.read(request.state.actor.scope_id, request.state.actor.actor_id, memory_id)
        return {"record": record, "text": text}

    @router.post("/memories/{memory_id}/revise")
    async def revise(memory_id: str, payload: MemoryRevision, request: Request):
        actor = request.state.actor
        old = await service.memories.get(actor.scope_id, memory_id)
        if not is_current(old, service.clock()):
            raise InvalidTransition("memory is no longer current")
        await check_pin(actor.scope_id, payload.tags, old.tags)
        try:
            return await service.revise(actor.scope_id, actor.actor_id, memory_id, payload)
        except ValueError as exc:
            raise ApiError(400, ErrorCode.INVALID_REQUEST, str(exc)) from exc

    @router.post("/memories/{memory_id}/confirm")
    async def confirm(memory_id: str, request: Request):
        return await service.confirm(request.state.actor.scope_id, request.state.actor.actor_id, memory_id)

    @router.post("/memories/{memory_id}/reject")
    async def reject(memory_id: str, request: Request):
        return await service.reject(request.state.actor.scope_id, request.state.actor.actor_id, memory_id)

    @router.post("/memories/{memory_id}/forget")
    async def forget(memory_id: str, request: Request):
        return await service.forget(request.state.actor.scope_id, request.state.actor.actor_id, memory_id)

    return router
