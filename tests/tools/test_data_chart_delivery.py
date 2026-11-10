"""Which chat channels get a PNG copy of data charts, and what the model is told."""

import contextvars
from pathlib import Path

import pytest

from agent.prompt_builder import PLATFORM_HINTS
from gateway.session_context import set_session_vars
from tools import data_agent_tools


@pytest.fixture
def fake_export(monkeypatch):
    calls = []

    def _export(html_path, png_path):
        calls.append(png_path)
        return {"ok": True, "path": png_path}

    from agent.data_agent import viz

    monkeypatch.setattr(viz, "export_chart_png", _export)
    return calls


def _decide(platform, args=None, html=Path("/tmp/chart.html")):
    def _run():
        if platform is not None:
            set_session_vars(platform=platform, chat_id="c1")
        return data_agent_tools._maybe_export_png(args or {}, html)

    return contextvars.copy_context().run(_run)


@pytest.mark.parametrize("platform", ["feishu", "mattermost", "matrix", "telegram"])
def test_file_capable_chats_get_png(fake_export, platform):
    out = _decide(platform)
    assert out["deliver_with"].startswith("MEDIA:")
    assert len(fake_export) == 1


@pytest.mark.parametrize("platform", [None, "cli", "web", "dingtalk", "sms", "homeassistant"])
def test_no_png_where_it_cannot_or_need_not_be_sent(fake_export, platform):
    assert _decide(platform) == {}
    assert fake_export == []


def test_export_png_flag_overrides_platform(fake_export):
    assert _decide("dingtalk", {"export_png": True})["png_path"].endswith(".png")
    assert _decide("feishu", {"export_png": False}) == {}


@pytest.mark.parametrize("platform", ["feishu", "mattermost", "matrix"])
def test_media_chat_hints_explain_media_delivery(platform):
    hint = PLATFORM_HINTS[platform]
    assert "MEDIA:/absolute/path" in hint
    assert "deliver_with" in hint


def test_dingtalk_hint_says_files_cannot_be_sent():
    hint = PLATFORM_HINTS["dingtalk"]
    assert "CANNOT" in hint and "MEDIA:" in hint
