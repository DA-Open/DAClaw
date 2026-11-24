"""Trusted HTTP lifecycle and read-only memory tools, through normal wiring."""
import json
from datetime import timedelta

import httpx
import pytest

from harness.api import TokenAuth, create_app, token_digest
from harness.app import build_system
from harness.config import HarnessConfig
from harness.contracts import Actor, ExecutionContext, RunLane, Subscription, Task, TaskOrigin, TaskRequest, ToolCall
from harness.runtime.models import ScriptedModelAdapter
from ..fakes import Clock, T0
from ..unit.test_repositories import repos  # noqa: F401
from ..unit.test_memory_service import remember

pytestmark = pytest.mark.asyncio
A = {"Authorization": "Bearer alice"}
B = {"Authorization": "Bearer bob"}
BODY = dict(kind="preference", text="组会改期请提醒", summary="组会提醒", tags=[])


@pytest.fixture
def system(repos, tmp_path):
    return build_system(HarnessConfig(artifact_root=tmp_path / "artifacts"), ScriptedModelAdapter(),
        repos=repos, clock=Clock(), policy_spec={"users": {"s1": {"proactive": {"paths": [str(tmp_path)]}}}})


@pytest.fixture
def client(system):
    auth = TokenAuth([dict(sha256=token_digest(token), scope_id=scope, actor_id=actor)
                      for token, scope, actor in [("alice", "s1", "u1"), ("bob", "s2", "u2")]])
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(system, auth)), base_url="http://harness")


async def create(client, **kw):
    response = await client.post("/memories", headers=A, json={**BODY, **kw})
    assert response.status_code == 201, response.text
    return response.json()


async def test_api_memory_flow(client):
    assert (await client.get("/memories")).status_code == 401
    old = await create(client, status="candidate")
    mid = old["id"]
    listing = (await client.get("/memories", headers=A)).json()
    assert listing["candidates"] == {"count": 1, "limit": 10}
    assert [r["id"] for r in listing["items"]] == [mid]
    read = (await client.get(f"/memories/{mid}", headers=A)).json()
    assert read["record"]["id"] == mid and read["text"] == BODY["text"]
    assert (await client.get(f"/memories/{mid}", headers=B)).status_code == 404
    for operation in ["confirm", "reject", "forget", "revise"]:
        assert (await client.post(f"/memories/{mid}/{operation}", headers=B,
            json=dict(text="x", summary="x", tags=[]) if operation == "revise" else None)).status_code == 404
    revised = await client.post(f"/memories/{mid}/revise", headers=A, json=dict(text="新的正文", summary="新标题", tags=[]))
    assert revised.status_code == 200
    new = revised.json()
    assert new["version"] == 2 and new["status"] == "candidate"
    confirmed = await client.post(f"/memories/{new['id']}/confirm", headers=A)
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "confirmed"
    cid = confirmed.json()["id"]
    assert (await client.post(f"/memories/{cid}/reject", headers=A)).status_code == 409
    assert (await client.post(f"/memories/{cid}/forget", headers=A)).json()["end_reason"] == "retracted"
    assert (await client.post(f"/memories/{cid}/forget", headers=A)).status_code == 409
    assert (await client.get(f"/memories/{cid}", headers=A)).json()["text"] == "新的正文"
    rejected = await create(client, status="candidate")
    assert (await client.post(f"/memories/{rejected['id']}/reject", headers=A)).json()["end_reason"] == "rejected"
    assert (await client.get("/memories", headers=A)).json() == {"items": [], "candidates": {"count": 0, "limit": 10}}
    history = (await client.get("/memories?include_ended=true&status=candidate&kind=preference", headers=A)).json()
    assert len(history["items"]) == 3
    for invalid in [dict(kind="analysis"), dict(kind="episode"), dict(text="x" * 4001), dict(summary=""),
                    dict(summary="x" * 301), dict(tags=[str(i) for i in range(17)]), dict(scope_id="s2")]:
        assert (await client.post("/memories", headers=A, json={**BODY, **invalid})).status_code == 400
    for i in range(5):
        assert "pinned" in (await create(client, pinned=True, tags=["pinned"] if i == 0 else []))["tags"]
    assert (await client.post("/memories", headers=A, json={**BODY, "pinned": True})).status_code == 400
    assert (await client.post("/memories", headers=A, json={**BODY, "tags": ["pinned"]})).status_code == 400
    candidate = await create(client, status="candidate")
    listing = (await client.get("/memories?status=confirmed", headers=A)).json()
    assert len(listing["items"]) == 5 and listing["candidates"]["count"] == 1
    assert (await client.post(f"/memories/{candidate['id']}/revise", headers=A,
        json=dict(text="x", summary="x", tags=["pinned"]))).status_code == 400
    assert (await client.post(f"/memories/{cid}/revise", headers=A,
        json=dict(text="x", summary="x", tags=["pinned"]))).status_code == 409


async def test_revise_expires_at(client, system):
    deadline = T0 + timedelta(days=1)
    old = await create(client, expires_at=deadline.isoformat())
    assert "temporary" in old["tags"]
    revision = dict(text="new", summary="new", tags=[])
    new = (await client.post(f"/memories/{old['id']}/revise", headers=A, json=revision)).json()
    assert new["expires_at"] == old["expires_at"]
    stable = (await client.post(f"/memories/{new['id']}/revise", headers=A, json={**revision, "expires_at": None})).json()
    assert stable["expires_at"] is None and "temporary" not in stable["tags"]
    for bad in [T0.isoformat(), T0.replace(tzinfo=None).isoformat()]:
        assert (await client.post(f"/memories/{stable['id']}/revise", headers=A,
            json={**revision, "expires_at": bad})).status_code == 400
        assert (await client.post("/memories", headers=A, json={**BODY, "expires_at": bad})).status_code == 400
    expired = await create(client, expires_at=deadline.isoformat())
    system.memory.clock.advance(86400)
    for operation in ["confirm", "reject", "forget", "revise"]:
        assert (await client.post(f"/memories/{expired['id']}/{operation}", headers=A,
            json=revision if operation == "revise" else None)).status_code == 409


async def execution(system, capability):
    actor = Actor(scope_id="s1", actor_id="u1")
    resource = str(system.config.artifact_root.parent)
    sub = await system.repos.subscriptions.create(Subscription(scope_id="s1", owner_id="u1", goal="read memories",
        capability_ref=capability, event_filter={"source": "test"}, resources=[resource], subject_ref=resource))
    task = Task.new(TaskRequest(request_key=capability, goal="read memories", capability_ref=capability,
                               subject_ref=resource), actor,
        TaskOrigin(agent_role="proactive", agent_profile_ref="proactive@1", run_lane="background", trigger_type="event",
                   subscription_id=sub.id), now=T0)
    await system.repos.tasks.create(task)
    lease = await system.repos.tasks.claim("worker", [RunLane.BACKGROUND], 86400, T0)
    return ExecutionContext(lease=lease, actor=actor, agent_role="proactive", run_lane="background")


async def test_memory_tools_for_proactive(system):
    # No production pack is changed: this test-only capability uses the real policy intersection.
    system.policy.capabilities["memory_test@1"] = frozenset({"memory.search", "memory.read"})
    ctx = await execution(system, "memory_test@1")
    old = await remember(system.memory, text="字" * 4100)
    foreign = await remember(system.memory, scope_id="s2")
    found = await system.gateway.invoke(ctx, ToolCall(tool="memory.search", args={"query": "组会"}, logical_key="search"))
    assert found.status == "succeeded"
    item, = json.loads(found.summary)
    ref = f"memory:{old.id}@1"
    assert item == dict(ref=ref, kind="preference", status="confirmed", summary=old.summary, tags=[], updated_at=old.updated_at.isoformat())
    result = await system.gateway.invoke(ctx, ToolCall(tool="memory.read", args={"ref": ref}, logical_key="read"))
    assert result.status == "succeeded" and len(json.loads(result.summary)["text"]) == 4000
    newer = await system.memory.confirm("s1", "u1", old.id)
    for i, bad in enumerate([ref, f"memory:{newer.id}@1", f"memory:{foreign.id}@1", "memory:mem_" + "0" * 32 + "@1"]):
        failed = await system.gateway.invoke(ctx, ToolCall(tool="memory.read", args={"ref": bad}, logical_key=f"bad{i}"))
        assert failed.error.code == "not_found"
    temporary = await remember(system.memory, expires_at=T0 + timedelta(seconds=1))
    system.memory.clock.advance(1)
    failed = await system.gateway.invoke(ctx, ToolCall(tool="memory.read", args={"ref": f"memory:{temporary.id}@1"}, logical_key="expired"))
    assert failed.error.code == "not_found"
    for name, args in [("memory.read", {"ref": "bad"}), ("memory.search", {"query": " "}),
                       ("memory.search", {"query": "x", "limit": 11}), ("memory.search", {"query": "x", "kinds": ["result"]})]:
        outcome = await system.gateway.invoke(ctx, ToolCall(tool=name, args=args, logical_key=f"invalid{args}"))
        assert outcome.error.code == "invalid_request"
    for spec in system.gateway.specs:
        if spec.name.startswith("memory."):
            assert spec.read_only and not spec.side_effects and spec.idempotent


async def test_data_card_task_cannot_call_memory_tools(system):
    ctx = await execution(system, "data_card@1")
    outcome = await system.gateway.invoke(ctx, ToolCall(tool="memory.search", args={"query": "组会"}, logical_key="forbidden"))
    assert outcome.status == "denied"
    assert "memory.search" not in system.capabilities.resolve("data_card@1").tools
