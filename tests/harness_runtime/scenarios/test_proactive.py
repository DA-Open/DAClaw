"""Q1-Q3 acceptance on the same memory and PostgreSQL repositories."""
import asyncio
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import httpx
import pytest

from harness.api import TokenAuth, create_app
from harness.app import build_system
from harness.config import HarnessConfig
from harness.contracts import (
    Actor, AgentRole, Event, EventType, ExecutionContext, Limits, NotFound, RunLane, Task,
    StaleLease, Subscription, TaskOrigin, TaskOutcome, TaskRequest, ToolCall, Usage,
    UsageKind, utcnow,
)
from harness.runtime.models import ScriptedModelAdapter, result_step, tool_step
from harness.tools.connectors import data_profile
from harness.storage.memory import InMemoryRepositories
from ..fakes import Clock
from ..unit.test_repositories import repos  # shared backend fixture

pytestmark = pytest.mark.asyncio
ACTOR = Actor(scope_id="s1", actor_id="u1")


def dataset(tmp_path):
    path = tmp_path / "sales.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("create table sales(id integer, amount real)")
        db.executemany("insert into sales values(?,?)", [(1, 10), (1, None), (2, 30)])
    return path


def scripted(path, *, corrupt=False, before=()):
    def write(ctx):
        content = [m["content"] for m in ctx.messages if m.get("role") == "tool"][-1]
        assert content.startswith("status: succeeded"), content
        output = json.loads(content.splitlines()[1])
        card = dict(schema_version="1", subject_ref=str(path), data_version="v1", meaning_source="computed",
                    profile_action_id=output["action_id"], profile=output["profile"], inferences=[])
        if corrupt:
            card["profile"]["tables"][0]["row_count"] += 1
        return tool_step(("artifacts.write", {"name": "data_card", "media_type": "application/json", "content": json.dumps(card)}))
    def submit(ctx):
        written = [m["content"] for m in ctx.messages if m.get("role") == "tool" and "wrote artifact" in m.get("content", "")][-1]
        return result_step("Data card generated", {"data_card": written.split(" as ")[1].split(" ")[0]})
    return ScriptedModelAdapter([*before, tool_step(("data.profile", {"path": str(path)})), write, submit])


def system(tmp_path, repos, *, model=None, clock=utcnow, limit=2):
    return build_system(HarnessConfig(artifact_root=tmp_path / "artifacts", background_scope_limit=limit),
        model or ScriptedModelAdapter(), repos=repos, clock=clock,
        policy_spec={"users": {"s1": {role: {"paths": [str(tmp_path)]} for role in ("main", "proactive")}}})


async def subscribe(s, path, *, resources=None, **kw):
    return await s.proactive.create_subscription(Subscription(scope_id="s1", owner_id="u1", goal="Understand sales quality",
        subject_ref=str(path), resources=resources if resources is not None else [str(path)],
        capability_ref="data_card@1", event_filter={"source": "dataset"},
        limits=Limits(max_model_calls=10, max_tool_calls=30), **kw))


def event(path, version="v1", *, key=None, fingerprint=None):
    return Event(scope_id="s1", type=EventType.EXTERNAL, source="dataset", source_event_id=key or version,
        subject_ref=str(path), payload={"kind": "data_connected" if version == "v1" else "data_updated",
                                     "data_version": version, "fingerprint": fingerprint or version})


async def main_task(s, goal="Explain sales", **kw):
    return await s.tasks.create(TaskRequest(request_key=goal, goal=goal, capability_ref="document_draft@2", **kw), ACTOR,
        TaskOrigin(agent_role="main", agent_profile_ref="main@1", run_lane="foreground", trigger_type="user"))


async def test_duplicate_delivery_and_two_subscriptions(repos, tmp_path):
    s = system(tmp_path, repos)
    path = dataset(tmp_path)
    subs = [await subscribe(s, path) for _ in range(2)]
    outputs = await asyncio.gather(*(s.proactive.on_event(event(path)) for _ in range(4)))
    ids = {t.task_id for group in outputs for t in group}
    assert len(ids) == 2
    assert {t.task_id for t in await s.proactive.on_event(event(path))} == ids
    for sub in subs:
        records = await repos.subscriptions.decisions("s1", sub.id)
        assert len(records) == 1 and records[0].decision == "admit" and records[0].kind == "baseline"
        assert records[0].task_id in ids
        assert (await s.tasks.get("s1", records[0].task_id)).subscription_id == sub.id
        assert records[0].input_snapshot["fingerprint"] == "v1"
    with pytest.raises(NotFound):
        await repos.subscriptions.get("another-scope", subs[0].id)


async def test_revoke_cancels_running_and_queued_and_denies_cached_tools(repos, tmp_path):
    s = system(tmp_path, repos)
    path = dataset(tmp_path)
    sub = await subscribe(s, path)
    task_id = (await s.proactive.on_event(event(path)))[0].task_id
    lease = await s.tasks.claim("background", [RunLane.BACKGROUND])
    execution = ExecutionContext(lease=lease, actor=ACTOR, agent_role="proactive", run_lane="background")
    call = ToolCall(tool="artifacts.write", args={"name": "draft", "content": "safe"}, logical_key="a")
    assert (await s.gateway.invoke(execution, call)).status == "succeeded"
    main = await main_task(s)
    await s.proactive.revoke("s1", sub.id)
    assert (await s.tasks.get("s1", task_id)).status == "cancelling"
    assert (await s.tasks.get("s1", main.id)).status == "queued"
    assert (await s.gateway.invoke(execution, call)).status == "denied"
    assert await s.proactive.on_event(event(path, "v2")) == []
    assert (await repos.subscriptions.decisions("s1", sub.id))[-1].decision == "skip"


async def test_defer_recovery_and_foreground_capacity(repos, tmp_path):
    clock = Clock()
    s = system(tmp_path, repos, clock=clock, limit=1)
    path = dataset(tmp_path)
    sub = await subscribe(s, path)
    first = (await s.proactive.on_event(event(path)))[0].task_id
    assert await s.proactive.on_event(event(path, "v2")) == []
    lease = await s.tasks.claim("background", [RunLane.BACKGROUND])
    main = await main_task(s)
    assert (await s.tasks.claim("foreground", [RunLane.FOREGROUND])).task_id == main.id
    clock.advance(61)
    assert await s.scheduler.tick() == 2
    assert (await s.tasks.get("s1", first)).status == "queued"
    assert (await s.tasks.get("s1", main.id)).status == "queued"
    with pytest.raises(StaleLease):
        await s.tasks.heartbeat(lease)
    await s.tasks.request_cancel("s1", first, "test complete")
    await s.scheduler.tick()
    records = await repos.subscriptions.decisions("s1", sub.id)
    assert len(records) == 2 and all(r.decision == "admit" for r in records)
    retried = next(r for r in records if r.input_snapshot["data_version"] == "v2")
    assert any(r["decision"] == "defer" and r["reason"] == "subscription_busy" for r in retried.evaluations)


@pytest.mark.parametrize("budget", [Usage(model_calls=1), Usage(input_tokens=1, output_tokens=1)])
async def test_daily_budget_defers_until_next_utc_day(repos, tmp_path, budget):
    clock = Clock()
    s = system(tmp_path, repos, clock=clock)
    path = dataset(tmp_path)
    sub = await subscribe(s, path, daily_budget=budget)
    task_id = (await s.proactive.on_event(event(path)))[0].task_id
    lease = await s.tasks.claim("b", [RunLane.BACKGROUND])
    charge = Usage(model_calls=1, input_tokens=1, output_tokens=1)
    await repos.usage.reserve(task_id, lease.attempt_id, "charge", UsageKind.MODEL, charge, clock())
    await repos.usage.settle("charge", charge, clock())
    await s.tasks.request_cancel("s1", task_id, "done")
    await s.tasks.commit(lease, TaskOutcome(status="cancelled"))
    assert await s.proactive.on_event(event(path, "v2")) == []
    records = await repos.subscriptions.decisions("s1", sub.id)
    assert records[-1].reason == "daily_budget"
    clock.advance(86400)
    await s.scheduler.tick()
    assert (await repos.subscriptions.decisions("s1", sub.id))[-1].decision == "admit"


@pytest.mark.parametrize("corrupt", [False, True])
async def test_data_card_end_to_end_and_tamper_failure(repos, tmp_path, corrupt):
    path = dataset(tmp_path)
    model = scripted(path, corrupt=corrupt)
    s = system(tmp_path, repos, model=model)
    sub = await subscribe(s, path)
    task_id = (await s.proactive.on_event(event(path)))[0].task_id
    report = await s.worker(lanes=[RunLane.BACKGROUND]).run_once()
    assert report.committed and report.status == ("failed" if corrupt else "succeeded"), report
    records = await repos.verifications.list_for_task(task_id)
    replay = next(c for c in records[-1].checks if c.id == "stats_replay")
    assert replay.status == ("failed" if corrupt else "passed")
    items = await s.delivery.inbox("s1")
    if corrupt:
        assert items == []
        return
    assert len(items) == 1 and items[0].mode == "notify"
    item = items[0]
    assert item.valid_until == (await s.tasks.get("s1", task_id)).result_expires_at
    main = await main_task(s)
    lease = await s.tasks.claim("f", [RunLane.FOREGROUND])
    context = await s.builder.build(main, s.agents.resolve_ref("main@1"), lease)
    assert len(context.injected_materials) == 1
    assert "row_count" not in context.injected_materials[0].model_dump_json()
    assert await s.delivery.materials(main.model_copy(update={"goal": "Unrelated astronomy"})) == []
    first = await s.delivery.read("s1", item.id)
    assert first["card"]["profile"]["tables"][0]["row_count"] == 3
    assert (await s.delivery.read("s1", item.id))["notice"] == "already_reserved"
    assert (await s.delivery.inbox("s1"))[0].mode == "digest"
    await repos.delivery.ack("s1", item.id, "handled", "useful", utcnow())
    assert (await repos.subscriptions.decisions("s1", sub.id))[0].backfill_label == "useful"
    await s.proactive.on_event(event(path, "v2"))
    assert await s.delivery.materials(main) == []
    assert (await s.delivery.read("s1", item.id, explicit=True))["card"]
    execution = ExecutionContext(lease=lease, actor=ACTOR, agent_role="main", run_lane="foreground")
    calls = [ToolCall(tool="background_results.read", args={"id": item.id, "explicit": True}, logical_key="read-card"),
             ToolCall(tool="artifacts.read", args={"artifact_id": item.result_ref}, logical_key="read-artifact")]
    for call in calls:
        assert (await s.gateway.invoke(execution, call)).status == "succeeded"
    await s.proactive.revoke("s1", sub.id)
    for call in calls:
        assert (await s.gateway.invoke(execution, call)).status == "denied"
    missing = ToolCall(tool="artifacts.read", args={"artifact_id": "missing"}, logical_key="missing")
    assert (await s.gateway.invoke(execution, missing)).status == "failed"
    with pytest.raises(NotFound):
        await s.delivery.read("s1", item.id, explicit=True)


async def test_subscription_resources_and_tool_boundaries(repos, tmp_path):
    path = dataset(tmp_path)
    s = system(tmp_path, repos)
    with pytest.raises(PermissionError):
        await subscribe(s, tmp_path.parent / "unauthorized.sqlite")
    await subscribe(s, path)
    await s.proactive.on_event(event(path))
    lease = await s.tasks.claim("b", [RunLane.BACKGROUND])
    execution = ExecutionContext(lease=lease, actor=ACTOR, agent_role="proactive", run_lane="background")
    for tool, args in [("files.write", {"path": str(path)}), ("data.profile", {"path": str(tmp_path / "other.sqlite")})]:
        assert (await s.gateway.invoke(execution, ToolCall(tool=tool, args=args, logical_key=tool))).status == "denied"


async def test_trusted_api_scope_and_decision_export(repos, tmp_path):
    s = system(tmp_path, repos)
    path = dataset(tmp_path)
    auth = TokenAuth([{"sha256": hashlib.sha256(b"test").hexdigest(), "scope_id": "s1", "actor_id": "u1"}])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(s, auth)), base_url="http://test",
                                headers={"Authorization": "Bearer test"}) as client:
        body = {"goal": "data quality", "subject_ref": str(path), "resources": [str(path)], "event_filter": {"source": "dataset"}}
        assert (await client.post("/subscriptions", json={**body, "scope_id": "other"})).status_code == 400
        sub = await client.post("/subscriptions", json=body)
        assert sub.status_code == 201, sub.text
        assert sub.json()["result_ttl_seconds"] == 7 * 24 * 3600
        assert sub.json()["daily_budget"] == Usage(
            model_calls=50, tool_calls=150, input_tokens=250_000
        ).model_dump()
        partial = await client.post("/subscriptions", json={**body, "daily_budget": {"model_calls": 1}})
        assert partial.status_code == 201, partial.text
        assert partial.json()["daily_budget"] == Usage(
            model_calls=1, tool_calls=150, input_tokens=250_000
        ).model_dump()
        paused = []
        for budget in ({}, {"model_calls": 0}, {"tool_calls": 0}, {"input_tokens": 0}):
            created = await client.post("/subscriptions", json={**body, "daily_budget": budget})
            assert created.status_code == 201, created.text
            paused.append(created.json()["id"])
        payload = {"source": "dataset", "source_event_id": "v1", "subject_ref": str(path), "data": event(path).payload}
        first, second = await client.post("/events", json=payload), await client.post("/events", json=payload)
        assert first.status_code == 202 and first.json() == second.json()
        assert len(first.json()["tasks"]) == 2
        for subscription_id in paused:
            records = await client.get(f"/subscriptions/{subscription_id}/decisions")
            record = json.loads(records.text)
            assert record["reason"] == "daily_budget" and record["task_id"] is None
        export = await client.get(f"/subscriptions/{sub.json()['id']}/decisions")
        assert json.loads(export.text)["policy_version"] == "proactive-admission@1"
        assert (await client.get("/subscriptions", headers={"Authorization": "Bearer wrong"})).status_code == 401


async def test_legacy_sqlite_profile_preserves_statistics(tmp_path):
    path = dataset(tmp_path)
    profile = await data_profile._profile_dataset(path)
    assert profile["tables"][0]["row_count"] == 3
    assert {f["kind"] for f in profile["findings"]} == {"null_values", "duplicate_key_candidate"}


async def test_profile_and_explicit_query_use_authorized_source(tmp_path):
    s = system(tmp_path, InMemoryRepositories())
    path = tmp_path / "sales.csv"
    path.write_text("id,amount\n1,10\n2,20\n", encoding="utf-8")
    await subscribe(s, path)
    await s.proactive.on_event(event(path))
    lease = await s.tasks.claim("background", [RunLane.BACKGROUND])
    ctx = ExecutionContext(lease=lease, actor=ACTOR, agent_role="proactive", run_lane="background")
    profiled = await s.gateway.invoke(ctx, ToolCall(tool="data.profile", args={"path": str(path)}, logical_key="profile"))
    assert profiled.status == "succeeded", profiled
    assert json.loads(profiled.summary)["profile"]["tables"][0]["row_count"] == 2
    disconnected = await s.gateway.invoke(ctx, ToolCall(
        tool="data.query", args={"sql": "SELECT * FROM sales"}, logical_key="disconnected"
    ))
    assert disconnected.status == "failed" and "connect a table" in disconnected.error.message
    connected = await s.gateway.invoke(ctx, ToolCall(
        tool="data.connect", args={"path": str(path), "name": "named_sales"}, logical_key="connect"
    ))
    assert connected.status == "succeeded", connected
    queried = await s.gateway.invoke(ctx, ToolCall(tool="data.query", args={"sql": "SELECT * FROM named_sales"}, logical_key="query"))
    assert queried.status == "succeeded", queried
    assert len(queried.artifact_refs) == 2
    evidence = await s.artifacts.read(queried.artifact_refs[0], ACTOR)
    assert json.loads(evidence.content)["row_count"] == 2
    blocked = await s.gateway.invoke(ctx, ToolCall(
        tool="data.connect", args={"path": str(tmp_path / "unsubscribed.csv")}, logical_key="other"
    ))
    assert blocked.status == "denied"


async def test_data_card_replays_with_an_existing_query_alias_and_collision(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text("id,amount\n1,10\n2,20\n", encoding="utf-8")
    other = tmp_path / "other" / "sales.csv"
    other.parent.mkdir()
    other.write_text("id,amount\n9,99\n", encoding="utf-8")
    model = scripted(path, before=[
        tool_step(("data.connect", {"path": str(other), "name": "renamed_sales"})),
        tool_step(("data.connect", {"path": str(path), "name": "renamed_sales"})),
    ])
    repos = InMemoryRepositories()
    s = system(tmp_path, repos, model=model)
    await subscribe(s, path, resources=[str(path), str(other)])
    task_id = (await s.proactive.on_event(event(path)))[0].task_id
    report = await s.worker(lanes=[RunLane.BACKGROUND]).run_once()
    assert report.committed and report.status == "succeeded", report
    connect_actions = [action for action in await repos.actions.list_for_task(task_id)
                       if action.tool == "data.connect"]
    assert len(connect_actions) == 2 and all(action.status == "succeeded" for action in connect_actions)
    connected_tables = {
        json.loads((await s.artifacts.read(action.result_ref, ACTOR)).content)["data"]["table"]
        for action in connect_actions
    }
    assert connected_tables == {"renamed_sales", "renamed_sales_2"}
    verification = (await repos.verifications.list_for_task(task_id))[-1]
    assert next(check for check in verification.checks if check.id == "stats_replay").status == "passed"
    item, = await s.delivery.inbox(ACTOR.scope_id)
    card = (await s.delivery.read(ACTOR.scope_id, item.id))["card"]
    table, = card["profile"]["tables"]
    assert table["table"] == "sales" and table["row_count"] == 2


@pytest.mark.parametrize("extension", [".txt", ".pq"])
async def test_profile_accepts_legacy_text_and_parquet_extensions(tmp_path, extension):
    path = tmp_path / f"sales{extension}"
    if extension == ".txt":
        path.write_text("id,amount\n1,10\n2,20\n", encoding="utf-8")
    else:
        import duckdb

        with duckdb.connect() as connection:
            connection.execute("COPY (SELECT * FROM (VALUES (1, 10), (2, 20)) AS sales(id, amount)) "
                               "TO ? (FORMAT PARQUET)", [str(path)])
    profile = await data_profile._profile_dataset(path)
    table, = profile["tables"]
    assert table["table"] == "sales" and table["row_count"] == 2


async def test_profile_reads_quoted_duckdb_table_and_column(tmp_path):
    import duckdb

    path = tmp_path / "sales.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute('CREATE TABLE "orders""east" ("customer""id" INTEGER)')
        connection.execute('INSERT INTO "orders""east" VALUES (7), (8)')
    profile = await data_profile._profile_dataset(path)
    table, = profile["tables"]
    assert table["table"] == 'orders"east'
    assert table["column_stats"]['customer"id']["top_values"] == [[7, 1], [8, 1]]


async def test_legacy_excel_profile(tmp_path):
    from openpyxl import Workbook

    path = tmp_path / "sales.xlsx"
    book = Workbook()
    book.active.append(["id", "amount"])
    book.active.append([1, 10])
    book.active.append([2, 20])
    book.save(path)
    profile = await data_profile._profile_dataset(path)
    assert profile["tables"][0]["row_count"] == 2


async def test_background_claim_limit_is_atomic_and_main_is_independent(repos, tmp_path):
    s = system(tmp_path, repos, limit=1)
    path = dataset(tmp_path)
    sub = await subscribe(s, path)
    for n in range(3):
        request = s.proactive._request(sub, str(n))
        await s.tasks.create(request, ACTOR, s.proactive._origin(sub, str(n)))
    leases = await asyncio.gather(*(s.tasks.claim(f"b{n}", [RunLane.BACKGROUND]) for n in range(5)))
    assert len([l for l in leases if l]) == 1
    main = await main_task(s)
    assert (await s.tasks.claim("foreground", [RunLane.FOREGROUND])).task_id == main.id


async def test_cron_is_durable_idempotent_and_restarts(repos, tmp_path):
    clock = Clock()
    s = system(tmp_path, repos, clock=clock)
    path = dataset(tmp_path)
    sub = await s.proactive.create_subscription(Subscription(scope_id="s1", owner_id="u1", goal="Scheduled quality",
        subject_ref=str(path), resources=[str(path)], capability_ref="data_card@1", schedule="* * * * *",
        created_at=clock() - timedelta(minutes=1)))
    await s.proactive.on_event(event(path))
    assert await repos.subscriptions.decisions("s1", sub.id) == []
    await s.scheduler.tick()
    fresh = system(tmp_path, repos, clock=clock)
    await fresh.scheduler.tick()
    records = await repos.subscriptions.decisions("s1", sub.id)
    assert len(records) == 1 and records[0].decision == "admit"
    assert (await s.tasks.get("s1", records[0].task_id)).trigger_type == "schedule"


async def test_profile_timeout_reaps_child_and_keeps_event_loop_responsive(tmp_path, monkeypatch):
    path = tmp_path / "sales.csv"
    path.write_text("id,amount\n1,10\n", encoding="utf-8")
    s = system(tmp_path, InMemoryRepositories())
    await subscribe(s, path)
    await s.proactive.on_event(event(path))
    lease = await s.tasks.claim("background", [RunLane.BACKGROUND])
    ctx = ExecutionContext(lease=lease, actor=ACTOR, agent_role="proactive", run_lane="background")
    spawned = asyncio.Event()
    start = asyncio.create_subprocess_exec
    children = []

    async def slow_profile(*_args, **kwargs):
        child = await start(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        children.append((child, Path(kwargs["cwd"])))
        spawned.set()
        return child

    async def pulse():
        await asyncio.wait_for(spawned.wait(), 5)
        await asyncio.sleep(0.01)
        return children[0][0].returncode is None

    with monkeypatch.context() as patch:
        patch.setattr(data_profile, "PROFILE_TIMEOUT_SECONDS", 0.2)
        patch.setattr(data_profile.asyncio, "create_subprocess_exec", slow_profile)
        outcome, pulsed = await asyncio.gather(s.gateway.invoke(ctx, ToolCall(
            tool="data.profile", args={"path": str(path)}, logical_key="slow"
        )), pulse())
    assert pulsed and outcome.status == "failed" and "data profile exceeded" in outcome.error.message
    child, scratch = children[0]
    assert child.returncode is not None and not scratch.exists()
    healthy = await s.gateway.invoke(ctx, ToolCall(
        tool="data.profile", args={"path": str(path)}, logical_key="healthy"
    ))
    assert healthy.status == "succeeded", healthy


async def test_delivery_rules_thresholds_and_quiet_window(tmp_path):
    from harness.delivery.rules import delivery_item
    from harness.contracts import AdmissionDecision
    path = dataset(tmp_path)
    s = system(tmp_path, InMemoryRepositories())
    sub = await subscribe(s, path)
    task = s.tasks.prepare(s.proactive._request(sub, "event"), ACTOR, s.proactive._origin(sub, "event"))
    task.result_ref = "artifact"
    record = AdmissionDecision(subscription_id=sub.id, event_id="event", decision="admit", reason="new",
                               policy_version="p@1", input_snapshot={"data_version": "v1"})
    card = {"profile": {"tables": [], "findings": [{"impact": 2, "urgency": 1}]}}
    now = utcnow()
    assert delivery_item(task, sub, record, card, now, allowed=True).mode == "notify"
    sub.proactivity = "quiet"
    assert delivery_item(task, sub, record, card, now, allowed=True).mode == "digest"
    sub.quiet_hours = (now.hour, (now.hour + 1) % 24)
    assert delivery_item(task, sub, record, card, now, allowed=True).mode == "silent"
    sub.quiet_hours = (0, 0)
    assert delivery_item(task, sub, record, card, now, allowed=False).mode == "silent"


async def test_main_question_is_absent_and_expiry_revokes_injection(repos, tmp_path):
    path = dataset(tmp_path)
    clock = Clock(utcnow())
    model = scripted(path)
    s = system(tmp_path, repos, clock=clock, model=model)
    main = await main_task(s, goal="sales: SECRET_MAIN_QUESTION")
    sub = await subscribe(s, path, result_ttl_seconds=60)
    await s.proactive.on_event(event(path))
    report = await s.worker(lanes=[RunLane.BACKGROUND]).run_once()
    assert report.status == "succeeded", report
    assert all("SECRET_MAIN_QUESTION" not in json.dumps(ctx.messages) for ctx in model.calls)
    assert await s.delivery.materials(main)
    clock.advance(61)
    assert await s.delivery.materials(main) == []
    assert await s.delivery.inbox("s1") == []


async def test_fingerprint_skip_and_diff_after_baseline(repos, tmp_path):
    path = dataset(tmp_path)
    s = system(tmp_path, repos, model=scripted(path))
    sub = await subscribe(s, path)
    await s.proactive.on_event(event(path))
    assert (await s.worker(lanes=[RunLane.BACKGROUND]).run_once()).status == "succeeded"
    assert await s.proactive.on_event(event(path, "v2", fingerprint="v1")) == []
    assert (await repos.subscriptions.decisions("s1", sub.id))[-1].reason == "unchanged_fingerprint"
    assert len(await s.proactive.on_event(event(path, "v3"))) == 1
    record = (await repos.subscriptions.decisions("s1", sub.id))[-1]
    assert record.kind == "diff" and record.input_snapshot["previous_version"] == "v1"
