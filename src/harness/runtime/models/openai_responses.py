"""OpenAI Responses API adapter (``openai_responses``).

Used for OpenAI reasoning models. The call is stateless (``store=False``):
the whole history is sent each time, and reasoning items come back encrypted
(``include=["reasoning.encrypted_content"]``) so they can be replayed between
tool calls without OpenAI keeping the conversation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import openai

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

PROTOCOL = WireProtocol.OPENAI_RESPONSES


class OpenAIResponsesAdapter:
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
            client = openai.AsyncOpenAI(
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
        instructions = "\n\n".join(m.content for m in messages if m.role == "system" and m.content)
        cap = cfg.max_output_tokens if max_output_tokens is None else max(16, min(cfg.max_output_tokens, max_output_tokens))
        request: dict[str, Any] = {
            "model": cfg.model,
            "input": self._items([m for m in messages if m.role != "system"], names),
            "max_output_tokens": cap,
            "store": False,
        }
        if instructions:
            request["instructions"] = instructions
        if context.tool_specs:
            request["tools"] = [
                {
                    "type": "function",
                    "name": names.to_wire(spec.name),
                    "description": spec.description,
                    "parameters": tool_schema(spec),
                    "strict": False,
                }
                for spec in context.tool_specs
            ]
        if cfg.reasoning_effort:
            request["reasoning"] = {"effort": cfg.reasoning_effort}
            request["include"] = ["reasoning.encrypted_content"]
        if cfg.temperature is not None:
            request["temperature"] = cfg.temperature
        if cfg.extra_body:
            request["extra_body"] = dict(cfg.extra_body)
        return request, names

    @staticmethod
    def _items(messages: list[Message], names: ToolNames) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "user":
                items.append({"role": "user", "content": m.content})
            elif m.role == "tool":
                output = f"Error: {m.content}" if m.is_error else m.content
                items.append({"type": "function_call_output", "call_id": m.tool_call_id, "output": output})
            else:
                native = native_for(m, PROTOCOL)
                if native and native.get("output"):
                    items.extend(dict(i) for i in native["output"])
                    continue
                if m.content:
                    items.append({"role": "assistant", "content": m.content})
                for c in m.tool_calls:
                    arguments = c.raw_args if c.raw_args is not None else json.dumps(c.args, ensure_ascii=False)
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": c.id,
                            "name": names.to_wire(c.name),
                            "arguments": arguments,
                        }
                    )
        return items

    # -- call ---------------------------------------------------------------

    async def next_step(
        self, context: ContextBundle, *, max_output_tokens: int | None = None
    ) -> AssistantStep:
        request, names = self._prepare(context, max_output_tokens)
        try:
            response = await self._client.responses.create(**request)
        except openai.APIStatusError as exc:
            raise ModelError(
                f"model request failed with HTTP {exc.status_code}: {str(exc)[:300]}",
                retryable=retryable_status(exc.status_code),
                status_code=exc.status_code,
                responded=True,
            ) from exc
        except openai.APIConnectionError as exc:
            raise ModelError(f"model request got no response: {str(exc)[:300]}", retryable=True, responded=False) from exc
        return self.parse_response(response, names=names)

    def parse_response(self, response: Any, *, names: ToolNames | None = None) -> AssistantStep:
        names = names or ToolNames([])
        if getattr(response, "status", None) == "failed":
            error = getattr(response, "error", None)
            raise ModelError(f"model response failed: {error}", retryable=True, responded=True)
        output = [drop_none(i.model_dump(mode="json")) for i in (response.output or [])]
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: list[ToolCallPart] = []
        for item in output:
            kind = item.get("type")
            if kind == "message":
                for part in item.get("content") or []:
                    if part.get("type") == "output_text":
                        text_parts.append(part.get("text", ""))
            elif kind == "reasoning":
                for part in item.get("summary") or []:
                    reasoning_parts.append(part.get("text", ""))
            elif kind == "function_call":
                args, problem, raw_args = parse_args(item.get("arguments"))
                calls.append(
                    ToolCallPart(
                        id=item.get("call_id") or new_id("call"),
                        name=names.from_wire(item.get("name", "")),
                        args=args,
                        invalid=problem,
                        raw_args=raw_args,
                    )
                )
        usage = getattr(response, "usage", None)
        return build_step(
            model=getattr(response, "model", None) or self.model,
            protocol=PROTOCOL,
            text="".join(text_parts),
            calls=calls,
            usage=model_usage(
                getattr(usage, "input_tokens", 0) if usage else 0,
                getattr(usage, "output_tokens", 0) if usage else 0,
            ),
            reasoning="\n".join(r for r in reasoning_parts if r) or None,
            native={"protocol": str(PROTOCOL), "status": getattr(response, "status", None), "output": output},
            native_ref=getattr(response, "id", None),
        )
