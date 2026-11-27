"""Send approved text messages. Feishu has no read-only lookup by uuid."""

import json
import os
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...config import HarnessConfig
from ...contracts import (
    ActionRef, Actor, AuthorizedCall, ErrorCode, ReconcileResult, TaskError, ToolCall, ToolResult, ToolSpec, ToolStatus,
)
from ...interfaces import ArtifactStore

WINDOW = timedelta(hours=1)
WINDOW_MARGIN = timedelta(minutes=5)
RECONCILE_WINDOW = WINDOW - WINDOW_MARGIN
OUTSIDE_WINDOW = "outside Feishu 55-minute replay window (1 hour minus a 5-minute margin)"
# The send API also reports throttling and an in-flight message with HTTP 400.
TRANSIENT_CODES = {230020, 230049, 99991400}
# Documented send rejections, even when an intermediary returns them with HTTP 200.
REJECTION_CODES = {
    230001, 230002, 230006, 230013, 230015, 230017, 230018, 230019, 230022, 230025, 230027, 230028,
    230029, 230034, 230035, 230036, 230038, 230053, 230054, 230055, 230075, 230099, 232009,
}


class _SendArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receive_id_type: Literal["chat_id", "open_id", "user_id", "union_id", "email"]
    receive_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class _Receipt(BaseModel):
    message_id: str = Field(pattern=r"\S")


class _TokenReply(BaseModel):
    code: int
    tenant_access_token: str | None = None


class _MessageReply(BaseModel):
    code: int
    data: object = None


def _message_result(response: httpx.Response, *, reconciling: bool) -> ReconcileResult:
    status = response.status_code
    uncertain = ReconcileResult(status="unknown", detail="Feishu did not confirm delivery")
    # A rejected replay cannot disprove an earlier delivery.
    if reconciling and status != 200:
        return uncertain
    client_error = 400 <= status < 500
    if status in {408, 425, 429} or (status != 200 and not client_error):
        return uncertain
    try:
        reply = _MessageReply.model_validate_json(response.content, strict=True)
    except ValidationError:
        if client_error:
            return ReconcileResult(status="failed", detail=f"Feishu rejected the message (HTTP {status})")
        return uncertain
    if (reconciling and reply.code != 0) or reply.code in TRANSIENT_CODES:
        return uncertain
    if client_error or reply.code in REJECTION_CODES:
        return ReconcileResult(status="failed", detail=f"Feishu rejected the message (HTTP {status}, code {reply.code})")
    if reply.code != 0:
        return uncertain
    try:
        receipt = _Receipt.model_validate(reply.data, strict=True).message_id
    except ValidationError:
        return uncertain
    return ReconcileResult(status="succeeded", receipt_ref=receipt, detail=f"Feishu message receipt: {receipt}")


class FeishuSendConnector:
    # Idempotency lasts for Feishu's one-hour UUID window. Automatic replay stops at 55 minutes.
    spec = ToolSpec(
        name="feishu.send_message",
        description="Send an approved text message to an allowed Feishu recipient.",
        input_schema=_SendArgs.model_json_schema(),
        read_only=False, side_effects=True, idempotent=True, supports_reconcile=True,
    )

    def __init__(self, config: HarnessConfig, artifacts: ArtifactStore, clock: Callable[[], datetime]) -> None:
        self._config = config
        self._artifacts = artifacts
        self._clock = clock

    def _within_window(self, created_at: datetime) -> bool:
        return timedelta(0) <= self._clock() - created_at < RECONCILE_WINDOW

    async def execute(self, call: AuthorizedCall) -> ToolResult:
        result = await self._send(call.action_id, call.args, None, reconciling=False)
        status = ToolStatus(result.status)
        return ToolResult(
            status=status, receipt_ref=result.receipt_ref, summary=result.detail,
            error=TaskError(code=ErrorCode.TOOL_ERROR, message=result.detail) if status is ToolStatus.FAILED else None,
        )

    async def reconcile(self, action: ActionRef) -> ReconcileResult:
        if action.created_at is None or not self._within_window(action.created_at):
            return ReconcileResult(status="unsupported", detail=OUTSIDE_WINDOW)
        if not action.scope_id or not action.args_ref:
            return ReconcileResult(status="unsupported", detail="original scoped Feishu arguments unavailable")
        try:
            stored = await self._artifacts.read(action.args_ref, Actor(scope_id=action.scope_id, actor_id="harness"))
            args = _SendArgs.model_validate(json.loads(stored.content), strict=True).model_dump()
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return ReconcileResult(status="unsupported", detail="invalid stored Feishu arguments")
        if ToolCall(tool=action.tool, args=args).request_hash() != action.request_hash:
            return ReconcileResult(status="unsupported", detail="stored arguments do not match the approved request")
        return await self._send(action.action_id, args, action.created_at, reconciling=True)

    async def _send(
        self, action_id: str, args: dict[str, Any], created_at: datetime | None, *, reconciling: bool,
    ) -> ReconcileResult:
        config = self._config
        app_id = os.environ.get(config.feishu_app_id_env)
        secret = os.environ.get(config.feishu_app_secret_env)
        # A failed token exchange tells us nothing about a previous send.
        auth_status = "unknown" if reconciling else "failed"
        if not app_id or not secret:
            return ReconcileResult(status=auth_status, detail="Feishu credentials unavailable")
        async with httpx.AsyncClient(
            base_url=config.feishu_base_url, timeout=config.feishu_timeout_sec, trust_env=False,
        ) as client:
            try:
                if created_at is not None and not self._within_window(created_at):
                    return ReconcileResult(status="unsupported", detail=OUTSIDE_WINDOW)
                auth = await client.post("/open-apis/auth/v3/tenant_access_token/internal", json={
                    "app_id": app_id, "app_secret": secret,
                })
                if auth.status_code != 200:
                    return ReconcileResult(status=auth_status, detail=f"Feishu authentication returned HTTP {auth.status_code}")
                token = _TokenReply.model_validate_json(auth.content, strict=True)
                if token.code != 0 or not token.tenant_access_token:
                    return ReconcileResult(status=auth_status, detail="Feishu authentication did not return a token")
            except httpx.RequestError:
                return ReconcileResult(status=auth_status, detail="Feishu authentication interrupted")
            except ValidationError:
                return ReconcileResult(status=auth_status, detail="invalid Feishu authentication response")
            if created_at is not None and not self._within_window(created_at):
                return ReconcileResult(status="unsupported", detail=OUTSIDE_WINDOW)
            try:
                response = await client.post(
                    "/open-apis/im/v1/messages", params={"receive_id_type": args["receive_id_type"]},
                    headers={"Authorization": f"Bearer {token.tenant_access_token}"},
                    json={"uuid": action_id, "receive_id": args["receive_id"], "msg_type": "text",
                          "content": json.dumps({"text": args["text"]}, ensure_ascii=False)},
                )
            except httpx.RequestError:
                return ReconcileResult(status="unknown", detail="Feishu request interrupted")
        return _message_result(response, reconciling=reconciling)
