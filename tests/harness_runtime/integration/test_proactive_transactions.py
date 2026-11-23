"""A database failure must not publish half an admission or completion."""
import pytest
import pytest_asyncio
from sqlalchemy import select, text, func
from sqlalchemy.exc import DBAPIError

from harness.contracts import ExecutionContext, Usage
from harness.storage import schema as tables
from ..scenarios.test_proactive import dataset, scripted, system, subscribe, event, ACTOR
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
    s = system(tmp_path, repos)
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
    duplicate = await s.proactive.on_event(event(path))
    assert len(duplicate) == 1
    records = await repos.subscriptions.decisions(sub.scope_id, sub.id)
    assert records[0].task_id == duplicate[0].task_id


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
