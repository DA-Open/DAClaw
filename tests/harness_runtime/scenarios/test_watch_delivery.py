"""Verified watch decisions, durable inbox records and post-commit memory on both stores."""
import json
from datetime import timedelta
from pathlib import Path

import pytest

from harness.contracts import (
    Event, EventType, ExecutionContext, MemoryKind, NotFound, RunLane, TaskOrigin,
    TaskOutcome, TaskRequest, ToolCall,
)
from ..fakes import ACTOR
from ..unit.test_repositories import repos
from ..watch_kit import analysis_case, calendar_case
from .test_personal_window import FIXTURE

pytestmark = pytest.mark.asyncio


async def main_task(s, goal):
    return await s.tasks.create(
        TaskRequest(request_key=goal, goal=goal, capability_ref="data_analysis@2"), ACTOR,
        TaskOrigin(agent_role="main", agent_profile_ref="main@1", run_lane="foreground", trigger_type="user"),
    )


async def successful_outcome(c, task):
    verification, = await c.repos.verifications.list_for_task(task.id)
    return TaskOutcome(status="succeeded", result_ref=task.result_ref, verification_ref=verification.id)


async def test_calendar_notify_reaches_inbox(repos, tmp_path):
    c = await calendar_case(tmp_path, repos, focus=["日程更新"])
    task = await c.run()
    assert task.status == "succeeded", task.error
    item, = await c.inbox()
    assert item["task_id"] == task.id and item["result_ref"] == task.result_ref
    assert (item["mode"], item["score"], item["topic"]) == ("notify", 7, c.decision["draft"]["title"])
    assert set(item["keywords"]) == {"日程更新", "组会"}
    assert item["subject_ref"] == c.sub.subject_ref
    snapshot = c.record.input_snapshot
    assert item["dedup_key"] == f"{c.sub.id}:" + "+".join(sorted(snapshot["changes"]))
    assert item["data_version"] == snapshot["until"]
    record = await repos.delivery.get("s1", item["id"])
    assert record.valid_until.isoformat().replace("+00:00", "Z") == c.decision["valid_until"].replace("+00:00", "Z")


async def test_analysis_notify_reaches_inbox(repos, tmp_path):
    c = await analysis_case(tmp_path, repos)
    task = await c.run()
    assert task.status == "succeeded", task.error
    assert (await c.checks())["recompute_replay"].status == "passed"
    item, = await c.inbox()
    assert (item["mode"], item["score"], item["topic"]) == ("notify", 7, c.decision["draft"]["title"])
    assert item["data_version"] == c.record.input_snapshot["new_sha256"]
    assert item["dedup_key"] == f"{c.sub.id}:{c.sub.baseline_ref}:{item['data_version']}"
    baseline_id = c.sub.baseline_ref.removeprefix("memory:").split("@")[0]
    baseline = await repos.memories.get("s1", baseline_id)
    assert set(item["keywords"]) == set(c.sub.focus) | set(baseline.tags)


async def test_recompute_error_notifies(repos, tmp_path):
    c = await analysis_case(tmp_path, repos, broken=True)
    task = await c.run()
    assert task.status == "succeeded", task.error
    assert (await c.checks())["recompute_replay"].status == "passed"
    item, = await c.inbox()
    assert "监测复算失败" in item["topic"] and c.decision["claims"] == []
    assert (item["mode"], item["score"]) == ("notify", 8)
    version = c.record.input_snapshot["new_sha256"]
    assert item["data_version"] == version
    assert item["dedup_key"] == f"{c.sub.id}:{c.sub.baseline_ref}:{version}:error"


async def test_silent_is_recorded_not_listed(repos, tmp_path):
    c = await calendar_case(tmp_path, repos)
    task = await c.run(silent=True)
    assert task.status == "succeeded", task.error
    assert await c.inbox() == []
    item, = await repos.delivery.list("s1")
    assert (item.mode, item.score, item.topic, item.keywords) == ("silent", 0, c.decision["observation_digest"][:80], [])
    assert (item.scope_id, item.subscription_id, item.task_id, item.result_ref) == ("s1", c.sub.id, task.id, task.result_ref)
    assert (item.subject_ref, item.data_version) == (c.sub.subject_ref, c.record.input_snapshot["until"])
    assert item.dedup_key == f"{c.sub.id}:" + "+".join(sorted(c.record.input_snapshot["changes"]))
    assert item.valid_until == item.created_at + timedelta(seconds=c.sub.result_ttl_seconds)


async def test_silent_not_injected_into_main(repos, tmp_path):
    c = await calendar_case(tmp_path, repos, focus=["排期"])
    assert (await c.run(silent=True)).status == "succeeded"
    main = await main_task(c.system, "帮我看看排期")
    assert await c.system.delivery.materials(main) == []


async def test_quiet_hours_force_silent(repos, tmp_path):
    c = await calendar_case(tmp_path, repos, quiet_hours=(0, 6))
    assert (await c.run()).status == "succeeded"
    assert c.decision["decision"] == "propose_notify"
    item, = await repos.delivery.list("s1")
    assert item.mode == "silent"
    assert await c.inbox() == []


async def test_permissions_and_revocation_at_delivery(repos, tmp_path):
    c = await calendar_case(tmp_path, repos)
    task = await c.run()
    outcome = await successful_outcome(c, task)
    for role in ("main", "proactive"):
        grant = c.system.policy.users["s1"].pop(role)
        item = await c.system.delivery.prepare(task, outcome)
        assert item.mode == "silent"
        assert await c.inbox() == []
        c.system.policy.users["s1"][role] = grant
    await c.system.proactive.revoke("s1", c.sub.id)
    assert (await c.system.delivery.prepare(task, outcome)).mode == "silent"
    stored, = await repos.delivery.list("s1")
    assert stored.status == "cancelled"
    assert await c.system.delivery.materials(await main_task(c.system, "组会")) == []


async def test_newer_calendar_version_hides_item(repos, tmp_path, monkeypatch):
    first, = json.loads(FIXTURE.read_text(encoding="utf-8"))["changes"]
    second = dict(first, item_id="committee", title="委员会")
    c = await calendar_case(tmp_path, repos, changes=[first, second])

    def cite_first(decision, observation):
        decision["evidence_refs"] = [next(r["ref"] for r in observation["changes"] if r["item_id"] == first["item_id"])]
        decision["valid_until"] = (c.task.created_at + timedelta(seconds=c.sub.result_ttl_seconds - 1)).isoformat()

    assert (await c.run(edit=cite_first)).status == "succeeded"
    item, = await c.inbox()

    async def ingest_revision(change):
        revised = dict(change, version=change["version"] + 1, occurred_at=(c.clock() + timedelta(minutes=10)).isoformat())
        c.path.write_text(json.dumps({"calendar_id": "lab", "changes": [revised]}), encoding="utf-8")
        c.clock.advance(3600)
        await c.system.scheduler.tick()

    async def forbid_artifact_read(*args, **kwargs):
        pytest.fail("calendar freshness must use the verified references already in the database")

    monkeypatch.setattr(c.system.artifacts, "read", forbid_artifact_read)
    await ingest_revision(second)
    assert [row["id"] for row in await c.inbox()] == [item["id"]]
    await ingest_revision(first)
    assert await c.inbox() == []


async def test_newer_source_hides_item(repos, tmp_path, monkeypatch):
    c = await analysis_case(tmp_path, repos)
    assert (await c.run()).status == "succeeded"
    item, = await c.inbox()
    subject = c.sub.subject_ref
    original_open = Path.open

    def guard_source(path, *args, **kwargs):
        if path == c.path:
            pytest.fail("inbox freshness must not read the watched data file")
        return original_open(path, *args, **kwargs)

    async def forbid_artifact_read(*args, **kwargs):
        pytest.fail("inbox freshness must not read artifact files")

    monkeypatch.setattr(Path, "open", guard_source)
    monkeypatch.setattr(c.system.artifacts, "read", forbid_artifact_read)
    assert [row["id"] for row in await c.inbox()] == [item["id"]]

    async def external(source, payload, subject_ref=subject):
        return await repos.events.append(Event(scope_id="s1", type=EventType.EXTERNAL, source=source,
                                               subject_ref=subject_ref, payload=payload))

    # Put the subsequent change beyond the first repository page.
    for _ in range(101):
        await external("catalogue", {"kind": "data_updated"})
    await external("watch", {"kind": "source_changed", "sha256": "c" * 64}, subject + ".other")
    assert [row["id"] for row in await c.inbox()] == [item["id"]]
    await external("watch", {"kind": "source_changed", "sha256": "b" * 64, "path": subject, "size_bytes": 1})
    assert await c.inbox() == []
    await external("catalogue", {"kind": "data_updated"})
    assert await c.inbox() == []


async def test_episode_written_once(repos, tmp_path):
    c = await calendar_case(tmp_path, repos)
    task = await c.run(silent=True)
    assert task.status == "succeeded", task.error
    episode, = await repos.memories.list("s1", now=c.clock(), kinds=[MemoryKind.EPISODE])
    assert episode.status == "confirmed" and episode.summary == c.decision["observation_digest"]
    assert episode.tags == ["watch", c.sub.id] and episode.source_ref == f"task:{task.id}"
    assert episode.expires_at == task.created_at + timedelta(days=3)
    _, body = await c.system.memory.read("s1", "u1", episode.id)
    assert json.loads(body) == {k: c.decision[k] for k in ("decision", "rationale", "evidence_refs")}
    await c.system.tasks._after_commit_hooks(task, await successful_outcome(c, task))
    again, = await repos.memories.list("s1", now=c.clock(), kinds=[MemoryKind.EPISODE])
    assert again.id == episode.id


async def test_revocation_racing_commit_leaves_no_episode(repos, tmp_path, monkeypatch):
    c = await calendar_case(tmp_path, repos)
    prepare = c.system.delivery.prepare

    async def revoke_after_verify(task, outcome):
        assert outcome.status == "succeeded"
        item = await prepare(task, outcome)
        await c.system.proactive.revoke("s1", c.sub.id)
        return item

    monkeypatch.setattr(c.system.delivery, "prepare", revoke_after_verify)
    assert (await c.run()).status == "cancelled"
    assert await repos.memories.list("s1", now=c.clock(), kinds=[MemoryKind.EPISODE]) == []
    assert await repos.delivery.list("s1") == []


async def test_proposals_become_candidates(repos, tmp_path):
    c = await calendar_case(tmp_path, repos)
    existing = await c.system.memory.remember("s1", "u1", "preference", "已经确认的正文", status="confirmed",
        summary="已经确认的偏好", tags=[], source_ref="user:u1", subject_ref=None, expires_at=None)

    def add_proposals(decision, observation):
        assert any(entry["ref"] == f"memory:{existing.id}@1" for entry in observation["preferences"]["stable"])
        row = {"kind": "preference", "evidence_refs": decision["evidence_refs"]}
        decision["memory_proposals"] = [
            dict(row, summary=existing.summary, text="已经确认的正文"),
            dict(row, summary="提前告知改期", text="希望在会议改期时提前一天知道。"),
            dict(row, summary="提醒汇报顺序", text="希望在汇报顺序变化时得到提醒。"),
        ]

    task = await c.run(edit=add_proposals)
    assert task.status == "succeeded", task.error
    candidates = await repos.memories.list("s1", now=c.clock(), statuses=["candidate"])
    assert sorted((r.source_ref, r.summary) for r in candidates) == [
        (f"task:{task.id}:proposal:1", "提前告知改期"), (f"task:{task.id}:proposal:2", "提醒汇报顺序")]
    assert all(r.kind == "preference" for r in candidates)
    await c.system.tasks._after_commit_hooks(task, await successful_outcome(c, task))
    replay = await repos.memories.list("s1", now=c.clock(), statuses=["candidate"])
    assert {r.id for r in replay} == {r.id for r in candidates}
    assert len(await repos.memories.list("s1", now=c.clock(), statuses=["confirmed"], kinds=["preference"])) == 1


async def test_main_gets_pointer_only(repos, tmp_path):
    c = await calendar_case(tmp_path, repos, focus=["排期"])

    def distinct_body(decision, observation):
        decision["draft"]["body"] = "会议更新的完整正文只在主动读取时出现。"

    assert (await c.run(edit=distinct_body)).status == "succeeded"
    item, = await repos.delivery.list("s1")
    main = await main_task(c.system, "帮我看看最近的排期")
    unrelated = main.model_copy(update={"goal": f"解释 {c.path.stem}"})
    assert await c.system.delivery.materials(unrelated) == []
    lease = await c.system.tasks.claim("watch-reader", [RunLane.FOREGROUND])
    assert lease.task_id == main.id
    ctx = ExecutionContext(lease=lease, actor=ACTOR, agent_role=main.agent_role, run_lane=main.run_lane)
    bundle = await c.system.builder.build(main, c.system.agents.resolve_ref(main.agent_profile_ref), lease)
    pointer, = bundle.injected_materials
    assert pointer.source_task_id == item.task_id and pointer.ref_ids == [item.id]
    assert c.decision["draft"]["body"] not in json.dumps(bundle.messages, ensure_ascii=False)
    assert c.decision["rationale"] not in json.dumps(bundle.messages, ensure_ascii=False)
    call = ToolCall(tool="background_results.read", args={"id": item.id}, logical_key="read-watch")
    outcome = await c.system.gateway.invoke(ctx, call)
    assert outcome.status == "succeeded", outcome
    response = json.loads(outcome.summary)
    assert response == {"notice": "read", "valid_until": item.valid_until.isoformat(), "decision": c.decision}
    c.clock.advance(c.sub.result_ttl_seconds + 1)
    with pytest.raises(NotFound):
        await c.system.delivery.read("s1", item.id)
    old = await c.system.delivery.read("s1", item.id, explicit=True)
    assert old["decision"] == c.decision and old["notice"] == "requested"
