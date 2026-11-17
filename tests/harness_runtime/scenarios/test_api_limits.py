"""HTTP request bounds and resource lifetime of active SSE connections."""

import asyncio
from contextlib import suppress
from types import SimpleNamespace

import pytest

from harness.api import TokenAuth, create_app, token_digest
from harness.api.app import MAX_GOAL_CHARACTERS, MAX_INPUT_REFS, MAX_INPUT_REF_CHARACTERS
from harness.contracts import Event, EventType

from .test_api import A, B, A_TOKEN, B_TOKEN, Api, assert_error

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("fields", [
    {"goal": "x" * (MAX_GOAL_CHARACTERS + 1)},
    {"input_refs": ["artifact:x"] * (MAX_INPUT_REFS + 1)},
    {"input_refs": ["x" * (MAX_INPUT_REF_CHARACTERS + 1)]},
])
async def test_oversized_task_is_rejected_before_creation(tmp_path, fields):
    api = Api(tmp_path)
    assert_error(await api.submit(**fields), 400, "invalid_request")
    assert await api.run_worker() is None


async def test_request_bounds_are_inclusive_and_count_unicode_characters(tmp_path):
    api = Api(tmp_path)
    response = await api.submit(
        goal="中" * MAX_GOAL_CHARACTERS,
        input_refs=["x" * MAX_INPUT_REF_CHARACTERS] * MAX_INPUT_REFS,
    )
    assert response.status_code == 202


async def test_authentication_precedes_size_validation(tmp_path):
    api = Api(tmp_path)
    assert_error(await api.submit(headers={}, goal="x" * (MAX_GOAL_CHARACTERS + 1)), 401, "unauthenticated")


class OpenStream:
    """Drive a real ASGI connection without buffering an infinite SSE body."""

    def __init__(self, app, task_id, token=A_TOKEN):
        self.started = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.messages = []
        self.sent_request = False
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": f"/tasks/{task_id}/events", "query_string": b"", "root_path": "",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "server": ("harness", 80), "client": ("127.0.0.1", 1234),
        }
        self.task = asyncio.create_task(app(scope, self.receive, self.send))

    async def receive(self):
        if not self.sent_request:
            self.sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self.disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(self, message):
        self.messages.append(message)
        if message["type"] == "http.response.start":
            self.started.set()

    async def start(self):
        await asyncio.wait_for(self.started.wait(), 2)
        return self.messages[0]["status"]

    async def close(self):
        self.disconnected.set()
        await asyncio.wait_for(self.task, 2)


async def test_sse_scope_and_process_limits_and_disconnect_release(tmp_path):
    api = Api(tmp_path, sse_max_connections=2, sse_max_connections_per_scope=1)
    task_a = (await api.submit()).json()["task_id"]
    task_b = (await api.submit(headers=B, input_refs=[])).json()["task_id"]
    streams = []
    try:
        first = OpenStream(api.app, task_a)
        streams.append(first)
        assert await first.start() == 200
        refused = await api.client.get(f"/tasks/{task_a}/events", headers=A)
        assert_error(refused, 429, "limits_exceeded")
        assert refused.json()["retryable"] and refused.headers["Retry-After"] == "1"
        second = OpenStream(api.app, task_b, B_TOKEN)
        streams.append(second)
        assert await second.start() == 200
        await first.close()
        streams.remove(first)
        replacement = OpenStream(api.app, task_a)
        streams.append(replacement)
        assert await replacement.start() == 200
    finally:
        for stream in streams:
            await stream.close()


async def test_sse_global_limit_applies_across_scopes(tmp_path):
    api = Api(tmp_path, sse_max_connections=1, sse_max_connections_per_scope=2)
    task_a = (await api.submit()).json()["task_id"]
    task_b = (await api.submit(headers=B, input_refs=[])).json()["task_id"]
    first = OpenStream(api.app, task_a)
    try:
        assert await first.start() == 200
        assert_error(await api.client.get(f"/tasks/{task_b}/events", headers=B), 429, "limits_exceeded")
    finally:
        await first.close()


async def test_sse_cancellation_releases_capacity(tmp_path):
    api = Api(tmp_path, sse_max_connections=1)
    task_id = (await api.submit()).json()["task_id"]
    first = OpenStream(api.app, task_id)
    assert await first.start() == 200
    first.task.cancel()
    with suppress(asyncio.CancelledError):
        await first.task
    replacement = OpenStream(api.app, task_id)
    try:
        assert await replacement.start() == 200
    finally:
        await replacement.close()


async def test_sse_final_releases_capacity(tmp_path):
    api = Api(tmp_path, sse_max_connections=1)
    task_id = (await api.submit()).json()["task_id"]
    await api.client.post(f"/tasks/{task_id}/cancel", headers=A)
    for _ in range(2):
        response = await api.client.get(f"/tasks/{task_id}/events", headers=A)
        assert response.status_code == 200 and "event: final" in response.text


async def test_sse_iterator_error_releases_capacity(tmp_path, monkeypatch):
    api = Api(tmp_path, sse_max_connections=1)
    task_id = (await api.submit()).json()["task_id"]
    original = api.system.repos.events.list

    async def fail(*args, **kwargs):
        raise RuntimeError("event repository unavailable")

    monkeypatch.setattr(api.system.repos.events, "list", fail)
    stream = OpenStream(api.app, task_id)
    with pytest.raises(RuntimeError, match="event repository unavailable"):
        await asyncio.wait_for(stream.task, 2)
    monkeypatch.setattr(api.system.repos.events, "list", original)
    replacement = OpenStream(api.app, task_id)
    try:
        assert await replacement.start() == 200
    finally:
        await replacement.close()


async def test_idle_polling_backs_off_caps_and_resets_on_events(tmp_path, monkeypatch):
    from harness.api import app as api_module

    api = Api(tmp_path)
    task_id = (await api.submit()).json()["task_id"]
    app = create_app(
        api.system, TokenAuth([{"sha256": token_digest(A_TOKEN), "scope_id": "s1", "actor_id": "alice"}]),
        poll_interval_sec=0.5, max_poll_interval_sec=2, keepalive_sec=4,
    )
    delays = []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 6:
            await api.system.repos.events.append(Event(
                scope_id="s1", task_id=task_id, type=EventType.EXTERNAL,
                payload={"message": "new progress"},
            ))
        elif len(delays) == 8:
            await api.system.tasks.request_cancel("s1", task_id, "test finished")
        await asyncio.sleep(0)

    monkeypatch.setattr(api_module, "asyncio", SimpleNamespace(sleep=sleep))
    stream = OpenStream(app, task_id)
    await asyncio.wait_for(stream.task, 2)
    assert delays == [0.5, 1, 2, 0.5, 2, 2, 0.5, 1]
    body = b"".join(m.get("body", b"") for m in stream.messages)
    assert b": keep-alive" in body and b"event: final" in body
