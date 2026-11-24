"""C2 admission without model execution, on both repository backends."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from harness.api import TokenAuth, create_app
from harness.app import build_system
from harness.config import HarnessConfig
from harness.contracts import Actor, AdmissionDecision, Event, EventType, Subscription, Usage
from harness.runtime.models import ScriptedModelAdapter
from ..fakes import Clock
from ..unit.test_repositories import repos  # shared backend fixture

pytestmark = pytest.mark.asyncio
T0 = datetime(2026, 11, 25, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parents[1] / "fixtures/personal/calendar.json"


def calendar(tmp_path, *, changes=None):
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if changes is not None:
        document["changes"] = changes
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def system(tmp_path, repos, clock, *, limit=2):
    return build_system(HarnessConfig(artifact_root=tmp_path / "artifacts", background_scope_limit=limit),
        ScriptedModelAdapter(), repos=repos, clock=clock,
        policy_spec={"users": {"s1": {role: {"paths": [str(tmp_path)]} for role in ("main", "proactive")}}})


async def subscribe(s, path, **overrides):
    values = dict(scope_id="s1", owner_id="u1", goal="关注组会日程变化", subject_ref=str(path),
                  resources=[str(path)], capability_ref="personal_watch@1", schedule="0 * * * *",
                  created_at=T0, updated_at=T0)
    return await s.proactive.create_subscription(Subscription(**(values | overrides)))


async def setup_watch(tmp_path, repos):
    clock = Clock(T0)
    path = calendar(tmp_path)
    s = system(tmp_path, repos, clock)
    sub = await subscribe(s, path)
    clock.advance(7205)
    return s, sub, path, clock


async def test_personal_subscription_validation(repos, tmp_path):
    path = calendar(tmp_path)
    s = system(tmp_path, repos, Clock(T0))
    for overrides in (
        {"schedule": None}, {"schedule": "", "event_filter": {"source": "calendar"}},
        {"event_filter": {"source": "calendar"}}, {"resources": [str(path), str(tmp_path)]},
        {"subject_ref": str(tmp_path / "calendar.txt"), "resources": [str(tmp_path / "calendar.txt")]},
        {"baseline_ref": "memory:other@1"}, {"watch": {}}, {"schedule": "bad cron"},
        {"grant_ref": "reserved"}, {"expires_at": datetime(2026, 11, 26)},
    ):
        with pytest.raises(ValueError):
            await subscribe(s, path, **overrides)
    auth = TokenAuth([{"sha256": hashlib.sha256(b"test").hexdigest(), "scope_id": "s1", "actor_id": "u1"}])
    body = dict(goal="关注日程", subject_ref=str(path), resources=[str(path)],
                capability_ref="personal_watch@1", schedule="0 * * * *")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(s, auth)), base_url="http://test",
                                headers={"Authorization": "Bearer test"}) as client:
        created = await client.post("/subscriptions", json=body)
        assert created.status_code == 201, created.text
        for role in ("proactive", "main"):
            grant = s.policy.users["s1"].pop(role)
            denied = await client.post("/subscriptions", json=body)
            assert denied.status_code == 403, denied.text
            assert role in denied.text
            s.policy.users["s1"][role] = grant
        response = await client.post("/events", json={"source": "scheduler", "source_event_id": "forged",
            "subject_ref": str(path), "data": {"kind": "personal_window", "subscription_id": created.json()["id"],
            "since": T0.isoformat(), "until": (T0 + timedelta(hours=1)).isoformat()}})
        assert response.status_code == 400


async def test_first_tick_admits_without_prior_event(repos, tmp_path):
    s, sub, path, clock = await setup_watch(tmp_path, repos)
    assert await s.scheduler.tick() == 0
    raw, = await repos.subscriptions.changes("s1", str(path), T0, clock(), source="mock_calendar")
    scheduler, = await repos.subscriptions.changes("s1", str(path), T0, clock(), source="scheduler")
    assert raw.processed_at == clock() and raw.source_event_id == "group-meeting-w48@2"
    assert raw.payload["body"] == "本周组会改到周四上午十点，汇报顺序不变。"
    assert raw.occurred_at == T0 + timedelta(hours=1, minutes=10) and raw.received_at == clock()
    assert scheduler.source_event_id == f"{sub.id}:{(T0 + timedelta(hours=2)).isoformat()}"
    record, = await repos.subscriptions.decisions("s1", sub.id)
    assert record.decision == "admit" and record.policy_version == "personal-admission@1"
    assert record.event_id == scheduler.id
    snapshot = record.input_snapshot
    assert snapshot["kind"] == "personal_window"
    assert datetime.fromisoformat(snapshot["since"]) == T0
    assert datetime.fromisoformat(snapshot["until"]) == T0 + timedelta(hours=2)
    assert snapshot["changes"] == [f"event:{raw.id}"]
    assert snapshot["subscription_version"] == sub.version
    assert snapshot["daily_usage"] == Usage().model_dump()
    assert snapshot["daily_budget"] == sub.daily_budget.model_dump()
    assert snapshot["scope_limit"] == 2 and snapshot["resources_allowed"] is True
    task = await s.tasks.get("s1", record.task_id)
    assert task.capability_ref == "personal_watch@1" and task.agent_role == "proactive"
    assert task.run_lane == "background" and task.trigger_type == "schedule"
    assert task.input_refs == [str(path)] and task.acceptance.required_artifacts == ["decision"]
    assert [c.id for c in task.acceptance.checks] == ["artifact_readable", "decision_schema", "decision_evidence"]
    pack = s.capabilities.resolve("personal_watch@1")
    assert pack.tools == frozenset({"memory.search", "memory.read", "artifacts.write"})
    assert pack.result_artifact == "decision" and pack.default_mode == "draft_only"


async def test_raw_calendar_event_not_readmitted(repos, tmp_path):
    s, sub, path, clock = await setup_watch(tmp_path, repos)
    await s.scheduler.tick()
    first = await repos.subscriptions.decisions("s1", sub.id)
    await system(tmp_path, repos, clock).scheduler.tick()
    assert await repos.subscriptions.pending_events() == []
    assert await repos.subscriptions.decisions("s1", sub.id) == first
    assert len(await repos.subscriptions.changes("s1", str(path), T0, clock(), source="mock_calendar")) == 1


async def test_empty_window_skips_without_task(repos, tmp_path):
    clock = Clock(T0)
    path = calendar(tmp_path, changes=[])
    s = system(tmp_path, repos, clock)
    sub = await subscribe(s, path)
    clock.advance(7205)
    await s.scheduler.tick()
    record, = await repos.subscriptions.decisions("s1", sub.id)
    assert (record.decision, record.reason, record.task_id) == ("skip", "no_changes", None)
    assert record.input_snapshot["changes"] == []
    assert await s.tasks.claim("background", ["background"]) is None


async def test_missed_ticks_leave_no_gap(repos, tmp_path):
    s, sub, path, clock = await setup_watch(tmp_path, repos)
    await s.scheduler.tick()
    first, = await repos.subscriptions.decisions("s1", sub.id)
    clock.advance(10800)
    await system(tmp_path, repos, clock).scheduler.tick()
    records = await repos.subscriptions.decisions("s1", sub.id)
    second, = [r for r in records if r.event_id != first.event_id]
    assert second.input_snapshot["since"] == first.input_snapshot["until"]
    assert datetime.fromisoformat(second.input_snapshot["until"]) == T0 + timedelta(hours=5)
    assert second.reason == "no_changes"  # Empty wins even while the first task is busy.


async def test_reingest_dedup_and_new_version(repos, tmp_path):
    s, sub, path, clock = await setup_watch(tmp_path, repos)
    change = json.loads(path.read_text(encoding="utf-8"))["changes"][0]
    calendar(tmp_path, changes=[change, change])
    await s.scheduler.tick()
    first, = await repos.subscriptions.decisions("s1", sub.id)
    await s.tasks.request_cancel("s1", first.task_id, "done")
    calendar(tmp_path, changes=[change, change, {**change, "version": 3, "occurred_at": "2026-11-25T02:30:00Z"}])
    clock.advance(3600)
    await s.scheduler.tick()
    raw = await repos.subscriptions.changes("s1", str(path), T0, clock(), source="mock_calendar")
    assert [e.source_event_id for e in raw] == ["group-meeting-w48@2", "group-meeting-w48@3"]
    records = await repos.subscriptions.decisions("s1", sub.id)
    second, = [r for r in records if r.event_id != first.event_id]
    assert second.decision == "admit" and second.input_snapshot["changes"] == [f"event:{raw[1].id}"]


async def test_revoked_path_does_not_read_source(repos, tmp_path, monkeypatch):
    s, sub, path, clock = await setup_watch(tmp_path, repos)
    s.policy.users["s1"].pop("proactive")
    def forbidden_read(*args):
        raise AssertionError("revoked source was read")
    monkeypatch.setattr(s.scheduler.calendar, "changes", forbidden_read)
    await s.scheduler.tick()
    record, = await repos.subscriptions.decisions("s1", sub.id)
    assert (record.decision, record.reason) == ("skip", "resource_permission_revoked")
    assert record.input_snapshot["resources_allowed"] is False
    assert await repos.subscriptions.changes("s1", str(path), T0, clock(), source="mock_calendar") == []


async def test_busy_subscription_defers_then_runs(repos, tmp_path):
    s, sub, path, clock = await setup_watch(tmp_path, repos)
    await s.scheduler.tick()
    first, = await repos.subscriptions.decisions("s1", sub.id)
    change = json.loads(path.read_text(encoding="utf-8"))["changes"][0]
    calendar(tmp_path, changes=[change, {**change, "version": 3, "occurred_at": "2026-11-25T02:30:00Z"}])
    clock.advance(3600)
    await s.scheduler.tick()
    pending, = await repos.subscriptions.pending_events()
    records = await repos.subscriptions.decisions("s1", sub.id)
    deferred, = [r for r in records if r.event_id == pending.id]
    assert (deferred.decision, deferred.reason, deferred.task_id) == ("defer", "subscription_busy", None)
    await s.tasks.request_cancel("s1", first.task_id, "done")
    await s.scheduler.tick()
    records = await repos.subscriptions.decisions("s1", sub.id)
    admitted, = [r for r in records if r.event_id == pending.id]
    assert len(records) == 2 and admitted.decision == "admit" and admitted.task_id != first.task_id
    assert admitted.input_snapshot["changes"] == deferred.input_snapshot["changes"]
    assert any(r["reason"] == "subscription_busy" for r in admitted.evaluations)
    assert await repos.subscriptions.pending_events() == []


async def test_calendar_error_does_not_block_other_subscriptions(repos, tmp_path, caplog):
    clock = Clock(T0)
    path = calendar(tmp_path)
    s = system(tmp_path, repos, clock)
    missing = await subscribe(s, tmp_path / "missing.json")
    good = await subscribe(s, path)
    clock.advance(7205)
    with caplog.at_level("WARNING"):
        await s.scheduler.tick()
    assert str(tmp_path / "missing.json") in caplog.text
    assert (await repos.subscriptions.decisions("s1", good.id))[0].decision == "admit"
    assert await repos.subscriptions.decisions("s1", missing.id) == []


async def test_directory_calendar_is_skipped_and_next_subscription_runs(repos, tmp_path, caplog):
    clock = Clock(T0)
    s = system(tmp_path, repos, clock)
    directory = tmp_path / "directory.json"
    directory.mkdir()
    bad = await subscribe(s, directory)
    good = await subscribe(s, calendar(tmp_path))
    clock.advance(7205)

    with caplog.at_level("WARNING", logger="harness.tasks.scheduler"):
        await s.scheduler.tick()

    assert any(bad.id in message and str(directory) in message for message in caplog.messages)
    assert await repos.subscriptions.decisions("s1", bad.id) == []
    record, = await repos.subscriptions.decisions("s1", good.id)
    assert record.decision == "admit"
    assert (await s.tasks.get("s1", record.task_id)).subscription_id == good.id


async def test_personal_tick_before_first_due(repos, tmp_path):
    clock = Clock(T0 + timedelta(seconds=5))
    path = calendar(tmp_path)
    s = system(tmp_path, repos, clock)
    sub = await subscribe(s, path, created_at=clock())
    await s.scheduler.tick()
    assert await repos.subscriptions.decisions("s1", sub.id) == []
    assert await repos.subscriptions.pending_events() == []


@pytest.mark.parametrize("condition,reason", [("inactive", "subscription_inactive"),
    ("budget", "daily_budget"), ("capacity", "scope_capacity")])
async def test_personal_admission_priority(repos, tmp_path, condition, reason):
    s, sub, path, clock = await setup_watch(tmp_path, repos)
    if condition == "inactive":
        await s.proactive.revoke("s1", sub.id)
        s.policy.users["s1"].pop("proactive")
        event = Event(scope_id="s1", type=EventType.EXTERNAL, source="scheduler", source_event_id="inactive",
            subject_ref=str(path), payload={"kind": "personal_window", "subscription_id": sub.id,
            "since": T0.isoformat(), "until": (T0 + timedelta(hours=2)).isoformat()})
        await s.proactive.on_event(event)
    elif condition == "budget":
        await s.tasks.create(s.proactive._request(sub, "occupied"), Actor(scope_id="s1", actor_id="u1"),
                             s.proactive._origin(sub, "occupied"))
        sub = await subscribe(s, path, daily_budget=Usage())
        s.proactive.scope_limit = 1
        await s.scheduler.tick()
    else:
        await s.tasks.create(s.proactive._request(sub, "occupied"), Actor(scope_id="s1", actor_id="u1"),
                             s.proactive._origin(sub, "occupied"))
        sub = await subscribe(s, path)
        s.proactive.scope_limit = 1
        await s.scheduler.tick()
    record, = await repos.subscriptions.decisions("s1", sub.id)
    assert record.reason == reason and record.task_id is None
    assert record.decision == ("skip" if condition == "inactive" else "defer")


async def test_window_cursor_ignores_other_decisions(repos, tmp_path):
    s, sub, path, clock = await setup_watch(tmp_path, repos)
    event = await repos.events.append(Event(scope_id="s1", type=EventType.EXTERNAL, source="legacy",
        subject_ref=str(path), payload={"kind": "data_updated"}))
    def evaluate(current, tasks, records, usage):
        return AdmissionDecision(subscription_id=current.id, event_id=event.id, decision="skip", reason="legacy",
            policy_version="proactive-admission@1", input_snapshot={"kind": "data_updated"}), None
    await repos.subscriptions.admit(event, sub.id, evaluate, clock())
    await repos.subscriptions.mark_processed("s1", event.id, clock())
    await s.scheduler.tick()
    records = await repos.subscriptions.decisions("s1", sub.id)
    current, = [r for r in records if r.event_id != event.id]
    assert current.decision == "admit" and datetime.fromisoformat(current.input_snapshot["since"]) == T0


async def test_changes_filter_scope_source_type_subject_and_order(repos):
    async def append(key, hour, **overrides):
        values = dict(scope_id="s1", type=EventType.EXTERNAL, source="mock_calendar", source_event_id=key,
                      subject_ref="calendar.json", occurred_at=T0 + timedelta(hours=hour))
        return await repos.events.append(Event(**(values | overrides)))
    last = await append("last", 2)
    first = await append("first", 1)
    tie = await append("tie", 1)
    await append("lower", 0)
    await append("upper", 3)
    await append("scope", 1, scope_id="s2")
    await append("source", 1, source="scheduler")
    await append("subject", 1, subject_ref="other.json")
    await append("type", 1, type=EventType.TASK_CREATED)
    found = await repos.subscriptions.changes("s1", "calendar.json", T0, T0 + timedelta(hours=2), source="mock_calendar")
    assert [e.id for e in found] == [first.id, tie.id, last.id]
    found[0].payload["mutated"] = True
    again = await repos.subscriptions.changes("s1", "calendar.json", T0, T0 + timedelta(hours=2), source="mock_calendar")
    assert again[0].payload == {}
