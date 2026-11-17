"""The PG runtime through real HTTP and independent API/Worker processes.

Only the scripted model is used. The existing pg_dsn fixture supplies the
migrated test database; input files, permissions and artifacts live in tmp_path.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx
import pytest

for _module in ("sqlalchemy", "asyncpg", "alembic"):
    pytest.importorskip(_module)

from harness.api import token_digest  # noqa: E402
from harness.storage.postgres import PgRepositories  # noqa: E402

from ..pg import _free_port, clean  # noqa: E402

pytestmark = pytest.mark.integration

TOKEN_A, TOKEN_B = "process-test-alice", "process-test-bob"
AUTH_A = {"Authorization": f"Bearer {TOKEN_A}"}
AUTH_B = {"Authorization": f"Bearer {TOKEN_B}"}
DRAFT = "# Draft\n## Goal\nRead the brief.\n## Plan\nRun the harness.\n"


@pytest.fixture
def process_setup(pg_dsn, tmp_path):
    async def reset_database():
        repos = PgRepositories.from_dsn(pg_dsn)
        try:
            await clean(repos.engine)
        finally:
            await repos.close()

    asyncio.run(reset_database())
    material = tmp_path / "material"
    material.mkdir()
    brief = material / "brief.md"
    brief.write_text("Read this brief and write a project draft.", encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "users": {"s1": {"main": {"paths": [str(material)]}}},
        "tokens": [
            {"sha256": token_digest(TOKEN_A), "scope_id": "s1", "actor_id": "alice"},
            {"sha256": token_digest(TOKEN_B), "scope_id": "s2", "actor_id": "bob"},
        ],
    }), encoding="utf-8")
    script = tmp_path / "script.json"
    script.write_text(json.dumps([
        {"tool": "files.read", "args": {"path": str(brief)}},
        {"tool": "artifacts.write", "args": {"name": "draft", "content": DRAFT}},
        {"submit": {"summary": "done", "artifacts": {"draft": "@draft"}}},
    ]), encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("DARAOS_HARNESS_")}
    env.update({
        "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DARAOS_HARNESS_DATABASE_DSN": pg_dsn,
        "DARAOS_HARNESS_ARTIFACT_ROOT": str(tmp_path / "shared-artifacts"),
        "DARAOS_HARNESS_POLICY_FILE": str(policy),
    })
    body = {
        "goal": "Write a project draft from the brief",
        "capability_ref": "document_draft@1",
        "input_refs": [f"file:{brief.as_posix()}"],
        "acceptance": {
            "required_artifacts": ["draft"],
            "checks": [
                {"id": "artifact_readable"},
                {"id": "required_sections", "sections": ["Goal", "Plan"]},
            ],
        },
    }
    return env, body, script


@contextmanager
def child(module, args, env, cwd):
    cwd.mkdir()
    process = subprocess.Popen(
        [sys.executable, "-m", module, *args], cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


@contextmanager
def api_server(env, cwd):
    port = _free_port()
    # No --with-worker, --model-config or --scripted: this process only serves HTTP.
    with child("harness.api", ["--host", "127.0.0.1", "--port", str(port)], env, cwd) as process:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False) as client:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    output, _ = process.communicate()
                    pytest.fail(f"API exited during startup ({process.returncode}): {output}")
                try:
                    if client.get("/healthz", timeout=0.5).status_code == 200:
                        break
                except (httpx.ConnectError, httpx.TimeoutException):
                    pass  # The socket is not ready yet.
                time.sleep(0.05)
            else:
                pytest.fail("API did not become ready within 15 seconds")
            yield client


def worker_report(process, expected_exit=0):
    output, _ = process.communicate(timeout=15)
    assert process.returncode == expected_exit, output
    return json.loads(output.strip())


def sse_events(text):
    events = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if line and not line.startswith(":"))
        if fields:
            fields["data"] = json.loads(fields["data"])
            events.append(fields)
    return events


def test_api_and_worker_share_tasks_across_processes(process_setup, tmp_path):
    env, body, script = process_setup
    headers = {**AUTH_A, "Idempotency-Key": "cross-process-draft"}
    with api_server(env, tmp_path / "api-cwd") as api:
        assert api.post("/tasks", json=body).status_code == 401
        response = api.post("/tasks", json=body, headers=headers)
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]
        assert api.get(f"/tasks/{task_id}", headers=AUTH_A).json()["status"] == "queued"

        with api.stream("GET", f"/tasks/{task_id}/events", headers=AUTH_A) as stream:
            assert stream.status_code == 200
            with child("harness.worker", ["--once", "--scripted", str(script)], env, tmp_path / "worker-cwd") as worker:
                stream.read()
                events = sse_events(stream.text)
                report = worker_report(worker)
        assert report["task_id"] == task_id and report["committed"] and report["status"] == "succeeded"
        assert events[-1]["event"] == "final"
        assert events[-1]["data"]["result_ref"] == report["result_ref"]
        progress = [event for event in events if event["event"] == "progress"]
        assert progress[-1]["data"]["to"] == "succeeded"
        event_ids = [int(event["id"]) for event in progress]
        assert event_ids == sorted(set(event_ids))
        task = api.get(f"/tasks/{task_id}", headers=AUTH_A).json()
        assert task["status"] == "succeeded" and task["usage"]["model_calls"] == 3
        assert task["usage"]["tool_calls"] == 2
        result_ref = task["result_ref"]
        draft = api.get(f"/artifacts/{result_ref}", headers=AUTH_A)
        assert draft.status_code == 200 and draft.text == DRAFT
        trace = api.get(f"/tasks/{task_id}/trace", headers=AUTH_A).json()
        assert [step["tool"] for step in trace["steps"] if step["type"] == "tool_action"] == [
            "files.read", "artifacts.write",
        ]

    # A new API process and a different cwd must still see the database and files.
    with api_server(env, tmp_path / "restarted-api-cwd") as api:
        restored = api.get(f"/tasks/{task_id}", headers=AUTH_A).json()
        for key in ("task_id", "status", "result_ref", "usage", "created_at", "updated_at"):
            assert restored[key] == task[key]
        assert api.get(f"/tasks/{task_id}/trace", headers=AUTH_A).json() == trace
        assert api.get(f"/artifacts/{result_ref}", headers=AUTH_A).text == DRAFT
        repeated = api.post("/tasks", json=body, headers=headers)
        assert repeated.status_code == 202 and repeated.json()["task_id"] == task_id
        assert api.post("/tasks", json={**body, "goal": "different"}, headers=headers).status_code == 409
        cursor = progress[len(progress) // 2]["id"]
        resumed = sse_events(api.get(
            f"/tasks/{task_id}/events", headers={**AUTH_A, "Last-Event-ID": cursor},
        ).text)
        assert [event["id"] for event in resumed if event["event"] == "progress"] == [
            event["id"] for event in progress if int(event["id"]) > int(cursor)
        ]
        assert resumed[-1]["event"] == "final" and resumed[-1]["data"]["status"] == "succeeded"
        for path in (f"/tasks/{task_id}", f"/tasks/{task_id}/trace", f"/tasks/{task_id}/events", f"/artifacts/{result_ref}"):
            assert api.get(path, headers=AUTH_B).status_code == 404
        assert api.post(f"/tasks/{task_id}/cancel", headers=AUTH_B).status_code == 404


def test_cancelled_queued_task_is_not_claimed_by_another_process(process_setup, tmp_path):
    env, body, script = process_setup
    with api_server(env, tmp_path / "api-cwd") as api:
        response = api.post("/tasks", json=body, headers={**AUTH_A, "Idempotency-Key": "cancel-before-worker"})
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]
        cancelled = api.post(f"/tasks/{task_id}/cancel", headers=AUTH_A)
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        with child("harness.worker", ["--once", "--scripted", str(script)], env, tmp_path / "worker-cwd") as worker:
            assert worker_report(worker, expected_exit=1) == {"claimed": None}
        task = api.get(f"/tasks/{task_id}", headers=AUTH_A).json()
        assert task["status"] == "cancelled" and task["result_ref"] is None
        assert task["usage"]["model_calls"] == task["usage"]["tool_calls"] == 0
        trace = api.get(f"/tasks/{task_id}/trace", headers=AUTH_A).json()
        assert not any(step["type"] in {"model_call", "tool_action"} for step in trace["steps"])
