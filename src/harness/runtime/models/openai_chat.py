"""Chat Completions adapter (``openai_chat``).

Works with OpenAI and the "OpenAI compatible" endpoints of other vendors,
local servers and relays. Vendor differences handled here:

* reasoning text comes back as ``reasoning_content`` (DeepSeek, Kimi, GLM,
  Qwen, vLLM) or ``reasoning`` (OpenRouter, some vLLM builds);
* some vendors require their reasoning field sent back during a tool-call
  loop; ``echo_fields`` lists which (other vendors reject unknown fields);
* Gemini carries thought signatures in ``tool_calls[].extra_content``;
* servers without a ``usage`` block get an estimate, so the budget still
  moves.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import openai

from ...contracts import AssistantStep, ContextBundle, ToolSpec, Usage, new_id
from ..messages import Message, ToolCallPart, native_for, parse
from .base import (
    ModelError,
    ToolNames,
    build_step,
    estimate_tokens,
    history_tool_names,
    model_usage,
    parse_args,
    retryable_status,
    tool_schema,
)
from .config import ModelConfig, WireProtocol

PROTOCOL = WireProtocol.OPENAI_CHAT

# Standard fields of a Chat Completions message; anything else the server
# returns is vendor-specific and kept in ``native``.
_STANDARD_MESSAGE_KEYS = {"role", "content", "tool_calls", "function_call", "refusal", "audio", "annotations"}
_STANDARD_CALL_KEYS = {"id", "type", "function", "index"}


class OpenAIChatAdapter:
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
                # Local servers take any key; the SDK refuses an empty one.
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
        request: dict[str, Any] = {
            "model": cfg.model,
            "messages": self._messages(messages, names),
            cfg.max_tokens_param or "max_tokens": _cap(cfg.max_output_tokens, max_output_tokens),
        }
        if context.tool_specs:
            request["tools"] = [_tool(spec, names) for spec in context.tool_specs]
        if cfg.temperature is not None:
            request["temperature"] = cfg.temperature
        if cfg.reasoning_effort:
            request["reasoning_effort"] = cfg.reasoning_effort
        if cfg.extra_body:
            request["extra_body"] = dict(cfg.extra_body)
        return request, names

    def _messages(self, messages: list[Message], names: ToolNames) -> list[dict[str, Any]]:
        echo = set(self.config.echo_fields or ())
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role in ("system", "user"):
                out.append({"role": m.role, "content": m.content})
            elif m.role == "tool":
                content = f"Error: {m.content}" if m.is_error else m.content
                out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": content})
            else:
                out.append(self._assistant(m, names, echo))
        return out

    @staticmethod
    def _assistant(m: Message, names: ToolNames, echo: set[str]) -> dict[str, Any]:
        native = native_for(m, PROTOCOL) or {}
        call_extras = native.get("tool_call_extras", {})
        message: dict[str, Any] = {"role": "assistant", "content": m.content or None}
        calls = []
        for c in m.tool_calls:
            arguments = c.raw_args if c.raw_args is not None else json.dumps(c.args, ensure_ascii=False)
            call: dict[str, Any] = {
                "id": c.id,
                "type": "function",
                "function": {"name": names.to_wire(c.name), "arguments": arguments},
            }
            for key, value in call_extras.get(c.id, {}).items():
                if key in echo:
                    call[key] = value
            calls.append(call)
        if calls:
            message["tool_calls"] = calls
        elif message["content"] is None:
            message["content"] = ""
        for key, value in native.get("fields", {}).items():
            if key in echo:
                message[key] = value
        return message

    # -- call ---------------------------------------------------------------

    async def next_step(
        self, context: ContextBundle, *, max_output_tokens: int | None = None
    ) -> AssistantStep:
        request, names = self._prepare(context, max_output_tokens)
        try:
            response = await self._client.chat.completions.create(**request)
        except openai.APIStatusError as exc:
            raise ModelError(
                f"model request failed with HTTP {exc.status_code}: {_short(exc)}",
                retryable=retryable_status(exc.status_code),
                status_code=exc.status_code,
                responded=True,
            ) from exc
        except openai.APIConnectionError as exc:
            # Includes timeouts. The server may have run (and billed) the call.
            raise ModelError(f"model request got no response: {_short(exc)}", retryable=True, responded=False) from exc
        return self.parse_response(response, request=request, names=names)

    def parse_response(
        self,
        response: Any,
        *,
        request: dict[str, Any] | None = None,
        names: ToolNames | None = None,
    ) -> AssistantStep:
        if not getattr(response, "choices", None):
            raise ModelError("model response has no choices", retryable=True, responded=True)
        choice = response.choices[0]
        raw = choice.message.model_dump(mode="json", exclude_none=True)
        names = names or ToolNames([])
        calls: list[ToolCallPart] = []
        call_extras: dict[str, dict[str, Any]] = {}
        for tc in raw.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args, problem, raw_args = parse_args(fn.get("arguments"))
            call_id = tc.get("id") or new_id("call")
            calls.append(
                ToolCallPart(
                    id=call_id,
                    name=names.from_wire(fn.get("name") or ""),
                    args=args,
                    invalid=problem,
                    raw_args=raw_args,
                )
            )
            extras = {k: v for k, v in tc.items() if k not in _STANDARD_CALL_KEYS}
            if extras:
                call_extras[call_id] = extras
        fields = {k: v for k, v in raw.items() if k not in _STANDARD_MESSAGE_KEYS}
        reasoning = fields.get("reasoning_content") or fields.get("reasoning")
        native = {
            "protocol": str(PROTOCOL),
            "finish_reason": choice.finish_reason,
            "fields": fields,
            "tool_call_extras": call_extras,
        }
        text = raw.get("content") or ""
        if isinstance(text, list):  # a few servers return content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        return build_step(
            model=getattr(response, "model", None) or self.model,
            protocol=PROTOCOL,
            text=text,
            calls=calls,
            usage=self._usage(response, request, text, calls),
            reasoning=reasoning if isinstance(reasoning, str) else None,
            native=native,
            native_ref=getattr(response, "id", None),
        )

    @staticmethod
    def _usage(response: Any, request: dict[str, Any] | None, text: str, calls: list[ToolCallPart]) -> Usage:
        usage = getattr(response, "usage", None)
        if usage is not None and (usage.prompt_tokens or usage.completion_tokens):
            return model_usage(usage.prompt_tokens, usage.completion_tokens)
        # No usage block: estimate both sides so the token budget still moves.
        input_tokens = estimate_tokens(request["messages"]) if request else 0
        output_chars = len(text) + sum(len(json.dumps(c.args, ensure_ascii=False)) for c in calls)
        return model_usage(input_tokens, output_chars // 3 + 1)


def _tool(spec: ToolSpec, names: ToolNames) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": names.to_wire(spec.name),
            "description": spec.description,
            "parameters": tool_schema(spec),
        },
    }


def _cap(configured: int, cap: int | None) -> int:
    return configured if cap is None else max(1, min(configured, cap))


def _short(exc: Exception, limit: int = 300) -> str:
    text = str(exc)
    return text if len(text) <= limit else text[:limit] + "..."
