"""PostgreSQL only: what the shared repository tests cannot show.

The shared tests (unit/test_repositories.py) run every call on one engine,
where asyncio.gather still interleaves at await points. Here the racers use
separate engines, so they are separate connections and transactions, as two
worker processes would be. Also checked: the migration matches schema.py,
there is one migration head, and downgrade/upgrade round-trips.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

# Needs the harness extra; without it the module is skipped, not an error.
for _mod in ("sqlalchemy", "asyncpg", "alembic"):
    pytest.importorskip(_mod)

from sqlalchemy import inspect, text  # noqa: E402

from harness.contracts import (  # noqa: E402
    WaitCondition,
    ApprovalDecision,
    Approval,
    ActionStatus,
    Action,
    ErrorCode,
    Event,
    EventType,
    Actor,
    AgentRole,
    Limits,
    RunLane,
    StaleLease,
    Task,
    TaskError,
    TaskOrigin,
    TaskOutcome,
    TaskRequest,
    TaskStatus,
    TriggerType,
    Usage,
    UsageKind,
    VerificationResult,
    VerificationStatus,
)
from harness.storage import schema  # noqa: E402
from harness.storage import migrate  # noqa: E402
from harness.storage.migrate import heads, run  # noqa: E402
from harness.storage.postgres import PgRepositories, async_url  # noqa: E402

from ..pg import clean  # noqa: E402

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)
TTL = 60
ACTOR = Actor(actor_id="u1", scope_id="s1")


def make_task(key: str, **limits) -> Task:
    return Task.new(
        TaskRequest(request_key=key, goal="g", capability_ref="document_draft@1", limits=Limits(**limits)),
        ACTOR,
        TaskOrigin(
            agent_role=AgentRole.MAIN, agent_profile_ref="main@1", run_lane=RunLane.FOREGROUND, trigger_type=TriggerType.USER
        ),
        now=T0,
    )


@pytest_asyncio.fixture
async def pools(pg_dsn):
    """Several PgRepositories, each with its own engine and pool."""
    stores = [PgRepositories.from_dsn(pg_dsn, pool_size=4) for _ in range(6)]
    await clean(stores[0].engine)
    try:
        yield stores
    finally:
        for store in stores:
            await store.close()


# -- races across connections --------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_claims_from_separate_connections(pools):
    for i in range(10):
        await pools[0].tasks.create(make_task(f"k{i}"))
    leases = await asyncio.gather(
        *(pools[i % len(pools)].tasks.claim(f"w{i}", [RunLane.FOREGROUND], TTL, T0) for i in range(30))
    )
    got = [lease.task_id for lease in leases if lease is not None]
    assert len(got) == 10 and len(set(got)) == 10


@pytest.mark.asyncio
async def test_last_budget_unit_goes_to_one_caller(pools):
    task = (await pools[0].tasks.create(make_task("k", max_tool_calls=1))).task
    lease = await pools[0].tasks.claim("w", [RunLane.FOREGROUND], TTL, T0)
    results = await asyncio.gather(
        *(
            pools[i % len(pools)].usage.reserve(task.id, lease.attempt_id, f"c{i}", UsageKind.TOOL, Usage(tool_calls=1), T0)
            for i in range(20)
        )
    )
    assert sum(r.granted for r in results) == 1


@pytest.mark.asyncio
async def test_same_action_key_registered_once_across_connections(pools):
    task = (await pools[0].tasks.create(make_task("k", max_tool_calls=5))).task
    lease = await pools[0].tasks.claim("w", [RunLane.FOREGROUND], TTL, T0)

    def action(i):
        return Action(
            id=f"act_{i}", task_id=task.id, attempt_id=lease.attempt_id, logical_key="step-1.0",
            tool="files.read", request_hash="sha256:a", read_only=True, created_at=T0, updated_at=T0,
        )

    regs = await asyncio.gather(
        *(pools[i % len(pools)].actions.register(action(i), reserve=Usage(tool_calls=1)) for i in range(12))
    )
    assert sum(r.created for r in regs) == 1
    assert len({r.action.id for r in regs}) == 1
    # Only the one that registered was charged.
    assert len(await pools[0].usage.open_reservations(task.id)) == 1


@pytest.mark.asyncio
async def test_same_request_key_created_once_across_connections(pools):
    results = await asyncio.gather(*(pools[i % len(pools)].tasks.create(make_task("same")) for i in range(12)))
    assert sum(r.created for r in results) == 1
    assert len({r.task.id for r in results}) == 1


@pytest.mark.asyncio
async def test_old_and_new_worker_race_to_commit(pools):
    task = (await pools[0].tasks.create(make_task("k"))).task
    old = await pools[0].tasks.claim("w-old", [RunLane.FOREGROUND], TTL, T0)
    await pools[0].tasks.commit(old, TaskOutcome(status=TaskStatus.QUEUED), T0)
    new = await pools[1].tasks.claim("w-new", [RunLane.FOREGROUND], TTL, T0)
    failed = TaskOutcome(status=TaskStatus.FAILED, error=TaskError(code=ErrorCode.TOOL_ERROR, message="x"))
    results = await asyncio.gather(
        pools[2].tasks.commit(old, failed, T0), pools[3].tasks.commit(new, failed, T0), return_exceptions=True
    )
    assert isinstance(results[0], StaleLease)
    assert results[1].status is TaskStatus.FAILED and results[1].lease_epoch == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_first", [True, False])
async def test_cancel_and_completion_resolve_under_the_task_lock(pools, monkeypatch, cancel_first):
    task = (await pools[0].tasks.create(make_task("cancel-race"))).task
    lease = await pools[0].tasks.claim("w", [RunLane.FOREGROUND], TTL, T0)
    verified = await pools[0].verifications.add(
        VerificationResult(task_id=task.id, result_ref="art_done", checker_version="1", status=VerificationStatus.PASSED)
    )
    outcome = TaskOutcome(status=TaskStatus.SUCCEEDED, result_ref="art_done", verification_ref=verified.id)
    inserted, release = asyncio.Event(), asyncio.Event()
    original = pools[0].tasks._db.status_event

    async def pause_before_commit(*args, **kwargs):
        await original(*args, **kwargs)
        inserted.set()
        await release.wait()

    monkeypatch.setattr(pools[0].tasks._db, "status_event", pause_before_commit)
    if cancel_first:
        first = pools[0].tasks.request_cancel("s1", task.id, "stop", T0)
        second = pools[1].tasks.commit(lease, outcome, T0)
    else:
        first = pools[0].tasks.commit(lease, outcome, T0)
        second = pools[1].tasks.request_cancel("s1", task.id, "too late", T0)
    first_call = asyncio.create_task(first)
    second_call = None
    try:
        await asyncio.wait_for(inserted.wait(), timeout=5)
        second_call = asyncio.create_task(second)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(second_call), timeout=0.2)
    finally:
        release.set()
        await asyncio.wait_for(first_call, timeout=5)
        if second_call is not None:
            await asyncio.wait_for(second_call, timeout=5)
        else:
            second.close()
    stored = await pools[0].tasks.get("s1", task.id)
    assert stored.status is (TaskStatus.CANCELLED if cancel_first else TaskStatus.SUCCEEDED)
    assert stored.result_ref == (None if cancel_first else "art_done")
    attempt = await pools[0].tasks.get_attempt("s1", lease.attempt_id)
    assert attempt.ended_at == T0 and attempt.exit_reason == stored.status.value


@pytest.mark.asyncio
async def test_heartbeat_and_cancel_do_not_deadlock(pools):
    task = (await pools[0].tasks.create(make_task("k"))).task
    lease = await pools[0].tasks.claim("w", [RunLane.FOREGROUND], TTL, T0)
    calls = []
    for i in range(20):
        calls.append(pools[i % 3].tasks.heartbeat(lease, TTL, T0 + timedelta(seconds=i % 5)))
        calls.append(pools[3 + i % 3].usage.reserve(task.id, lease.attempt_id, f"c{i}", UsageKind.MODEL, Usage(model_calls=1), T0))
    calls.append(pools[5].tasks.request_cancel("s1", task.id, "user", T0))
    await asyncio.wait_for(asyncio.gather(*calls), timeout=20)
    assert (await pools[0].tasks.get("s1", task.id)).status is TaskStatus.CANCELLING


@pytest.mark.asyncio
@pytest.mark.parametrize("second_kind", ["cancel", "other_task", "taskless"])
async def test_event_cursor_does_not_skip_a_late_commit(pools, second_kind):
    task = (await pools[0].tasks.create(make_task("events"))).task
    await pools[0].tasks.claim("w", [RunLane.FOREGROUND], TTL, T0)
    other_task = (await pools[0].tasks.create(make_task("other"))).task if second_kind == "other_task" else None
    task_filter = task.id if second_kind == "cancel" else None
    before = await pools[0].events.list("s1", task_id=task_filter)
    cursor = before[-1].seq
    pending = None
    try:
        # Hold the first event between INSERT and COMMIT, as a delayed worker
        # can be. The public repository call below must not publish past it.
        async with pools[0].engine.begin() as conn:
            first = await pools[0].events._db.append_event(
                conn,
                Event(scope_id="s1", task_id=None if second_kind == "taskless" else task.id, type=EventType.MODEL_CALL),
            )
            if second_kind == "cancel":
                later = pools[1].tasks.request_cancel("s1", task.id, "user", T0)
            else:
                later = pools[1].events.append(
                    Event(scope_id="s1", task_id=other_task.id if other_task else None, type=EventType.EXTERNAL)
                )
            pending = asyncio.create_task(later)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(pending), timeout=0.2)
            assert await pools[2].events.list("s1", task_id=task_filter, after_seq=cursor) == []
            # Locking one scope must not stop a different scope publishing.
            independent = await asyncio.wait_for(
                pools[2].events.append(Event(scope_id="s2", type=EventType.EXTERNAL)), timeout=5
            )
            assert (await pools[2].events.list("s2"))[0].id == independent.id
    finally:
        if pending is not None:
            await asyncio.wait_for(pending, timeout=5)
    page = await pools[0].events.list("s1", task_id=task_filter, after_seq=cursor, limit=1)
    assert [event.id for event in page] == [first.id]
    remaining = await pools[0].events.list("s1", task_id=task_filter, after_seq=page[-1].seq)
    assert len(remaining) == 1 and remaining[0].seq > first.seq


# -- migrations ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_round_trip_and_requeue(pools):
    """An approval is bound to its arguments, and approving requeues the task."""
    store = pools[0]
    task = (await store.tasks.create(make_task("approve-me"))).task
    lease = await store.tasks.claim("w1", [RunLane.FOREGROUND], TTL, T0)
    action = Action(
        task_id=task.id,
        attempt_id=lease.attempt_id,
        logical_key="mail.1.0",
        tool="mail.send",
        request_hash="h1",
        read_only=False,
    )
    registered = await store.actions.register(action)
    await store.actions.update(registered.action.id, ActionStatus.NEEDS_APPROVAL, now=T0, approval_ref=None)
    waiting = TaskOutcome(status=TaskStatus.WAITING, wait_condition=WaitCondition.APPROVAL)
    await store.tasks.commit(lease, waiting, T0)

    approval = Approval(
        action_id=registered.action.id,
        request_hash="h1",
        approver="u1",
        decision=ApprovalDecision.APPROVE,
        decided_at=T0,
    )
    await store.approvals.add(approval)
    # A decision for other arguments is a different row, not a match.
    assert await store.approvals.get(registered.action.id, "h2") is None
    assert (await store.approvals.get(registered.action.id, "h1")).decision is ApprovalDecision.APPROVE

    queued = await store.tasks.requeue("s1", task.id, T0)
    assert queued.status is TaskStatus.QUEUED and queued.wait_condition is None


def test_one_migration_head():
    # Two people branching from the same revision give two heads; merge them.
    assert heads() == ["0005"]


@pytest.mark.asyncio
async def test_migration_matches_schema(pools):
    def snapshot(sync_conn):
        insp = inspect(sync_conn)
        out = {}
        for table in schema.metadata.sorted_tables:
            cols = {c["name"]: (str(c["type"]).split("(")[0], c["nullable"]) for c in insp.get_columns(table.name)}
            idx = {i["name"]: (tuple(i["column_names"]), bool(i["unique"])) for i in insp.get_indexes(table.name)}
            uqs = {tuple(u["column_names"]) for u in insp.get_unique_constraints(table.name)}
            out[table.name] = (cols, idx, uqs)
        return out

    async with pools[0].engine.connect() as conn:
        live = await conn.run_sync(snapshot)
    for table in schema.metadata.sorted_tables:
        cols, idx, uqs = live[table.name]
        want_cols = {c.name: c.nullable for c in table.columns}
        assert {k: v[1] for k, v in cols.items()} == want_cols, table.name
        want_idx = {i.name for i in table.indexes}
        assert want_idx <= set(idx), (table.name, want_idx - set(idx))
        for c in table.constraints:
            if c.__class__.__name__ == "UniqueConstraint":
                assert tuple(col.name for col in c.columns) in uqs, (table.name, c.name)
    # The claim index is partial: only queued rows are indexed.
    async with pools[0].engine.connect() as conn:
        definition = (
            await conn.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_tasks_queue'"))
        ).scalar_one()
    assert "WHERE (status = 'queued'::text)" in definition and "priority DESC" in definition


@pytest.mark.asyncio
async def test_downgrade_and_upgrade_round_trip(pools):
    engine = pools[0].engine
    await run(engine, "downgrade", "base")
    async with engine.connect() as conn:
        left = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
    assert not (left & {t.name for t in schema.metadata.sorted_tables})
    await run(engine, "upgrade", "head")
    async with engine.connect() as conn:
        back = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
    assert {t.name for t in schema.metadata.sorted_tables} <= back


@pytest.mark.asyncio
async def test_generate_and_run_a_revision(pools, tmp_path, monkeypatch):
    # Generate into a copy: never add a test revision to the source tree.
    copied = tmp_path / "migrations"
    shutil.copytree(migrate.MIGRATIONS, copied)
    monkeypatch.setattr(migrate, "MIGRATIONS", copied)
    existing = {path.name for path in (copied / "versions").glob("*.py")}
    previous_head = migrate.heads()[0]
    await migrate.run(pools[0].engine, "revision", message="test revision", autogenerate=True)
    generated = [path for path in (copied / "versions").glob("*.py") if path.name not in existing]
    assert len(generated) == 1
    assert migrate.heads() != [previous_head]
    await migrate.run(pools[0].engine, "upgrade", "head")
    async with pools[0].engine.connect() as conn:
        assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == migrate.heads()[0]
    await migrate.run(pools[0].engine, "downgrade", previous_head)


def test_async_url():
    assert async_url("postgresql://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert async_url("postgres://u@h/db") == "postgresql+asyncpg://u@h/db"
    assert async_url("postgresql+asyncpg://u@h/db") == "postgresql+asyncpg://u@h/db"
