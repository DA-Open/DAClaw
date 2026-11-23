"""A database failure must not publish half an admission or completion."""
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, text, func
from sqlalchemy.exc import DBAPIError

from harness.contracts import ExecutionContext, RunLane, ToolCall, Usage, utcnow
from harness.storage import schema as tables
from ..scenarios.test_proactive import dataset, scripted, system, subscribe, event, ACTOR
from ..fakes import Clock
from harness.storage.postgres import PgRepositories
from ..pg import clean

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture
async def repos(pg_dsn):
    store = PgRepositories.from_dsn(pg_dsn)
    await clean(store.engine)
    try:
        yield store
    finally:
        await store.close()


async def reject_insert(store, table, install):
    async with store.engine.begin() as connection:
        if install:
            await connection.execute(text("CREATE FUNCTION reject_proactive_insert() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected transaction failure'; END $$"))
            await connection.execute(text(f"CREATE TRIGGER reject_insert BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION reject_proactive_insert()"))
        else:
            await connection.execute(text(f"DROP TRIGGER reject_insert ON {table}"))
            await connection.execute(text("DROP FUNCTION reject_proactive_insert()"))


async def test_admission_rolls_back_task_when_decision_insert_fails(repos, tmp_path):
    path = dataset(tmp_path)
    s = system(tmp_path, repos)
    sub = await subscribe(s, path)
    await reject_insert(repos, "subscription_events", True)
    try:
        with pytest.raises(DBAPIError, match="injected transaction failure"):
            await s.proactive.on_event(event(path))
    finally:
        await reject_insert(repos, "subscription_events", False)
    assert await repos.subscriptions.decisions("s1", sub.id) == []
    async with repos.engine.connect() as connection:
        assert (await connection.execute(select(func.count()).select_from(tables.tasks))).scalar_one() == 0
    assert len(await s.proactive.on_event(event(path))) == 1


async def test_loads_subscriptions_saved_by_the_earlier_draft(repos, tmp_path):
    path = dataset(tmp_path)
    s = system(tmp_path, repos, model=scripted(path))
    sub = await subscribe(s, path)
    body = sub.model_dump(mode="json", exclude={"daily_budget", "result_ttl_seconds"})
    body.update({
        "daily_limits": {"max_model_calls": 2, "max_tool_calls": 12, "max_total_tokens": 6000},
        "result_ttl_sec": 90,
    })
    async with repos.engine.begin() as connection:
        await connection.execute(
            tables.subscriptions.update().where(tables.subscriptions.c.id == sub.id).values(body=body)
        )
    restored = await repos.subscriptions.get(sub.scope_id, sub.id)
    assert restored.daily_budget == Usage(model_calls=2, tool_calls=12, input_tokens=6000)
    assert restored.result_ttl_seconds == 90
    assert await repos.subscriptions.list_for_scope(sub.scope_id) == [restored]
    assert await repos.subscriptions.list_all() == [restored]
    admitted = await s.proactive.on_event(event(path))
    assert len(admitted) == 1
    records = await repos.subscriptions.decisions(sub.scope_id, sub.id)
    assert records[0].task_id == admitted[0].task_id
    report = await s.worker(lanes=["background"]).run_once()
    assert report.committed and report.status == "succeeded"
    item, = await s.delivery.inbox(sub.scope_id)
    assert item.valid_until - item.created_at == timedelta(seconds=90)
    assert (await s.delivery.read(sub.scope_id, item.id, explicit=True))["card"]["profile"]["tables"][0]["row_count"] == 3

    assert await s.proactive.on_event(event(path, "v2")) == []
    before = await repos.subscriptions.decisions(sub.scope_id, sub.id)
    deferred = next(record for record in before if record.input_snapshot["data_version"] == "v2")
    assert deferred.reason == "daily_budget"
    consumed = (await s.tasks.get(sub.scope_id, admitted[0].task_id)).usage
    new_budget = Usage(
        model_calls=consumed.model_calls + 1,
        tool_calls=consumed.tool_calls + 1,
        input_tokens=consumed.total_tokens + 1,
    )
    updated = restored.model_copy(update={"daily_budget": new_budget, "version": 2})
    async with repos.engine.begin() as connection:
        await connection.execute(tables.subscriptions.update().where(tables.subscriptions.c.id == sub.id)
                                 .values(body=updated.model_dump(mode="json")))
    await s.scheduler.tick()
    after = await repos.subscriptions.decisions(sub.scope_id, sub.id)
    retried = next(record for record in after if record.event_id == deferred.event_id)
    assert retried.decision == "admit" and retried.task_id, retried.input_snapshot
    assert retried.input_snapshot["subscription_version"] == 2
    assert retried.evaluations[0]["reason"] == "daily_budget"


async def test_completion_rolls_back_status_when_inbox_insert_fails(repos, tmp_path):
    path = dataset(tmp_path)
    s = system(tmp_path, repos, model=scripted(path))
    await subscribe(s, path)
    task_id = (await s.proactive.on_event(event(path)))[0].task_id
    lease = await s.tasks.claim("b", ["background"])
    task = await s.tasks.get("s1", task_id)
    profile = s.agents.resolve_ref("proactive@1")
    ctx = await s.builder.build(task, profile, lease)
    outcome = await s.runner.run(ctx, profile, ExecutionContext(lease=lease, actor=ACTOR, agent_role="proactive", run_lane="background"))
    assert outcome.status == "succeeded"
    await reject_insert(repos, "inbox_items", True)
    try:
        with pytest.raises(DBAPIError, match="injected transaction failure"):
            await s.tasks.commit(lease, outcome)
    finally:
        await reject_insert(repos, "inbox_items", False)
    assert (await s.tasks.get("s1", task_id)).status == "running"
    assert await repos.delivery.list("s1") == []
    assert not any(e.type == "background_result_ready" for e in await repos.events.list("s1", task_id=task_id))
    assert (await s.tasks.commit(lease, outcome)).status == "succeeded"


async def test_recovery_restores_only_live_subscription_resources(repos, tmp_path):
    clock = Clock(utcnow())
    kept = tmp_path / "retained.csv"
    kept.write_text("id,amount\n1,10\n", encoding="utf-8")
    removed = tmp_path / "removed.csv"
    removed.write_text("id,amount\n2,99\n", encoding="utf-8")
    first = system(tmp_path, repos, clock=clock)
    async with first.lifespan():
        sub = await subscribe(first, kept, resources=[str(kept), str(removed)])
        task_id = (await first.proactive.on_event(event(kept)))[0].task_id
        lease = await first.tasks.claim("first", [RunLane.BACKGROUND])
        ctx = ExecutionContext(lease=lease, actor=ACTOR, agent_role="proactive", run_lane="background")
        for name, path in (("kept_rows", kept), ("removed_rows", removed)):
            connected = await first.gateway.invoke(ctx, ToolCall(
                tool="data.connect", args={"path": str(path), "name": name}, logical_key=name,
            ))
            assert connected.status == "succeeded", connected

        narrowed = sub.model_copy(update={"resources": [str(kept)], "version": sub.version + 1})
        async with repos.engine.begin() as connection:
            await connection.execute(tables.subscriptions.update().where(tables.subscriptions.c.id == sub.id)
                                     .values(body=narrowed.model_dump(mode="json")))
        clock.advance(61)
        assert await first.scheduler.tick() == 1

    restarted = system(tmp_path, repos, clock=clock)
    async with restarted.lifespan():
        fresh = await restarted.tasks.claim("restarted", [RunLane.BACKGROUND])
        assert fresh.task_id == task_id and fresh.lease_epoch == 2
        ctx = ctx.model_copy(update={"lease": fresh})
        allowed = await restarted.gateway.invoke(ctx, ToolCall(
            tool="data.query", args={"sql": "SELECT amount FROM kept_rows"}, logical_key="query-kept",
        ))
        assert allowed.status == "succeeded" and "| 10 |" in allowed.summary, allowed
        blocked = await restarted.gateway.invoke(ctx, ToolCall(
            tool="data.query", args={"sql": "SELECT amount FROM removed_rows"}, logical_key="query-removed",
        ))
        assert blocked.status == "failed" and not blocked.artifact_refs
        forbidden = await restarted.gateway.invoke(ctx, ToolCall(
            tool="data.connect", args={"path": str(removed)}, logical_key="reconnect-removed",
        ))
        assert forbidden.status == "denied"
