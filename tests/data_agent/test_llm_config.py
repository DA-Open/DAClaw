import json

import pytest

import daclaw_cli.config
import daclaw_cli.runtime_provider
from agent.data_agent import llm


@pytest.fixture
def no_config_file(monkeypatch):
    monkeypatch.setattr(llm, "_resolve_config_path", lambda explicit=None: None)


def _main_model(monkeypatch, model, runtime):
    monkeypatch.setattr(daclaw_cli.config, "load_config", lambda: {"model": model})
    monkeypatch.setattr(daclaw_cli.runtime_provider, "resolve_runtime_provider", lambda **_: runtime)


def test_explicit_config_file_wins(tmp_path, monkeypatch):
    cfg = {"client_type": "OpenAI", "client_args": {"base_url": "http://x"}, "request_args": {"model": "m"}}
    path = tmp_path / "llm.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    _main_model(monkeypatch, {"default": "other"}, {"base_url": "http://y", "api_key": "k", "provider": "openrouter"})

    assert llm.load_config(str(path)) == cfg


def test_falls_back_to_main_model(no_config_file, monkeypatch):
    _main_model(
        monkeypatch,
        {"default": "deepseek-v4-flash", "provider": "deepseek"},
        {"base_url": "https://api.deepseek.com/v1", "api_key": "sk-test", "provider": "deepseek", "api_mode": "chat_completions"},
    )

    cfg = llm.load_config()

    assert cfg["client_type"] == "OpenAI"
    assert cfg["client_args"] == {"base_url": "https://api.deepseek.com/v1", "api_key": "sk-test"}
    # same normalisation the agent applies: DeepSeek only accepts deepseek-chat / deepseek-reasoner
    assert cfg["request_args"]["model"] == "deepseek-chat"


def test_non_chat_completions_main_model_is_not_used(no_config_file, monkeypatch):
    _main_model(
        monkeypatch,
        {"default": "claude-sonnet-4-6"},
        {"base_url": "https://api.anthropic.com", "api_key": "k", "provider": "anthropic", "api_mode": "anthropic_messages"},
    )

    with pytest.raises(llm.LLMConfigError, match="No LLM config found"):
        llm.load_config()


def test_missing_credentials_raise(no_config_file, monkeypatch):
    _main_model(monkeypatch, {"default": "gpt-4o"}, {"base_url": "https://openrouter.ai/api/v1", "api_key": "", "provider": "openrouter"})

    with pytest.raises(llm.LLMConfigError):
        llm.load_config()
