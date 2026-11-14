"""Anthropic Messages adapter (``anthropic_messages``).

Claude, and vendors with an Anthropic-compatible endpoint (MiniMax and
others). Differences from Chat Completions handled here:

* the system prompt is a top-level field, not a message;
* tool results are ``tool_result`` blocks inside a user message, and
  consecutive results are merged into one user turn;
* thinking blocks (with their signatures) must be sent back unchanged
  during a tool-call loop, so the whole assistant ``content`` is kept in
  ``native`` and replayed when the history goes back to the same protocol.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import anthropic

from ...contracts import AssistantStep, ContextBundle, new_id
from ..messages import Message, ToolCallPart, native_for, parse
from .base import (
    ModelError,
    ToolNames,
    build_step,
    drop_none,
    history_tool_names,
    model_usage,
    parse_args,
    retryable_status,
    tool_schema,
)
from .config import ModelConfig, WireProtocol

PROTOCOL = WireProtocol.ANTHROPIC_MESSAGES


class AnthropicMessagesAdapter:
    protocol = PROTOCOL

    def __init__(
        self,
        config: ModelConfig,
        *,
        client: Any | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config.resolved()
        self.model = self.config.model
        self.max_output_tokens = self.config.max_output_tokens
        if client is None:
            client = anthropic.AsyncAnthropic(
                api_key=self.config.api_key(env) or "not-needed",
                base_url=self.config.base_url,
                timeout=self.config.timeout_sec,
                max_retries=self.config.max_retries,
                default_headers=dict(self.config.extra_headers) or None,
            )
        self._client = client

    # -- request ----------------------------------------------------------

    def build_request(
        self, context: ContextBundle, *, max_output_tokens: int | None = None
    ) -> dict[str, Any]:
        return self._prepare(context, max_output_tokens)[0]

    def _prepare(
        self, context: ContextBundle, max_output_tokens: int | None
    ) -> tuple[dict[str, Any], ToolNames]:
        cfg = self.config
        messages = parse(context.messages)
        names = ToolNames(context.tool_specs, history_tool_names(messages))
        system_parts = [m.content for m in messages if m.role == "system" and m.content]
        max_tokens = cfg.max_output_tokens if max_output_tokens is None else max(1, min(cfg.max_output_tokens, max_output_tokens))
        request: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": max_tokens,
            "messages": self._messages([m for m in messages if m.role != "system"], names),
        }
        if system_parts:
            request["system"] = "\n\n".join(system_parts)
        if context.tool_specs:
            request["tools"] = [
                {
                    "name": names.to_wire(spec.name),
                    "description": spec.description,
                    "input_schema": tool_schema(spec),
                }
                for spec in context.tool_specs
            ]
        if cfg.thinking_budget_tokens:
            budget = min(cfg.thinking_budget_tokens, max(max_tokens - 1, 1024))
            request["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # Extended thinking needs max_tokens above the thinking budget.
            request["max_tokens"] = max(max_tokens, budget + 1)
        elif cfg.temperature is not None:
            # Thinking mode does not accept a temperature.
            request["temperature"] = cfg.temperature
        if cfg.extra_body:
            request["extra_body"] = dict(cfg.extra_body)
        return request, names

    @staticmethod
    def _messages(messages: list[Message], names: ToolNames) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []

        def append(role: str, blocks: list[dict[str, Any]]) -> None:
            # Anthropic wants user and assistant turns to alternate.
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": blocks})

        for m in messages:
            if m.role == "user":
                append("user", [{"type": "text", "text": m.content or " "}])
            elif m.role == "tool":
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id,
                    "content": m.content or " ",
                }
                if m.is_error:
                    block["is_error"] = True
                append("user", [block])
            else:
                native = native_for(m, PROTOCOL)
                if native and native.get("content"):
                    append("assistant", [dict(b) for b in native["content"]])
                    continue
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for c in m.tool_calls:
                    blocks.append(
                        {"type": "tool_use", "id": c.id, "name": names.to_wire(c.name), "input": c.args}
                    )
                append("assistant", blocks or [{"type": "text", "text": " "}])
        return out

    # -- call ---------------------------------------------------------------

    async def next_step(
        self, context: ContextBundle, *, max_output_tokens: int | None = None
    ) -> AssistantStep:
        request, names = self._prepare(context, max_output_tokens)
        try:
            response = await self._client.messages.create(**request)
        except anthropic.APIStatusError as exc:
            raise ModelError(
                f"model request failed with HTTP {exc.status_code}: {str(exc)[:300]}",
                retryable=retryable_status(exc.status_code),
                status_code=exc.status_code,
                responded=True,
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ModelError(f"model request got no response: {str(exc)[:300]}", retryable=True, responded=False) from exc
        return self.parse_response(response, names=names)

    def parse_response(self, response: Any, *, names: ToolNames | None = None) -> AssistantStep:
        names = names or ToolNames([])
        content = [drop_none(b.model_dump(mode="json")) for b in (response.content or [])]
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        calls: list[ToolCallPart] = []
        for block in content:
            kind = block.get("type")
            if kind == "text":
                text_parts.append(block.get("text", ""))
            elif kind == "thinking":
                thinking_parts.append(block.get("thinking", ""))
            elif kind == "tool_use":
                args, problem, raw_args = parse_args(block.get("input"))
                calls.append(
                    ToolCallPart(
                        id=block.get("id") or new_id("call"),
                        name=names.from_wire(block.get("name", "")),
                        args=args,
                        invalid=problem,
                        raw_args=raw_args if raw_args is not None else None,
                    )
                )
        usage = response.usage
        input_tokens = (usage.input_tokens or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        input_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
        return build_step(
            model=getattr(response, "model", None) or self.model,
            protocol=PROTOCOL,
            text="".join(text_parts),
            calls=calls,
            usage=model_usage(input_tokens, usage.output_tokens),
            reasoning="\n".join(t for t in thinking_parts if t) or None,
            native={"protocol": str(PROTOCOL), "stop_reason": response.stop_reason, "content": content},
            native_ref=getattr(response, "id", None),
        )
