"""Minimal API: create / read / cancel (item 7)."""

import httpx
import pytest

from harness.api.app import create_app
from harness.config import HarnessConfig
from harness.contracts import EventType, TaskStatus
from harness.storage.memory import InMemoryRepositories
from harness.tasks.service import TaskService

pytestmark = pytest.mark.asyncio

HEADERS = {"X-Actor-Id": "u1", "X-Scope-Id": "s1"}


def make_app():
    repos = InMemoryRepositories()
    service = TaskService(repos, HarnessConfig())
    return create_app(service), service, repos


def body(**overrides):
    fields = {
        "request_key": "k1",
        "goal": "Draft the notes",
        "capability_ref": "document_draft@1",
    }
    fields.update(overrides)
    return fields


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://harness")


async def test_health():
    app, _, _ = make_app()
    async with client(app) as http:
        response = await http.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_create_then_get_then_cancel():
    app, service, _ = make_app()
    async with client(app) as http:
        created = await http.post("/tasks", json=body(), headers=HEADERS)
        assert created.status_code == 202
        payload = created.json()
        assert payload["agent_role"] == "main"
        assert payload["run_lane"] == "foreground"
        assert payload["status"] == "queued"
        assert created.headers["location"] == f"/tasks/{payload['task_id']}"

        fetched = await http.get(f"/tasks/{payload['task_id']}", headers=HEADERS)
        assert fetched.status_code == 200
        assert fetched.json()["goal"] == "Draft the notes"
        assert fetched.json()["capability_ref"] == "document_draft@1"

        cancelled = await http.post(
            f"/tasks/{payload['task_id']}/cancel",
            json={"reason": "not needed"},
            headers=HEADERS,
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["cancel_reason"] == "not needed"

        again = await http.post(f"/tasks/{payload['task_id']}/cancel", headers=HEADERS)
        assert again.status_code == 200
        assert again.json()["status"] == "cancelled"

    stored = await service.get("s1", payload["task_id"])
    assert stored.status is TaskStatus.CANCELLED


async def test_create_is_idempotent_and_rejects_a_changed_body():
    app, _, repos = make_app()
    async with client(app) as http:
        first = await http.post("/tasks", json=body(), headers=HEADERS)
        second = await http.post("/tasks", json=body(), headers=HEADERS)
        assert first.json()["task_id"] == second.json()["task_id"]

        conflict = await http.post("/tasks", json=body(goal="Other"), headers=HEADERS)
        assert conflict.status_code == 409
        assert conflict.json()["error_code"] == "idempotency_conflict"
        assert conflict.json()["request_id"]

    events = await repos.events.list("s1", limit=100)
    assert len([e for e in events if e.type is EventType.TASK_CREATED]) == 1


async def test_tasks_are_scoped_by_header():
    app, _, _ = make_app()
    async with client(app) as http:
        created = await http.post("/tasks", json=body(), headers=HEADERS)
        task_id = created.json()["task_id"]

        other_scope = await http.get(
            f"/tasks/{task_id}",
            headers={"X-Actor-Id": "u2", "X-Scope-Id": "s2"},
        )
        assert other_scope.status_code == 404
        assert other_scope.json()["error_code"] == "not_found"

        missing = await http.get("/tasks/task_missing", headers=HEADERS)
        assert missing.status_code == 404


async def test_identity_headers_are_optional_in_development():
    app, service, _ = make_app()
    async with client(app) as http:
        created = await http.post("/tasks", json=body())
        assert created.status_code == 202
        fetched = await http.get(f"/tasks/{created.json()['task_id']}")
    assert fetched.status_code == 200
    assert (await service.get("dev", created.json()["task_id"])).actor_id == "dev"


async def test_limits_above_caps_are_a_bad_request():
    app, _, _ = make_app()
    async with client(app) as http:
        response = await http.post(
            "/tasks", json=body(limits={"max_model_calls": 10_000}), headers=HEADERS
        )
    assert response.status_code == 400
    assert response.json()["error_code"] == "limits_exceeded"


async def test_invalid_body_is_rejected_before_the_service():
    app, _, _ = make_app()
    async with client(app) as http:
        response = await http.post("/tasks", json={"request_key": "k1"}, headers=HEADERS)
    assert response.status_code == 422
