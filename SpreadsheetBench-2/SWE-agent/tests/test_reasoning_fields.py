import json
from types import SimpleNamespace
from unittest.mock import Mock

from litellm.types.utils import Message

from sweagent.agent.hooks.abstract import AbstractAgentHook, CombinedAgentHook
from sweagent.agent.models import GenericAPIModelConfig, LiteLLMModel, _extract_reasoning_fields


def _history_model() -> LiteLLMModel:
    model = object.__new__(LiteLLMModel)
    model.config = GenericAPIModelConfig(name="custom/test-model")
    model.logger = Mock()
    return model


def test_openai_encrypted_reasoning_is_preserved_but_not_displayed() -> None:
    reasoning_items = [
        {
            "type": "reasoning",
            "id": "rs_123",
            "encrypted_content": "encrypted-value",
            "summary": [],
        }
    ]
    message = Message(
        content=None,
        reasoning_content="",
        reasoning_items=reasoning_items,
    )

    result = _extract_reasoning_fields(message)

    assert result["reasoning_content"] == ""
    assert result["reasoning_items"] == reasoning_items
    assert "reasoning_text" not in result
    json.dumps(result)


def test_openai_reasoning_summary_becomes_display_text() -> None:
    message = SimpleNamespace(
        reasoning_content="",
        reasoning_items=[
            {
                "type": "reasoning",
                "encrypted_content": "must-not-be-displayed",
                "summary": [{"type": "summary_text", "text": "Inspected the workbook."}],
            }
        ],
        provider_specific_fields=None,
    )

    result = _extract_reasoning_fields(message)

    assert result["reasoning_text"] == "Inspected the workbook."
    assert "must-not-be-displayed" not in result["reasoning_text"]


def test_all_native_reasoning_fields_are_extracted_independently() -> None:
    thinking_blocks = [{"type": "thinking", "thinking": "Claude text", "signature": "signature"}]
    reasoning_items = [
        {
            "type": "reasoning",
            "encrypted_content": "encrypted-value",
            "summary": [{"type": "summary_text", "text": "OpenAI summary"}],
        }
    ]
    reasoning_details = [{"type": "reasoning.text", "text": "Provider detail"}]
    message = SimpleNamespace(
        reasoning_content="Direct reasoning",
        thinking_blocks=thinking_blocks,
        reasoning_items=reasoning_items,
        provider_specific_fields={"reasoning_details": reasoning_details},
    )

    result = _extract_reasoning_fields(message)

    assert result["reasoning_content"] == "Direct reasoning"
    assert result["thinking_blocks"] == thinking_blocks
    assert result["reasoning_items"] == reasoning_items
    assert result["reasoning_details"] == reasoning_details
    assert result["reasoning_text"] == "Direct reasoning"


def test_claude_and_provider_reasoning_have_safe_display_fallbacks() -> None:
    claude = SimpleNamespace(
        thinking_blocks=[
            {"type": "thinking", "thinking": "Readable thinking", "signature": "signature"},
            {"type": "redacted_thinking", "data": "redacted-data"},
        ],
        provider_specific_fields=None,
    )
    provider = SimpleNamespace(
        provider_specific_fields={
            "reasoning_details": [
                {"type": "reasoning.text", "text": "Readable provider reasoning"},
                {"type": "reasoning.encrypted", "data": "encrypted-provider-data"},
            ]
        }
    )

    claude_result = _extract_reasoning_fields(claude)
    provider_result = _extract_reasoning_fields(provider)

    assert claude_result["reasoning_text"] == "Readable thinking"
    assert "redacted-data" not in claude_result["reasoning_text"]
    assert provider_result["reasoning_text"] == "Readable provider reasoning"
    assert "encrypted-provider-data" not in provider_result["reasoning_text"]


def test_tool_call_history_round_trips_only_native_reasoning_fields() -> None:
    model = _history_model()
    reasoning_items = [
        {
            "type": "reasoning",
            "id": "rs_123",
            "encrypted_content": "encrypted-value",
            "summary": [],
        }
    ]
    history = [
        {
            "role": "assistant",
            "content": "",
            "message_type": "action",
            "tool_calls": [{"id": "call_123", "type": "function", "function": {"name": "tool"}}],
            "reasoning_content": "",
            "reasoning_text": "Display-only text",
            "reasoning_items": reasoning_items,
        }
    ]

    messages = model._history_to_messages(history)

    assert messages[0]["reasoning_content"] == ""
    assert messages[0]["reasoning_items"] == reasoning_items
    assert "reasoning_text" not in messages[0]


def test_non_tool_history_does_not_receive_reasoning_fields() -> None:
    model = _history_model()
    history = [
        {
            "role": "assistant",
            "content": "Final answer",
            "message_type": "action",
            "reasoning_content": "Internal reasoning",
            "reasoning_items": [{"type": "reasoning", "encrypted_content": "encrypted-value"}],
        }
    ]

    messages = model._history_to_messages(history)

    assert messages == [{"role": "assistant", "content": "Final answer"}]


def test_combined_hook_forwards_all_reasoning_fields() -> None:
    hook = AbstractAgentHook()
    hook.on_query_message_added = Mock()
    combined = CombinedAgentHook([hook])
    thinking_blocks = [{"type": "thinking", "thinking": "text"}]
    reasoning_items = [{"type": "reasoning", "encrypted_content": "encrypted-value"}]

    combined.on_query_message_added(
        agent="main",
        role="assistant",
        content="",
        message_type="action",
        thinking_blocks=thinking_blocks,
        reasoning_content="",
        reasoning_text="display",
        reasoning_items=reasoning_items,
    )

    kwargs = hook.on_query_message_added.call_args.kwargs
    assert kwargs["thinking_blocks"] == thinking_blocks
    assert kwargs["reasoning_content"] == ""
    assert kwargs["reasoning_text"] == "display"
    assert kwargs["reasoning_items"] == reasoning_items
