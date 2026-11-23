"""Trusted subscription/event endpoints and scope-isolated inbox access."""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from ..contracts import ErrorCode, Event, EventType, Limits, ProactivityLevel, Subscription, Usage
from ..proactive_types import DataEvent, default_daily_budget
from .errors import ApiError


class CreateSubscription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=32768)
    subject_ref: str = Field(min_length=1, max_length=4096)
    resources: list[str] = Field(min_length=1, max_length=128)
    event_filter: dict[str, str] = Field(default_factory=dict)
    schedule: str | None = None
    capability_ref: Literal["data_card@1"] = "data_card@1"
    limits: Limits = Field(default_factory=lambda: Limits(timeout_sec=600, max_model_calls=10, max_tool_calls=30, max_total_tokens=50000))
    daily_budget: Usage = Field(default_factory=default_daily_budget)
    proactivity: ProactivityLevel = ProactivityLevel.NORMAL
    result_ttl_seconds: int = Field(7 * 24 * 3600, gt=0)
    quiet_hours: tuple[int, int] = (0, 0)
    focus: list[str] = Field(default_factory=list, max_length=64)
    expires_at: datetime | None = None


class CreateEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=200)
    source_event_id: str = Field(min_length=1, max_length=200)
    subject_ref: str = Field(min_length=1, max_length=4096)
    data: DataEvent


class Ack(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["read", "handled", "ignored"]
    label: Literal["useful", "not_useful", "harmful"] | None = None


def routes(system):
    router = APIRouter()

    @router.post("/subscriptions", status_code=201)
    async def create_subscription(payload: CreateSubscription, request: Request):
        actor = request.state.actor
        try:
            sub = Subscription(
                scope_id=actor.scope_id, owner_id=actor.actor_id, daily_budget=payload.daily_budget,
                **payload.model_dump(exclude={"daily_budget"}),
            )
            return await system.proactive.create_subscription(sub)
        except PermissionError as exc:
            raise ApiError(403, ErrorCode.FORBIDDEN, str(exc)) from exc
        except ValueError as exc:
            raise ApiError(400, ErrorCode.INVALID_REQUEST, str(exc)) from exc

    @router.get("/subscriptions")
    async def subscriptions(request: Request):
        return await system.repos.subscriptions.list_for_scope(request.state.actor.scope_id)

    @router.delete("/subscriptions/{subscription_id}")
    async def revoke(subscription_id: str, request: Request):
        return await system.proactive.revoke(request.state.actor.scope_id, subscription_id)

    @router.get("/subscriptions/{subscription_id}/decisions")
    async def decisions(subscription_id: str, request: Request):
        rows = await system.repos.subscriptions.decisions(request.state.actor.scope_id, subscription_id)
        return Response("".join(r.model_dump_json() + "\n" for r in rows), media_type="application/x-ndjson")

    @router.post("/events", status_code=202)
    async def event(payload: CreateEvent, request: Request):
        if payload.source == "scheduler" or payload.data.kind == "schedule":
            raise ApiError(400, ErrorCode.INVALID_REQUEST, "schedule events are created by Scheduler")
        event = Event(scope_id=request.state.actor.scope_id, type=EventType.EXTERNAL, source=payload.source,
                      source_event_id=payload.source_event_id, subject_ref=payload.subject_ref, payload=payload.data.model_dump())
        return {"tasks": await system.proactive.on_event(event)}

    @router.get("/inbox")
    async def inbox(request: Request):
        return await system.delivery.inbox(request.state.actor.scope_id)

    @router.post("/inbox/{item_id}/ack")
    async def ack(item_id: str, payload: Ack, request: Request):
        label = payload.label or {"read": None, "handled": "useful", "ignored": "not_useful"}[payload.status]
        return await system.repos.delivery.ack(request.state.actor.scope_id, item_id, payload.status, label, system.delivery.clock())

    return router
