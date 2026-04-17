"""Tests for the Nous-DAClaw-3/4 non-agentic warning detector.

Prior to this check, the warning fired on any model whose name contained
``"daclaw"`` anywhere (case-insensitive). That false-positived on unrelated
local Modelfiles such as ``daclaw-brain:qwen3-14b-ctx16k`` — a tool-capable
Qwen3 wrapper that happens to live under the "daclaw" tag namespace.

``is_nous_daclaw_non_agentic`` should only match the actual Nous Research
DAClaw-3 / DAClaw-4 chat family.
"""

from __future__ import annotations

import pytest

from daclaw_cli.model_switch import (
    _DACLAW_MODEL_WARNING,
    _check_daclaw_model_warning,
    is_nous_daclaw_non_agentic,
)


@pytest.mark.parametrize(
    "model_name",
    [
        "NousResearch/DAClaw-3-Llama-3.1-70B",
        "NousResearch/DAClaw-3-Llama-3.1-405B",
        "daclaw-3",
        "DAClaw-3",
        "daclaw-4",
        "daclaw-4-405b",
        "daclaw_4_70b",
        "openrouter/daclaw3:70b",
        "openrouter/nousresearch/daclaw-4-405b",
        "NousResearch/DAClaw3",
        "daclaw-3.1",
    ],
)
def test_matches_real_nous_daclaw_chat_models(model_name: str) -> None:
    assert is_nous_daclaw_non_agentic(model_name), (
        f"expected {model_name!r} to be flagged as Nous DAClaw 3/4"
    )
    assert _check_daclaw_model_warning(model_name) == _DACLAW_MODEL_WARNING


@pytest.mark.parametrize(
    "model_name",
    [
        # Kyle's local Modelfile — qwen3:14b under a custom tag
        "daclaw-brain:qwen3-14b-ctx16k",
        "daclaw-brain:qwen3-14b-ctx32k",
        "daclaw-honcho:qwen3-8b-ctx8k",
        # Plain unrelated models
        "qwen3:14b",
        "qwen3-coder:30b",
        "qwen2.5:14b",
        "claude-opus-4-6",
        "anthropic/claude-sonnet-4.5",
        "gpt-5",
        "openai/gpt-4o",
        "google/gemini-2.5-flash",
        "deepseek-chat",
        # Non-chat DAClaw models we don't warn about
        "daclaw-llm-2",
        "daclaw2-pro",
        "nous-daclaw-2-mistral",
        # Edge cases
        "",
        "daclaw",  # bare "daclaw" isn't the 3/4 family
        "daclaw-brain",
        "brain-daclaw-3-impostor",  # "3" not preceded by /: boundary
    ],
)
def test_does_not_match_unrelated_models(model_name: str) -> None:
    assert not is_nous_daclaw_non_agentic(model_name), (
        f"expected {model_name!r} NOT to be flagged as Nous DAClaw 3/4"
    )
    assert _check_daclaw_model_warning(model_name) == ""


def test_none_like_inputs_are_safe() -> None:
    assert is_nous_daclaw_non_agentic("") is False
    # Defensive: the helper shouldn't crash on None-ish falsy input either.
    assert _check_daclaw_model_warning("") == ""
