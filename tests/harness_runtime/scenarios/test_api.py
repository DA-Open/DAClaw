"""The P0 HTTP API (api/app.py) over the full system, in process.

P0 completion criterion: Main submits a task through the API -> reads a
file -> writes a draft -> verified -> result readable through the API. Also:
errors use one body and the right status, identity comes only from the
token, another scope sees 404, SSE resumes and ends with "final".
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from harness.api import TokenAuth, create_app, token_digest
from harness.app import build_system
from harness.config import DeploymentCaps, HarnessConfig
from harness.contracts import RunLane, TaskStatus
from harness.runtime.models import ScriptedModelAdapter, result_step, tool_step

from .test_worker import ACCEPTANCE, DRAFT, written_id

pytestmark = pytest.mark.asyncio

A_TOKEN, B_TOKEN = "token-for-alice", "token-for-bob"
A = {"Authorization": f"Bearer {A_TOKEN}"}
B = {"Authorization": f"Bearer {B_TOKEN}"}


def happy(material):
    return [
        tool_step(("files.read", {"path": str(material / "brief.md")})),
        tool_step(("artifacts.write", {"name": "draft", "content": DRAFT})),
        lambda ctx: result_step("done", {"draft": written_id(ctx)}),
    ]


class Api:
    def __init__(self, tmp_path, script=happy, **config):
        self.material = tmp_path / "material"
        self.material.mkdir(exist_ok=True)
        (self.material / "brief.md").write_text("目标 P0；方案 harness；计划两周。", encoding="utf-8")
        cfg = HarnessConfig(artifact_root=tmp_path / "artifacts", **config)
        spec = {
            "disabled_tools": [],
            "users": {"s1": {"main": {"paths": [str(self.material)]}}, "s2": {"main": {"paths": [str(tmp_path / "b")]}}},
            "tokens": [
                {"sha256": token_digest(A_TOKEN), "scope_id": "s1", "actor_id": "alice"},
                {"sha256": token_digest(B_TOKEN), "scope_id": "s2", "actor_id": "bob"},
            ],
        }
        self.system = build_system(cfg, ScriptedModelAdapter(script(self.material)), policy_spec=spec)
        self.app = create_app(self.system, TokenAuth(spec["tokens"]), poll_interval_sec=0.01, keepalive_sec=0.05)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://harness")

    def body(self, **kw):
        data = {
            "goal": "写项目说明草稿",
            "capability_ref": "document_draft@1",
            "acceptance": ACCEPTANCE.model_dump(mode="json"),
            "input_refs": [f"file:{(self.material / 'brief.md').as_posix()}"],
        }
        data.update(kw)
        return data

    async def submit(self, key="k1", headers=A, **kw):
        return await self.client.post("/tasks", json=self.body(**kw), headers={**headers, "Idempotency-Key": key})

    async def run_worker(self, lanes=(RunLane.FOREGROUND, RunLane.BACKGROUND)):
        return await self.system.worker(lanes=list(lanes)).run_once()


def assert_error(resp, status, code):
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["error_code"] == code
    assert set(body) == {"error_code", "message", "retryable", "request_id"}
    assert body["request_id"].startswith("req_") and resp.headers["X-Request-ID"] == body["request_id"]
    assert "Traceback" not in body["message"]


# -- the P0 chain through HTTP -------------------------------------------------------


async def test_submit_run_and_read_the_result(tmp_path):
    api = Api(tmp_path)
    resp = await api.submit()
    assert resp.status_code == 202
    created = resp.json()
    assert created["agent_role"] == "main" and created["run_lane"] == "foreground" and created["status"] == "queued"
    assert created["events_url"] == f"/tasks/{created['task_id']}/events"

    report = await api.run_worker()
    assert report.committed and report.status is TaskStatus.SUCCEEDED

    got = (await api.client.get(f"/tasks/{created['task_id']}", headers=A)).json()
    assert got["status"] == "succeeded" and got["result_ref"] and got["error"] is None
    assert got["usage"]["model_calls"] == 3 and got["usage"]["tool_calls"] == 2
    draft = await api.client.get(f"/artifacts/{got['result_ref']}", headers=A)
    assert draft.status_code == 200 and draft.text == DRAFT
    assert draft.headers["content-type"].startswith("text/markdown")
    assert draft.headers["X-Content-Hash"].startswith("sha256:")

    trace = (await api.client.get(f"/tasks/{created['task_id']}/trace", headers=A)).json()
    assert trace["task"]["status"] == "succeeded"
    assert [s["tool"] for s in trace["steps"] if s["type"] == "tool_action"] == ["files.read", "artifacts.write"]


async def test_same_key_returns_the_same_task_and_other_content_conflicts(tmp_path):
    api = Api(tmp_path)
    first = (await api.submit()).json()
    again = await api.submit()
    assert again.status_code == 202 and again.json()["task_id"] == first["task_id"]
    assert_error(await api.submit(goal="something else"), 409, "idempotency_conflict")
    # The same key in another scope is another task.
    other = await api.submit(headers=B, input_refs=[])
    assert other.status_code == 202 and other.json()["task_id"] != first["task_id"]


async def test_prefer_background_picks_the_lane_not_the_role(tmp_path):
    api = Api(tmp_path)
    created = (await api.submit(prefer_background=True)).json()
    assert created["run_lane"] == "background" and created["agent_role"] == "main"
    assert await api.system.worker(lanes=[RunLane.FOREGROUND]).run_once() is None
    assert (await api.system.worker(lanes=[RunLane.BACKGROUND]).run_once()).committed


# -- identity and scope ------------------------------------------------------------


async def test_no_or_bad_token_is_401(tmp_path):
    api = Api(tmp_path)
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": f"Basic {A_TOKEN}"}, {"Authorization": "Bearer "}):
        assert_error(await api.submit(headers=headers), 401, "unauthenticated")
    assert_error(await api.client.get("/tasks/task_x"), 401, "unauthenticated")


async def test_identity_and_role_cannot_come_from_the_body(tmp_path):
    api = Api(tmp_path)
    for field, value in (("scope_id", "s2"), ("actor_id", "bob"), ("agent_role", "proactive"), ("run_lane", "background")):
        assert_error(await api.submit(**{field: value}), 400, "invalid_request")


async def test_other_scope_sees_404_for_tasks_and_artifacts(tmp_path):
    api = Api(tmp_path)
    task_id = (await api.submit()).json()["task_id"]
    await api.run_worker()
    result_ref = (await api.client.get(f"/tasks/{task_id}", headers=A)).json()["result_ref"]
    for path in (f"/tasks/{task_id}", f"/tasks/{task_id}/trace", f"/tasks/{task_id}/events", f"/artifacts/{result_ref}"):
        assert_error(await api.client.get(path, headers=B), 404, "not_found")
    assert_error(await api.client.post(f"/tasks/{task_id}/cancel", headers=B), 404, "not_found")
    # And B's 404 for a real task looks the same as for a made-up one.
    missing = await api.client.get("/tasks/task_missing", headers=B)
    assert missing.json()["message"].replace("task_missing", "X") == (
        await api.client.get(f"/tasks/{task_id}", headers=B)
    ).json()["message"].replace(task_id, "X")


async def test_input_outside_the_callers_directories_is_400(tmp_path):
    api = Api(tmp_path)
    secret = tmp_path / "secret.env"
    secret.write_text("KEY=1", encoding="utf-8")
    resp = await api.submit(input_refs=[f"file:{secret.as_posix()}"])
    assert_error(resp, 400, "forbidden")
    assert "outside the directories" in resp.json()["message"]
    assert await api.run_worker() is None  # no task was created


# -- bad requests ----------------------------------------------------------------------


@pytest.mark.parametrize("reference", ["file:\x00", "file:~__no_such_harness_user__/brief.md"])
async def test_invalid_input_file_paths_are_400(tmp_path, reference):
    api = Api(tmp_path)
    response = await api.submit(input_refs=[reference])
    assert_error(response, 400, "invalid_request")
    assert "invalid file path" in response.json()["message"]
    assert await api.run_worker() is None


@pytest.mark.parametrize(
    "method,path,status,code",
    [("GET", "/missing", 404, "not_found"), ("PUT", "/tasks", 405, "invalid_request")],
)
async def test_routing_errors_use_the_common_body(tmp_path, method, path, status, code):
    api = Api(tmp_path)
    response = await api.client.request(method, path, headers=A)
    assert_error(response, status, code)
    if status == 405:
        assert response.headers["Allow"] == "POST"


async def test_bad_requests_are_400_with_the_reason(tmp_path):
    api = Api(tmp_path, caps=DeploymentCaps(max_model_calls=30))
    resp = await api.client.post("/tasks", json=api.body(), headers=A)
    assert_error(resp, 400, "invalid_request")
    assert "Idempotency-Key" in resp.json()["message"]
    assert_error(await api.submit(capability_ref="spreadsheet@1"), 400, "invalid_request")
    assert_error(await api.submit(capability_ref="no-version"), 400, "invalid_request")
    resp = await api.submit(acceptance={"checks": [{"id": "facts_match"}]})
    assert_error(resp, 400, "invalid_request")
    assert "facts_match" in resp.json()["message"]
    resp = await api.submit(limits={"max_model_calls": 31})
    assert_error(resp, 400, "limits_exceeded")
    assert_error(await api.submit(goal=""), 400, "invalid_request")
    raw = await api.client.post("/tasks", content=b"{not json", headers={**A, "Idempotency-Key": "x", "Content-Type": "application/json"})
    assert_error(raw, 400, "invalid_request")


async def test_default_limits_apply_when_none_are_given(tmp_path):
    api = Api(tmp_path)
    task_id = (await api.submit()).json()["task_id"]
    got = (await api.client.get(f"/tasks/{task_id}", headers=A)).json()
    assert got["limits"] == api.system.config.default_limits.model_dump(mode="json")


async def test_unexpected_errors_are_500_without_details(tmp_path):
    api = Api(tmp_path)

    async def boom(*args, **kwargs):
        raise RuntimeError("database password is hunter2")

    api.system.tasks.get = boom
    resp = await api.client.get("/tasks/task_x", headers=A)
    assert_error(resp, 500, "internal")
    assert "hunter2" not in resp.text and resp.json()["message"] == "internal error"


# -- cancel ------------------------------------------------------------------------------


async def test_cancel_is_idempotent(tmp_path):
    api = Api(tmp_path)
    task_id = (await api.submit()).json()["task_id"]
    first = await api.client.post(f"/tasks/{task_id}/cancel", json={"reason": "changed my mind"}, headers=A)
    assert first.status_code == 200 and first.json()["status"] == "cancelled" and first.json()["cancel_requested"]
    again = await api.client.post(f"/tasks/{task_id}/cancel", headers=A)
    assert again.status_code == 200 and again.json()["status"] == "cancelled"
    assert await api.run_worker() is None


# -- events (SSE) ------------------------------------------------------------------------


def parse_sse(text: str) -> list[dict]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = [l for l in block.splitlines() if not l.startswith(":")]
        if not lines:
            continue
        item = {}
        for line in lines:
            key, _, value = line.partition(": ")
            item[key] = value
        item["data"] = json.loads(item["data"])
        out.append(item)
    return out


async def test_events_stream_ends_with_final_after_the_commit(tmp_path):
    api = Api(tmp_path)
    task_id = (await api.submit()).json()["task_id"]

    async def read_stream():
        async with api.client.stream("GET", f"/tasks/{task_id}/events", headers=A) as resp:
            assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
            return "".join([chunk async for chunk in resp.aiter_text()])

    reader = asyncio.create_task(read_stream())
    await asyncio.sleep(0.05)
    assert not reader.done()  # still waiting while the task is queued
    await api.run_worker()
    events = parse_sse(await asyncio.wait_for(reader, timeout=5))
    kinds = [e["event"] for e in events]
    assert kinds[-1] == "final" and kinds.count("final") == 1
    assert events[-1]["data"]["status"] == "succeeded" and events[-1]["data"]["result_ref"]
    progress = [e for e in events if e["event"] == "progress"]
    assert progress[0]["data"]["type"] == "task_created"
    assert any(e["data"]["type"] == "verification_recorded" for e in progress)
    # "final" comes after the last status change.
    assert progress[-1]["data"]["type"] == "task_status_changed" and progress[-1]["data"]["to"] == "succeeded"
    ids = [int(e["id"]) for e in progress]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)


async def test_events_resume_from_last_event_id(tmp_path):
    api = Api(tmp_path)
    task_id = (await api.submit()).json()["task_id"]
    await api.run_worker()
    full = parse_sse((await api.client.get(f"/tasks/{task_id}/events", headers=A)).text)
    progress = [e for e in full if e["event"] == "progress"]
    middle = progress[len(progress) // 2]["id"]
    resumed = parse_sse(
        (await api.client.get(f"/tasks/{task_id}/events", headers={**A, "Last-Event-ID": middle})).text
    )
    assert [e["id"] for e in resumed if e["event"] == "progress"] == [
        e["id"] for e in progress if int(e["id"]) > int(middle)
    ]
    assert resumed[-1]["event"] == "final"
    assert_error(await api.client.get(f"/tasks/{task_id}/events", headers={**A, "Last-Event-ID": "abc"}), 400, "invalid_request")


async def test_waiting_task_also_ends_the_stream(tmp_path):
    def asks_for_approval(material):
        return [tool_step(("artifacts.write", {"name": "draft", "content": DRAFT})), lambda ctx: result_step("done", {"draft": written_id(ctx)})]

    api = Api(tmp_path, script=asks_for_approval)
    task_id = (await api.submit(acceptance={**ACCEPTANCE.model_dump(mode="json"), "required_actions": ["send"]})).json()["task_id"]
    await api.run_worker()
    events = parse_sse((await api.client.get(f"/tasks/{task_id}/events", headers=A)).text)
    assert events[-1]["event"] == "final" and events[-1]["data"]["status"] == "waiting"


# -- artifacts -------------------------------------------------------------------------


async def test_artifact_ranges(tmp_path):
    api = Api(tmp_path)
    task_id = (await api.submit()).json()["task_id"]
    await api.run_worker()
    ref = (await api.client.get(f"/tasks/{task_id}", headers=A)).json()["result_ref"]
    whole = DRAFT.encode("utf-8")
    part = await api.client.get(f"/artifacts/{ref}", params={"offset": 2, "length": 5}, headers=A)
    assert part.content == whole[2:7] and part.headers["X-Artifact-Offset"] == "2"
    assert part.headers["X-Artifact-Size"] == str(len(whole))
    assert_error(await api.client.get(f"/artifacts/{ref}", params={"offset": -1}, headers=A), 400, "invalid_request")
    assert_error(await api.client.get("/artifacts/art_missing", headers=A), 404, "not_found")


# -- server entry ------------------------------------------------------------------------


async def test_closed_by_default_without_tokens(tmp_path):
    system = build_system(HarnessConfig(artifact_root=tmp_path / "a"), ScriptedModelAdapter([]), policy_spec={"users": {}})
    app = create_app(system, TokenAuth([]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://h") as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        assert_error(await client.post("/tasks", json={}, headers={"Authorization": "Bearer anything", "Idempotency-Key": "k"}), 401, "unauthenticated")
