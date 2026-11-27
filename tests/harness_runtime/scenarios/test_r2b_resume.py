"""Resume waiting tasks through the HTTP API over both repository backends."""

from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio

from harness.api import TokenAuth, create_app, token_digest
from harness.api.app import MAX_INPUT_REF_CHARACTERS, MAX_INPUT_REFS
from harness.app import build_system
from harness.config import HarnessConfig
from harness.contracts import (
    ArtifactInput, CheckResult, EventType, RunLane, TaskOutcome, TaskStatus, VerificationStatus, WaitCondition,
)
from harness.results import DEFAULT_CHECKERS
from harness.results.verifier import artifact_readable
from harness.runtime import messages as msg
from harness.runtime.models import ScriptedModelAdapter, result_step, tool_step

from ..unit.test_p1_reconciliation_repositories import task_in_state
from ..unit.test_repositories import repos
from .test_api import assert_error
from .test_worker import written_id

pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def open_api(repos, tmp_path, script=()):
    policy = {"users": {"s1": {"main": {"paths": [str(tmp_path / "inputs")]}}}}
    system = build_system(HarnessConfig(artifact_root=tmp_path / "artifacts"), ScriptedModelAdapter(script),
                          policy_spec=policy, repos=repos)
    auth = TokenAuth([
        {"sha256": token_digest(token), "scope_id": scope, "actor_id": actor}
        for token, scope, actor in [("owner", "s1", "u1"), ("peer", "s1", "u2"), ("foreign", "s2", "u1")]
    ])
    transport = httpx.ASGITransport(app=create_app(system, auth))
    async with system.lifespan(), httpx.AsyncClient(
        transport=transport, base_url="http://harness", headers={"Authorization": "Bearer owner"},
    ) as client:
        yield system, client


@pytest_asyncio.fixture
async def api(repos, tmp_path):
    async with open_api(repos, tmp_path) as active:
        yield active


async def submit(client, input_refs, **fields):
    response = await client.post("/tasks", headers={"Idempotency-Key": "resume-example"}, json={
        "goal": "Complete the draft when the source is available.", "capability_ref": "document_draft@1",
        "input_refs": input_refs, **fields,
    })
    assert response.status_code == 202, response.text
    return response.json()["task_id"]


async def waiting_input(api, refs):
    system, client = api
    task_id = await submit(client, refs)
    lease = await system.tasks.claim("first-worker", [RunLane.FOREGROUND])
    assert lease.task_id == task_id
    return await system.tasks.commit(lease, TaskOutcome(status=TaskStatus.WAITING, wait_condition=WaitCondition.INPUT))


async def test_resume_waiting_input_with_new_refs(api):
    system, client = api
    task = await waiting_input(api, ["artifact:old"])
    url = f"/tasks/{task.id}"
    view = await client.get(url)
    assert view.status_code == 200 and view.json()["version"] == task.version
    before = await system.repos.events.list("s1", task_id=task.id)

    response = await client.post(f"{url}/resume", json={
        "expected_version": view.json()["version"],
        "input_refs": ["artifact:z", "artifact:old", "artifact:z", "artifact:a"],
    })
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued" and response.json()["version"] == task.version + 1
    stored = await system.tasks.get("s1", task.id)
    assert stored.status is TaskStatus.QUEUED and stored.wait_condition is None
    assert stored.version == task.version + 1
    assert stored.input_refs == ["artifact:old", "artifact:z", "artifact:a"]
    events = await system.repos.events.list("s1", task_id=task.id, after_seq=before[-1].seq)
    assert len(events) == 1 and events[0].type is EventType.TASK_STATUS_CHANGED
    assert events[0].occurred_at == stored.updated_at
    assert events[0].payload["from"] == "waiting" and events[0].payload["to"] == "queued"
    assert events[0].payload["reason"] == "resumed_with_input"
    assert events[0].payload["added_input_refs"] == ["artifact:z", "artifact:a"]


@pytest.mark.parametrize(("status", "condition"), [
    (TaskStatus.WAITING, WaitCondition.APPROVAL), (TaskStatus.WAITING, WaitCondition.RECONCILIATION),
    (TaskStatus.WAITING, WaitCondition.DEPENDENCY), (TaskStatus.QUEUED, None), (TaskStatus.RUNNING, None),
    (TaskStatus.CANCELLED, None),
])
async def test_resume_cannot_bypass_approval_or_reconciliation(api, status, condition):
    system, client = api
    task = await task_in_state(system.repos, "unavailable", status, condition)
    response = await client.post(f"/tasks/{task.id}/resume",
                                 json={"expected_version": task.version, "input_refs": ["artifact:new"]})
    assert_error(response, 409, "invalid_transition")
    assert await system.tasks.get("s1", task.id) == task


async def test_resume_version_conflict_409(api):
    system, client = api
    task = await waiting_input(api, ["artifact:old"])
    before = await system.repos.events.list("s1", task_id=task.id)
    response = await client.post(f"/tasks/{task.id}/resume",
                                 json={"expected_version": task.version - 1, "input_refs": ["artifact:new"]})
    assert_error(response, 409, "version_conflict")
    assert await system.tasks.get("s1", task.id) == task
    assert await system.repos.events.list("s1", task_id=task.id) == before


async def test_resume_checks_version_before_counting_merged_inputs(api):
    system, client = api
    task = await waiting_input(api, ["artifact:old"] * MAX_INPUT_REFS)
    response = await client.post(f"/tasks/{task.id}/resume", json={
        "expected_version": task.version - 1, "input_refs": ["artifact:new"],
    })
    assert_error(response, 409, "version_conflict")
    assert await system.tasks.get("s1", task.id) == task


async def test_resume_auth_and_path_whitelist(api, tmp_path):
    system, client = api
    task = await waiting_input(api, [])
    payload = {"expected_version": task.version, "input_refs": [f"file:{tmp_path / 'outside.txt'}"]}
    url = f"/tasks/{task.id}/resume"
    for token, status, code in [("foreign", 404, "not_found"), ("peer", 403, "forbidden"), ("owner", 400, "forbidden")]:
        response = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
        assert_error(response, status, code)
    assert await system.tasks.get("s1", task.id) == task


async def test_resume_validates_payload_and_total_ref_count(api):
    system, client = api
    old_refs = ["artifact:old"] * (MAX_INPUT_REFS - 1)
    task = await waiting_input(api, old_refs)
    url = f"/tasks/{task.id}/resume"
    for fields in [
        {"expected_version": 0}, {"actor_id": "u2"}, {"input_refs": ["x" * (MAX_INPUT_REF_CHARACTERS + 1)]},
        {"input_refs": ["duplicate"] * (MAX_INPUT_REFS + 1)}, {"input_refs": ["artifact:one", "artifact:two"]},
    ]:
        response = await client.post(url, json={"expected_version": task.version, **fields})
        assert_error(response, 400, "invalid_request")
    assert await system.tasks.get("s1", task.id) == task

    new_ref = "x" * MAX_INPUT_REF_CHARACTERS
    response = await client.post(url, json={"expected_version": task.version, "input_refs": [new_ref] * MAX_INPUT_REFS})
    assert response.status_code == 200, response.text
    assert (await system.tasks.get("s1", task.id)).input_refs == old_refs + [new_ref]


async def test_resume_without_refs_preserves_inputs(api):
    system, client = api
    task = await waiting_input(api, ["artifact:old"])
    response = await client.post(f"/tasks/{task.id}/resume", json={"expected_version": task.version})
    assert response.status_code == 200 and response.json()["status"] == "queued"
    assert (await system.tasks.get("s1", task.id)).input_refs == task.input_refs
    events = await system.repos.events.list("s1", task_id=task.id)
    assert events[-1].payload["added_input_refs"] == []


@pytest.mark.parametrize("with_checkpoint", [False, True], ids=["fresh", "checkpoint"])
async def test_resume_exposes_new_inputs_with_and_without_checkpoint(repos, tmp_path, monkeypatch, with_checkpoint):
    material = tmp_path / "inputs"
    material.mkdir()
    old_source, new_source = material / "notes-previous", material / "notes"
    old_ref, new_ref = f"file:{old_source}", f"file:{new_source}"
    old_source.write_text(f"Earlier source mentions another file:\n- {new_ref}", encoding="utf-8")
    new_source.write_text("revised result", encoding="utf-8")

    async def check_with_source(spec, ctx):
        if new_ref not in ctx.task.input_refs:
            return CheckResult(id=spec.id, status=VerificationStatus.INCONCLUSIVE, detail="awaiting source")
        return await artifact_readable(spec, ctx)

    monkeypatch.setitem(DEFAULT_CHECKERS, "artifact_readable", check_with_source)
    resumed_messages = []

    def read_new_input(context):
        resumed_messages.extend(context.messages)
        user_refs = [line[2:] for message in context.messages if message["role"] == "user"
                     for line in message["content"].splitlines() if line.startswith("- file:")]
        return tool_step(("files.read", {"path": user_refs[-1].removeprefix("file:")}))

    def write_draft(context):
        return tool_step(("artifacts.write", {"name": "draft", "content": context.messages[-1]["content"]}))

    first_steps = [tool_step(("files.read", {"path": str(old_source)}))] if with_checkpoint else []
    steps = [*first_steps, lambda ctx: result_step("awaiting source", {"draft": provisional.id}),
             read_new_input, write_draft, lambda ctx: result_step("completed", {"draft": written_id(ctx)})]
    async with open_api(repos, tmp_path, steps) as (system, client):
        provisional = await system.artifacts.put(ArtifactInput(scope_id="s1", media_type="text/plain", content=b"draft"))
        task_id = await submit(client, [old_ref], acceptance={"checks": [{"id": "artifact_readable"}]})
        url = f"/tasks/{task_id}"
        first = await system.worker(worker_id="first").run_once()
        assert first.committed and first.status is TaskStatus.WAITING
        waiting = await system.tasks.get("s1", task_id)
        assert waiting.wait_condition is WaitCondition.INPUT
        assert (waiting.checkpoint_ref is not None) == with_checkpoint
        response = await client.post(f"{url}/resume", json={"expected_version": waiting.version, "input_refs": [new_ref]})
        assert response.status_code == 200, response.text

        second = await system.worker(worker_id="second").run_once()
        assert second.committed and second.status is TaskStatus.SUCCEEDED, second
        user_lines = [line for message in resumed_messages if message["role"] == "user"
                      for line in message["content"].splitlines()]
        assert user_lines.count(f"- {old_ref}") == user_lines.count(f"- {new_ref}") == 1
        if with_checkpoint:
            assert resumed_messages[-1] == msg.user(f"Additional inputs provided on resume:\n- {new_ref}")
        result = await client.get(f"/artifacts/{second.result_ref}")
        assert result.status_code == 200 and "revised result" in result.text
        verifications = [e.payload["status"] for e in await repos.events.list("s1", task_id=task_id)
                         if e.type is EventType.VERIFICATION_RECORDED]
        assert verifications == ["inconclusive", "passed"]
