"""Which model to call and how: protocol, endpoint, credentials, options.

Three wire protocols cover the providers in use today:

* ``openai_chat``: Chat Completions. OpenAI and nearly every other vendor's
  "OpenAI compatible" endpoint, local servers (vLLM, SGLang, Ollama,
  LM Studio) and relays.
* ``openai_responses``: the OpenAI Responses API, for OpenAI reasoning
  models (keeps reasoning between tool calls).
* ``anthropic_messages``: Anthropic Messages. Claude, and vendors that offer
  an Anthropic-compatible endpoint (MiniMax and others).

A provider preset fills in the endpoint, protocol and the environment
variable that holds the key. ``provider="custom"`` takes them from the config
instead. Any preset value can be overridden.

The API key itself is never stored here: ``api_key_env`` names the variable
it is read from when the adapter is built.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

ENV_PREFIX = "DARAOS_HARNESS_MODEL_"


class WireProtocol(StrEnum):
    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "openai_responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"


@dataclass(frozen=True)
class ProviderPreset:
    protocol: WireProtocol
    base_url: str | None
    api_key_env: str | None
    # Chat Completions only: assistant-message fields to send back with the
    # history. DeepSeek / Kimi / GLM thinking modes need reasoning_content
    # returned during a tool-call loop; other providers reject unknown fields.
    echo_fields: tuple[str, ...] = ()
    # "max_tokens" for most Chat Completions servers; OpenAI itself wants
    # "max_completion_tokens" for reasoning models.
    max_tokens_param: str = "max_tokens"
    note: str = ""


_CHAT = WireProtocol.OPENAI_CHAT

PRESETS: dict[str, ProviderPreset] = {
    "openai": ProviderPreset(
        WireProtocol.OPENAI_RESPONSES,
        "https://api.openai.com/v1",
        "OPENAI_API_KEY",
        max_tokens_param="max_completion_tokens",
        note="Responses API; set protocol=openai_chat for Chat Completions",
    ),
    "anthropic": ProviderPreset(
        WireProtocol.ANTHROPIC_MESSAGES, "https://api.anthropic.com", "ANTHROPIC_API_KEY"
    ),
    "gemini": ProviderPreset(
        _CHAT,
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "GEMINI_API_KEY",
        echo_fields=("extra_content",),
        note="OpenAI-compatible endpoint; thought signatures ride in extra_content",
    ),
    "deepseek": ProviderPreset(
        _CHAT, "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", echo_fields=("reasoning_content",)
    ),
    "qwen": ProviderPreset(
        _CHAT,
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY",
        echo_fields=("reasoning_content",),
        note="DashScope mainland; international: https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    ),
    "kimi": ProviderPreset(
        _CHAT, "https://api.moonshot.cn/v1", "MOONSHOT_API_KEY", echo_fields=("reasoning_content",)
    ),
    "glm": ProviderPreset(
        _CHAT,
        "https://open.bigmodel.cn/api/paas/v4",
        "ZHIPUAI_API_KEY",
        echo_fields=("reasoning_content",),
        note="Zhipu mainland; international: https://api.z.ai/api/paas/v4",
    ),
    "doubao": ProviderPreset(
        _CHAT,
        "https://ark.cn-beijing.volces.com/api/v3",
        "ARK_API_KEY",
        echo_fields=("reasoning_content",),
        note="Volcengine Ark; model is the model or endpoint ID",
    ),
    "minimax": ProviderPreset(
        WireProtocol.ANTHROPIC_MESSAGES,
        "https://api.minimaxi.com/anthropic",
        "MINIMAX_API_KEY",
        note="mainland; international: https://api.minimax.io/anthropic",
    ),
    "xai": ProviderPreset(_CHAT, "https://api.x.ai/v1", "XAI_API_KEY"),
    "mistral": ProviderPreset(_CHAT, "https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "openrouter": ProviderPreset(
        _CHAT, "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", echo_fields=("reasoning_details",)
    ),
    "siliconflow": ProviderPreset(
        _CHAT, "https://api.siliconflow.cn/v1", "SILICONFLOW_API_KEY", echo_fields=("reasoning_content",)
    ),
    "ollama": ProviderPreset(_CHAT, "http://localhost:11434/v1", None),
    "vllm": ProviderPreset(
        _CHAT,
        "http://localhost:8000/v1",
        None,
        echo_fields=("reasoning_content",),
        note="also SGLang, LMDeploy, LM Studio and other local OpenAI-compatible servers",
    ),
    "custom": ProviderPreset(_CHAT, None, None, note="set base_url, and protocol if not openai_chat"),
}


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    protocol: WireProtocol | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    max_output_tokens: int = 4096
    temperature: float | None = None
    # openai_chat: reasoning_effort; openai_responses: reasoning.effort.
    reasoning_effort: str | None = None
    # anthropic_messages: enables extended thinking with this token budget.
    thinking_budget_tokens: int | None = None
    timeout_sec: float = 300.0
    # Retries inside the SDK, for connection errors, 429 and 5xx.
    max_retries: int = 2
    echo_fields: tuple[str, ...] | None = None
    max_tokens_param: str | None = None
    # Sent as is: provider-specific request fields and headers.
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    extra_headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.provider not in PRESETS:
            raise ValueError(f"unknown provider {self.provider!r}; known: {', '.join(PRESETS)}")
        if not self.model:
            raise ValueError("model is required")
        if self.provider == "custom" and not self.base_url:
            raise ValueError("provider 'custom' needs base_url")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.max_tokens_param not in (None, "max_tokens", "max_completion_tokens"):
            raise ValueError("max_tokens_param is max_tokens or max_completion_tokens")
        if self.protocol is not None:
            object.__setattr__(self, "protocol", WireProtocol(self.protocol))

    @property
    def preset(self) -> ProviderPreset:
        return PRESETS[self.provider]

    def resolved(self) -> ModelConfig:
        """This config with every preset default filled in."""
        p = self.preset
        return replace(
            self,
            protocol=self.protocol or p.protocol,
            base_url=self.base_url or p.base_url,
            api_key_env=self.api_key_env if self.api_key_env is not None else p.api_key_env,
            echo_fields=self.echo_fields if self.echo_fields is not None else p.echo_fields,
            max_tokens_param=self.max_tokens_param or p.max_tokens_param,
        )

    def api_key(self, env: Mapping[str, str] | None = None) -> str | None:
        """The key, read from the environment now. None for keyless local servers."""
        name = self.resolved().api_key_env
        if not name:
            return None
        value = (os.environ if env is None else env).get(name, "").strip()
        if not value:
            raise ValueError(f"model API key variable {name} is not set")
        return value

    def safe_dict(self) -> dict[str, Any]:
        """For logs and trajectories: no key, no header values."""
        r = self.resolved()
        return {
            "provider": r.provider,
            "model": r.model,
            "protocol": str(r.protocol),
            "base_url": r.base_url,
            "api_key_env": r.api_key_env,
            "max_output_tokens": r.max_output_tokens,
            "temperature": r.temperature,
            "reasoning_effort": r.reasoning_effort,
            "thinking_budget_tokens": r.thinking_budget_tokens,
            "extra_body_keys": sorted(r.extra_body),
            "extra_header_names": sorted(r.extra_headers),
        }

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ModelConfig:
        """Read ``DARAOS_HARNESS_MODEL_*``: PROVIDER, NAME, PROTOCOL, BASE_URL,
        API_KEY_ENV, MAX_OUTPUT_TOKENS, TEMPERATURE, REASONING_EFFORT,
        THINKING_BUDGET_TOKENS, TIMEOUT_SEC, MAX_RETRIES, EXTRA_BODY (JSON)."""
        env = os.environ if env is None else env

        def get(name: str) -> str | None:
            value = env.get(ENV_PREFIX + name, "").strip()
            return value or None

        def num(name: str, kind: type) -> Any:
            value = get(name)
            if value is None:
                return None
            try:
                return kind(value)
            except ValueError as exc:
                raise ValueError(f"{ENV_PREFIX}{name} is not a {kind.__name__}: {value!r}") from exc

        model = get("NAME")
        if not model:
            raise ValueError(f"{ENV_PREFIX}NAME is required")
        kwargs: dict[str, Any] = {
            "provider": get("PROVIDER") or "custom",
            "model": model,
            "protocol": get("PROTOCOL"),
            "base_url": get("BASE_URL"),
            "api_key_env": get("API_KEY_ENV"),
            "temperature": num("TEMPERATURE", float),
            "reasoning_effort": get("REASONING_EFFORT"),
            "thinking_budget_tokens": num("THINKING_BUDGET_TOKENS", int),
        }
        for name, key, kind in (
            ("MAX_OUTPUT_TOKENS", "max_output_tokens", int),
            ("TIMEOUT_SEC", "timeout_sec", float),
            ("MAX_RETRIES", "max_retries", int),
        ):
            value = num(name, kind)
            if value is not None:
                kwargs[key] = value
        extra = get("EXTRA_BODY")
        if extra:
            try:
                kwargs["extra_body"] = json.loads(extra)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{ENV_PREFIX}EXTRA_BODY is not JSON") from exc
        return cls(**kwargs)
