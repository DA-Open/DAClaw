"""Loop-level check for the context bill.

Two claims, both cheap to verify offline:
  1. running a turn records what was injected (system prompt layers)
  2. recording changes nothing -- the messages sent to the model are identical
     with the recorder on and off

If (2) ever fails, the bill is not observation and must not ship.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.context import events as ctx_events
from daclaw_state import SessionDB
from run_agent import AIAgent


def _tool_defs(*names):
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": n,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _resp(content="done"):
    msg = SimpleNamespace(
        content=content,
        tool_calls=None,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _new_agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._use_prompt_caching = False
    return agent


def _run_turn(agent):
    """Run one turn and return the messages handed to the model."""
    agent.client.chat.completions.create.reset_mock()
    agent.client.chat.completions.create.return_value = _resp()
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        agent.run_conversation("hello")
    return agent.client.chat.completions.create.call_args.kwargs["messages"]


def test_turn_records_system_prompt_layers(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    agent = _new_agent()

    with patch.object(ctx_events, "default_db", return_value=store):
        _run_turn(agent)

    bill = store.injection_bill(agent.session_id)
    assert bill, "no injection events recorded for the turn"
    sources = {row["source"] for row in bill if row["kind"] == "system_prompt"}
    assert "identity" in sources
    total = sum(int(row["est_tokens"] or 0) for row in bill)
    assert total > 0


def test_recording_does_not_change_model_input(tmp_path):
    store = SessionDB(tmp_path / "state.db")

    on_agent = _new_agent()
    with patch.object(ctx_events, "default_db", return_value=store):
        messages_on = _run_turn(on_agent)

    off_agent = _new_agent()
    with (
        patch.object(ctx_events, "record", lambda *a, **k: 0),
        patch.object(ctx_events, "record_system_prompt_layers", lambda *a, **k: 0),
    ):
        messages_off = _run_turn(off_agent)

    assert messages_on == messages_off
