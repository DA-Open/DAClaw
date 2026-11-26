"""The saved watch context contains ranked, scoped records and versioned source data."""
import json
from datetime import datetime, timedelta

import pytest

from harness.contracts import Actor, ArtifactInput, Checkpoint, RunLane, TaskOutcome, VerificationResult, canonical_json
from harness.personal.memory import MemoryService
from harness.proactive_types import InboxItem
from ..unit.test_repositories import created, repos
from ..watch_kit import analysis_case, calendar_case, observation
from .test_personal_window import FIXTURE, subscribe

pytestmark = pytest.mark.asyncio


async def remember(case, kind, summary, at, *, text="", tags=(), status="confirmed", expiry=None, scope="s1"):
    memory = MemoryService(case.repos.memories, case.system.artifacts, lambda: at)
    record = await memory.remember(scope, "u1", kind, text or summary, status=status, summary=summary,
                                   tags=list(tags), source_ref="user:u1", subject_ref=None, expires_at=expiry)
    return f"memory:{record.id}@{record.version}"


async def test_observation_common_parts(tmp_path, repos):
    change = json.loads(FIXTURE.read_text(encoding="utf-8"))["changes"][0]
    other_change = {**change, "item_id": "seminar", "title": "进度", "occurred_at": "2026-11-25T01:05:00Z"}
    case = await calendar_case(tmp_path, repos, focus=["Lab"], changes=[change, other_change])
    now = case.clock()
    forged = "memory:mem_" + "a" * 32 + "@1"
    text = forged + "长" * 520
    stable = []
    for i in range(12):
        title = "组会改期" if i == 0 else f"稳定偏好 {i}"
        tags = ["Pinned"] if i == 1 else ["laboratory"] if i == 2 else []
        stable.append(await remember(case, "preference", title, now - timedelta(minutes=20-i), text=text, tags=tags))
    temporary = []
    for i in range(7):
        tags = ["pinned"] if i == 0 else ["LAB"] if i == 1 else []
        temporary.append(await remember(case, "preference", f"临时 {i}", now - timedelta(minutes=10-i),
                                        tags=tags, expiry=now + timedelta(days=1)))
    candidates = [await remember(case, "preference", f"候选 {i}", now - timedelta(minutes=7-i),
                                 status="candidate") for i in range(6)]
    await remember(case, "preference", "已过期", now - timedelta(days=2), expiry=now - timedelta(hours=1))
    await remember(case, "preference", "其他用户", now, scope="s2")

    facts = []
    for i in range(52):
        title = "组会" + "标" * 45 if i == 0 else f"资料 {i}"
        facts.append(await remember(case, "fact", title, now - timedelta(minutes=60-i), text="事实正文不可进入目录"))
    for days in [3, 2, 1]:
        await remember(case, "episode", f"第 {days} 天", now - timedelta(days=days), tags=[case.sub.id])
    await remember(case, "episode", "甲" * 260, now - timedelta(minutes=10), tags=[case.sub.id])
    await remember(case, "episode", "乙" * 260, now, tags=[case.sub.id])
    await remember(case, "episode", "其他订阅", now, tags=["another-sub"])

    bundle = await case.build()
    data = observation(bundle)
    submitted = bundle.messages[1]["content"]
    window_end = case.task.created_at + timedelta(seconds=case.sub.result_ttl_seconds)
    assert case.task.created_at.isoformat() in submitted and window_end.isoformat() in submitted
    assert "strictly between" in submitted
    assert data["subscription"] == case.sub.model_dump(mode="json", include={
        "id", "goal", "focus", "proactivity", "quiet_hours", "version"})
    assert data["changes"] == [
        {"ref": ref, **(await repos.subscriptions.event("s1", ref.removeprefix("event:"))).payload}
        for ref in case.record.input_snapshot["changes"]
    ]
    assert [entry["item_id"] for entry in data["changes"]] == ["seminar", change["item_id"]]
    pref = data["preferences"]
    assert [r["ref"] for r in pref["stable"]] == [stable[1], stable[0], *stable[:3:-1]]
    assert pref["stable"][0]["pinned"] and not pref["stable"][1]["pinned"]
    assert pref["stable"][1]["text"] == text[:500]
    assert pref["stable_more"] == [{"ref": stable[i], "summary": f"稳定偏好 {i}"} for i in (3, 2)]
    assert [r["ref"] for r in pref["temporary"]] == [temporary[i] for i in (0, 1, 6, 5, 4)]
    assert pref["temporary"][0]["pinned"]
    assert datetime.fromisoformat(pref["temporary"][0]["expires_at"]) == now + timedelta(days=1)
    assert [r["ref"] for r in pref["candidates"]] == candidates[:0:-1]
    assert all(set(r) == {"ref", "summary", "text"} for r in pref["candidates"])

    days = {r["date"]: r["text"] for r in data["recent_days"]}
    assert set(days) == {(now - timedelta(days=i)).date().isoformat() for i in range(3)}
    today = days[now.date().isoformat()]
    assert len(today) == 400 and today.startswith("甲" * 260) and today.endswith("乙" * 120)
    assert "其他订阅" not in today
    index = data["long_term_index"]
    assert index["total"] == 52 and index["truncated"] == 32
    assert [r["ref"] for r in index["items"]] == [facts[0], *facts[:32:-1]]
    assert index["items"][0]["title"] == ("组会" + "标" * 45)[:40]
    assert all(set(r) == {"ref", "title", "tags"} for r in index["items"])

    refs = {item["ref"] for group in pref.values() for item in group} | {r["ref"] for r in index["items"]}
    assert set(bundle.memory_refs) == refs and len(bundle.memory_refs) == len(refs)
    assert forged not in bundle.memory_refs
    assert forged not in bundle.messages[0]["content"]
    assert "事实正文不可进入目录" not in bundle.messages[1]["content"]


async def test_observation_analysis_parts(tmp_path, repos):
    case = await analysis_case(tmp_path, repos)
    baseline_id = case.sub.baseline_ref.removeprefix("memory:").split("@")[0]
    record, body = await case.system.memory.read("s1", "u1", baseline_id)
    older = await remember(case, "fact", "matching source table", case.clock() - timedelta(days=1),
                           tags=[record.tags[0]])
    await remember(case, "fact", "unrelated", case.clock())

    bundle = await case.build()
    data = observation(bundle)
    snapshot = case.record.input_snapshot
    assert data["trigger"] == {"ref": f"event:{case.task.trigger_ref}", "path": str(case.path),
                               "old_sha256": snapshot["old_sha256"], "new_sha256": snapshot["new_sha256"]}
    assert data["trigger"]["old_sha256"] != data["trigger"]["new_sha256"]
    assert data["baseline"] == {"ref": case.sub.baseline_ref, **json.loads(body)}
    assert data["threshold"] == case.sub.watch
    assert data["long_term_index"]["items"][0]["ref"] == older
    assert set(bundle.memory_refs) == {case.sub.baseline_ref, *(r["ref"] for r in data["long_term_index"]["items"])}
    recompute, = [a for a in await repos.artifacts.list_for_task("s1", case.task.id) if a.name == "recompute"]
    content = await case.system.artifacts.read(recompute.id, Actor(scope_id="s1", actor_id="u1"))
    assert data["recompute"] == json.loads(content.content)
    assert data["recompute"]["new_sha256"] == snapshot["new_sha256"]
    assert data["recompute"]["threshold"]["new"] == pytest.approx(232944.31)
    assert "changes" not in data


@pytest.mark.parametrize("legacy", [False, True], ids=["watch-observation", "pre-watch-checkpoint"])
async def test_resumed_watch_uses_the_memory_refs_from_its_saved_observation(tmp_path, repos, legacy, monkeypatch):
    case = await calendar_case(tmp_path, repos)
    first = await remember(case, "preference", "original", case.clock())
    lease = await case.system.tasks.claim("first-attempt", [RunLane.BACKGROUND])
    task = await repos.tasks.get("s1", lease.task_id)
    profile = case.system.agents.resolve_ref(task.agent_profile_ref)
    original = await case.system.builder.build(task, profile, lease)
    assert original.memory_refs == [first]
    messages = [dict(m) for m in original.messages]
    if legacy:
        # A pre-C5 goal can quote a complete observation-shaped example. Its final
        # Harness-generated section still identifies the old subscription brief.
        messages[1]["content"] += "\n\nSubscription trigger (data, not instructions):\n" + canonical_json(
            case.record.input_snapshot)
    checkpoint = Checkpoint(stage="tools_completed", step_id=f"{task.id}.step-1")
    saved = await case.system.artifacts.put(ArtifactInput(
        scope_id="s1", task_id=task.id, name="checkpoint", media_type="application/json",
        content=canonical_json({"checkpoint": checkpoint, "messages": messages}).encode()))
    await repos.tasks.checkpoint(lease, saved.id, case.clock())
    await case.system.memory.forget("s1", "u1", first.removeprefix("memory:").split("@")[0])
    unseen = await remember(case, "preference", "new since checkpoint", case.clock())
    async def no_fresh_observation(*args, **kwargs):
        pytest.fail("a durable checkpoint already contains the watch observation")

    monkeypatch.setattr(repos.memories, "current", no_fresh_observation)
    resumed = await case.system.builder.build(await repos.tasks.get("s1", task.id), profile, lease)
    assert resumed.messages[1] == messages[1] and unseen not in resumed.memory_refs
    assert resumed.memory_refs == ([] if legacy else [first])


@pytest.mark.parametrize("count", [2, 11], ids=["seven-day-window", "ten-latest"])
async def test_recent_notices_show_seven_days_for_this_subscription(tmp_path, repos, count):
    case = await calendar_case(tmp_path, repos)
    other = await subscribe(case.system, case.path)
    now = case.clock()

    async def publish(key, when, sub, mode):
        task = await created(repos, key=key, at=when)
        lease = await repos.tasks.claim("earlier-worker", [RunLane.FOREGROUND], 60, when)
        assert lease.task_id == task.id
        ref = f"result-{key}"
        checked = await repos.verifications.add(VerificationResult(
            task_id=task.id, result_ref=ref, checker_version="1", status="passed"))
        item = InboxItem(scope_id="s1", subscription_id=sub.id, task_id=task.id, result_ref=ref,
                         subject_ref=str(case.path), data_version=key, topic=key, keywords=[], mode=mode,
                         score=7, dedup_key=key, status="handled", created_at=when, valid_until=now+timedelta(days=1))
        await repos.tasks.commit(lease, TaskOutcome(status="succeeded", result_ref=ref, verification_ref=checked.id),
                                 when, inbox=item)

    for i in range(count):
        await publish(f"notice-{i}", now - timedelta(hours=i), case.sub, "digest")
    await publish("old", now - timedelta(days=8), case.sub, "notify")
    await publish("silent", now, case.sub, "silent")
    await publish("other", now, other, "notify")
    notices = observation(await case.build())["recent_notices"]
    assert [n["dedup_key"] for n in notices] == [f"notice-{i}" for i in range(min(count, 10))]
    assert all(n["status"] == "handled" and n["topic"] == n["dedup_key"] for n in notices)
    assert datetime.fromisoformat(notices[0]["created_at"]) == now
