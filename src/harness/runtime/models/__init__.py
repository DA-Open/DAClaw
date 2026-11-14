"""Model adapters (spec 5.2 ModelAdapter).

``create_adapter(ModelConfig(...))`` picks the adapter for the config's wire
protocol. Examples::

    ModelConfig(provider="anthropic", model="claude-sonnet-4-5")
    ModelConfig(provider="deepseek", model="deepseek-chat")
    ModelConfig(provider="qwen", model="qwen-plus")
    ModelConfig(provider="openai", model="gpt-5", reasoning_effort="medium")
    ModelConfig(provider="ollama", model="qwen3:8b")
    ModelConfig(provider="custom", model="my-model",
                base_url="https://relay.example.com/v1", api_key_env="RELAY_API_KEY")
    ModelConfig(provider="custom", model="my-claude", protocol="anthropic_messages",
                base_url="https://relay.example.com", api_key_env="RELAY_API_KEY")
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .anthropic_messages import AnthropicMessagesAdapter
from .base import ModelAdapter, ModelError, estimate_tokens
from .config import PRESETS, ModelConfig, ProviderPreset, WireProtocol
from .openai_chat import OpenAIChatAdapter
from .openai_responses import OpenAIResponsesAdapter
from .scripted import ScriptedModelAdapter, result_step, tool_step

_ADAPTERS = {
    WireProtocol.OPENAI_CHAT: OpenAIChatAdapter,
    WireProtocol.OPENAI_RESPONSES: OpenAIResponsesAdapter,
    WireProtocol.ANTHROPIC_MESSAGES: AnthropicMessagesAdapter,
}


def create_adapter(
    config: ModelConfig,
    *,
    client: Any | None = None,
    env: Mapping[str, str] | None = None,
) -> ModelAdapter:
    resolved = config.resolved()
    return _ADAPTERS[WireProtocol(resolved.protocol)](resolved, client=client, env=env)


__all__ = [
    "PRESETS",
    "AnthropicMessagesAdapter",
    "ModelAdapter",
    "ModelConfig",
    "ModelError",
    "OpenAIChatAdapter",
    "OpenAIResponsesAdapter",
    "ProviderPreset",
    "ScriptedModelAdapter",
    "WireProtocol",
    "create_adapter",
    "estimate_tokens",
    "result_step",
    "tool_step",
]
