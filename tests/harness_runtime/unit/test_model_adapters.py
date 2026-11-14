"""Model adapters: request building and response parsing, no network.

Responses are built with the SDKs' own types, so a field the SDK renames
breaks these tests rather than a live run.
"""

import pytest
from anthropic.types import Message as AnthropicMessage
from openai.types.chat import ChatCompletion
from openai.types.responses import Response

from harness.contracts import Acceptance, ContextBundle, Limits, RemainingBudget, StepKind, Usage
from harness.runtime import messages as msg
from harness.runtime.models import (
    PRESETS,
    AnthropicMessagesAdapter,
    ModelConfig,
    ModelError,
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
    WireProtocol,
    create_adapter,
)

from ..fakes import READ

pytestmark = pytest.mark.asyncio


def bundle(messages, tools=(READ, msg.SUBMIT_RESULT_SPEC)):
    return ContextBundle(
        task_id="task_1",
        goal="g",
        acceptance=Acceptance(),
        messages=messages,
        tool_specs=list(tools),
        remaining_budget=RemainingBudget.compute(Limits(), Usage(), 0),
    )


HISTORY = [
    msg.system("You draft documents."),
    msg.user("Summarise notes.md"),
    msg.Message(
        role="assistant",
        content="Reading it.",
        tool_calls=[msg.ToolCallPart(id="call_1", name="files.read", args={"path": "notes.md"})],
    ).to_dict(),
    msg.tool_result("call_1", "files.read", "hello"),
    msg.user("[budget] Remaining: model calls 4/20", kind="budget_reminder"),
]


class Recorder:
    """Fake SDK client: records the request and returns a fixed response."""

    def __init__(self, response=None, error=None):
        self.requests = []
        self._response = response
        self._error = error
        self.chat = self.completions = self.messages = self.responses = self

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if self._error:
            raise self._error
        return self._response


# -- config -----------------------------------------------------------------------


async def test_every_preset_builds_an_adapter():
    for name, preset in PRESETS.items():
        cfg = ModelConfig(provider=name, model="m", base_url="http://localhost/v1" if name == "custom" else None)
        env = {preset.api_key_env: "k"} if preset.api_key_env else {}
        adapter = create_adapter(cfg, env=env)
        assert adapter.protocol == cfg.resolved().protocol


async def test_custom_needs_base_url_and_can_pick_protocol():
    with pytest.raises(ValueError):
        ModelConfig(provider="custom", model="m")
    cfg = ModelConfig(
        provider="custom",
        model="m",
        base_url="https://relay.example.com",
        protocol="anthropic_messages",
        api_key_env="RELAY_KEY",
    )
    assert isinstance(create_adapter(cfg, env={"RELAY_KEY": "k"}), AnthropicMessagesAdapter)


async def test_missing_key_is_an_error_and_key_never_in_safe_dict():
    cfg = ModelConfig(provider="deepseek", model="deepseek-chat")
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        cfg.api_key(env={})
    safe = cfg.safe_dict()
    assert safe["api_key_env"] == "DEEPSEEK_API_KEY" and "sk-" not in str(safe)


async def test_from_env():
    cfg = ModelConfig.from_env(
        {
            "DARAOS_HARNESS_MODEL_PROVIDER": "custom",
            "DARAOS_HARNESS_MODEL_NAME": "gpt-x",
            "DARAOS_HARNESS_MODEL_BASE_URL": "https://relay.example.com/v1",
            "DARAOS_HARNESS_MODEL_API_KEY_ENV": "RELAY_KEY",
            "DARAOS_HARNESS_MODEL_MAX_OUTPUT_TOKENS": "2048",
            "DARAOS_HARNESS_MODEL_EXTRA_BODY": '{"top_k": 20}',
        }
    )
    r = cfg.resolved()
    assert r.protocol is WireProtocol.OPENAI_CHAT and r.max_output_tokens == 2048
    assert dict(r.extra_body) == {"top_k": 20}


# -- Chat Completions ---------------------------------------------------------------


def chat_response(message, usage=None, finish="tool_calls"):
    data = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", **message}}],
    }
    if usage is not False:
        data["usage"] = usage or {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    return ChatCompletion.model_validate(data)


def chat_adapter(response=None, **cfg):
    client = Recorder(response)
    cfg.setdefault("base_url", "http://x/v1")
    config = ModelConfig(provider=cfg.pop("provider", "custom"), model="m", **cfg)
    return OpenAIChatAdapter(config, client=client), client


async def test_chat_request_shape():
    adapter, _ = chat_adapter()
    req = adapter.build_request(bundle(HISTORY))
    roles = [m["role"] for m in req["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assert req["messages"][2]["tool_calls"][0]["function"] == {"name": "files_read", "arguments": '{"path": "notes.md"}'}
    assert [t["function"]["name"] for t in req["tools"]] == ["files_read", "submit_result"]
    assert req["max_tokens"] == 4096


async def test_chat_parses_tool_calls_back_to_harness_names():
    resp = chat_response(
        {"content": None, "tool_calls": [{"id": "c9", "type": "function", "function": {"name": "files_read", "arguments": '{"path": "b.md"}'}}]}
    )
    adapter, client = chat_adapter(resp)
    step = await adapter.next_step(bundle(HISTORY))
    assert step.kind is StepKind.TOOL_CALLS
    assert step.tool_calls[0].tool == "files.read" and step.tool_calls[0].native_call_id == "c9"
    assert step.usage == Usage(model_calls=1, input_tokens=100, output_tokens=20)
    assert client.requests[0]["model"] == "m"


async def test_chat_submit_result_becomes_candidate():
    args = '{"summary": "done", "artifacts": {"draft": "art_1"}}'
    resp = chat_response({"tool_calls": [{"id": "c1", "type": "function", "function": {"name": "submit_result", "arguments": args}}]})
    adapter, _ = chat_adapter(resp)
    step = await adapter.next_step(bundle(HISTORY))
    assert step.kind is StepKind.CANDIDATE_RESULT
    assert step.candidate.artifact_refs == {"draft": "art_1"}


async def test_chat_plain_text_becomes_candidate():
    adapter, _ = chat_adapter(chat_response({"content": "All done."}, finish="stop"))
    step = await adapter.next_step(bundle(HISTORY))
    assert step.kind is StepKind.CANDIDATE_RESULT and step.candidate.summary == "All done."


async def test_chat_bad_json_arguments_are_kept_and_flagged():
    resp = chat_response({"tool_calls": [{"id": "c1", "type": "function", "function": {"name": "files_read", "arguments": "{oops"}}]})
    adapter, _ = chat_adapter(resp)
    step = await adapter.next_step(bundle(HISTORY))
    part = msg.Message.model_validate(step.message).tool_calls[0]
    assert part.invalid and part.raw_args == "{oops"


async def test_chat_empty_answer_is_a_retryable_error():
    adapter, _ = chat_adapter(chat_response({"content": ""}, finish="stop"))
    with pytest.raises(ModelError) as info:
        await adapter.next_step(bundle(HISTORY))
    assert info.value.retryable and info.value.usage.input_tokens == 100


async def test_chat_reasoning_content_is_echoed_only_where_required():
    resp = chat_response(
        {
            "content": None,
            "reasoning_content": "think think",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "files_read", "arguments": "{}"}}],
        }
    )
    deepseek, _ = chat_adapter(resp, provider="deepseek", base_url="http://x/v1")
    step = await deepseek.next_step(bundle(HISTORY))
    assert msg.Message.model_validate(step.message).reasoning == "think think"
    history = [*HISTORY, step.message, msg.tool_result("c1", "files.read", "x")]
    assert deepseek.build_request(bundle(history))["messages"][-2]["reasoning_content"] == "think think"
    plain, _ = chat_adapter(resp)
    assert "reasoning_content" not in plain.build_request(bundle(history))["messages"][-2]


async def test_chat_without_usage_estimates_tokens():
    adapter, _ = chat_adapter(chat_response({"content": "ok"}, usage=False, finish="stop"))
    step = await adapter.next_step(bundle(HISTORY))
    assert step.usage.input_tokens > 0 and step.usage.output_tokens > 0


async def test_chat_output_cap_and_options():
    adapter, _ = chat_adapter(temperature=0.2, reasoning_effort="low", extra_body={"top_k": 5})
    req = adapter.build_request(bundle(HISTORY), max_output_tokens=500)
    assert req["max_tokens"] == 500 and req["temperature"] == 0.2
    assert req["reasoning_effort"] == "low" and req["extra_body"] == {"top_k": 5}
    openai_chat = OpenAIChatAdapter(
        ModelConfig(provider="openai", model="gpt", protocol="openai_chat"), client=Recorder()
    )
    assert "max_completion_tokens" in openai_chat.build_request(bundle(HISTORY))


async def test_chat_http_errors_are_classified():
    import httpx
    import openai

    request = httpx.Request("POST", "http://x/v1/chat/completions")
    for status, retryable in ((429, True), (500, True), (400, False), (401, False)):
        err = openai.APIStatusError("boom", response=httpx.Response(status, request=request), body=None)
        adapter = OpenAIChatAdapter(ModelConfig(provider="custom", model="m", base_url="http://x/v1"), client=Recorder(error=err))
        with pytest.raises(ModelError) as info:
            await adapter.next_step(bundle(HISTORY))
        assert info.value.retryable is retryable and info.value.details["responded"] is True
    timeout = openai.APITimeoutError(request=request)
    adapter = OpenAIChatAdapter(ModelConfig(provider="custom", model="m", base_url="http://x/v1"), client=Recorder(error=timeout))
    with pytest.raises(ModelError) as info:
        await adapter.next_step(bundle(HISTORY))
    assert info.value.retryable and info.value.details["responded"] is False


# -- Anthropic Messages ---------------------------------------------------------------


def anthropic_response(content, stop="tool_use"):
    return AnthropicMessage.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude",
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 80, "output_tokens": 30, "cache_read_input_tokens": 20},
        }
    )


def anthropic_adapter(response=None, **cfg):
    client = Recorder(response)
    config = ModelConfig(provider="anthropic", model="claude", **cfg)
    return AnthropicMessagesAdapter(config, client=client), client


async def test_anthropic_request_shape():
    adapter, _ = anthropic_adapter()
    req = adapter.build_request(bundle(HISTORY))
    assert req["system"] == "You draft documents."
    roles = [m["role"] for m in req["messages"]]
    assert roles == ["user", "assistant", "user"]  # tool result + reminder merged into one user turn
    merged = req["messages"][2]["content"]
    assert merged[0]["type"] == "tool_result" and merged[0]["tool_use_id"] == "call_1"
    assert merged[1]["type"] == "text"
    assert req["tools"][0]["name"] == "files_read" and "input_schema" in req["tools"][0]


async def test_anthropic_thinking_blocks_are_replayed_unchanged():
    resp = anthropic_response(
        [
            {"type": "thinking", "thinking": "plan", "signature": "sig123"},
            {"type": "tool_use", "id": "tu_1", "name": "files_read", "input": {"path": "a"}},
        ]
    )
    adapter, _ = anthropic_adapter(resp, thinking_budget_tokens=2000)
    step = await adapter.next_step(bundle(HISTORY))
    assert step.kind is StepKind.TOOL_CALLS and step.tool_calls[0].tool == "files.read"
    assert step.usage.input_tokens == 100  # cache reads count
    history = [*HISTORY, step.message, msg.tool_result("tu_1", "files.read", "x")]
    req = adapter.build_request(bundle(history))
    replayed = req["messages"][-2]["content"]
    assert replayed[0] == {"type": "thinking", "thinking": "plan", "signature": "sig123"}
    assert req["thinking"]["budget_tokens"] == 2000 and "temperature" not in req


async def test_anthropic_text_answer_is_candidate():
    adapter, _ = anthropic_adapter(anthropic_response([{"type": "text", "text": "Finished."}], stop="end_turn"))
    step = await adapter.next_step(bundle(HISTORY))
    assert step.kind is StepKind.CANDIDATE_RESULT and step.candidate.summary == "Finished."


async def test_history_from_another_protocol_converts():
    chat, _ = chat_adapter(chat_response({"content": None, "reasoning_content": "r", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "files_read", "arguments": "{}"}}]}))
    step = await chat.next_step(bundle(HISTORY))
    history = [*HISTORY, step.message, msg.tool_result("c1", "files.read", "x")]
    adapter, _ = anthropic_adapter()
    req = adapter.build_request(bundle(history))
    assert req["messages"][-2]["content"][-1] == {"type": "tool_use", "id": "c1", "name": "files_read", "input": {}}


# -- Responses ------------------------------------------------------------------------


def responses_response(output):
    return Response.model_validate(
        {
            "id": "resp_1",
            "object": "response",
            "created_at": 0,
            "model": "gpt",
            "status": "completed",
            "output": output,
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "usage": {
                "input_tokens": 50,
                "output_tokens": 10,
                "total_tokens": 60,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 4},
            },
        }
    )


async def test_responses_round_trip_keeps_reasoning_items():
    resp = responses_response(
        [
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc"},
            {"type": "function_call", "id": "fc_1", "call_id": "call_7", "name": "files_read", "arguments": '{"path": "a"}', "status": "completed"},
        ]
    )
    client = Recorder(resp)
    adapter = OpenAIResponsesAdapter(ModelConfig(provider="openai", model="gpt", reasoning_effort="medium"), client=client)
    step = await adapter.next_step(bundle(HISTORY))
    assert step.kind is StepKind.TOOL_CALLS and step.tool_calls[0].native_call_id == "call_7"
    first = client.requests[0]
    assert first["store"] is False and first["include"] == ["reasoning.encrypted_content"]
    assert first["instructions"] == "You draft documents."
    history = [*HISTORY, step.message, msg.tool_result("call_7", "files.read", "x")]
    items = adapter.build_request(bundle(history))["input"]
    assert items[-3]["type"] == "reasoning" and items[-3]["encrypted_content"] == "enc"
    assert items[-1] == {"type": "function_call_output", "call_id": "call_7", "output": "x"}
