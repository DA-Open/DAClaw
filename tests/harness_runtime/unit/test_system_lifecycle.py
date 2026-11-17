"""Repository selection and ownership at the system's startup/shutdown boundary."""

import argparse
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from harness.app import HarnessSystem, build_system
from harness.config import HarnessConfig
from harness.runtime.models import ScriptedModelAdapter
from harness.storage.memory import InMemoryRepositories
from harness.storage.postgres import PgRepositories

pytestmark = pytest.mark.asyncio

DSN = "postgresql://harness:unused@localhost/harness_unit_test"


@pytest.fixture
def pg_boundary():
    """Use the real repositories; only the database connection is replaced."""
    engine = create_autospec(AsyncEngine, instance=True)
    connection = create_autospec(AsyncConnection, instance=True)
    connection.__aenter__.return_value = connection
    engine.connect.return_value = connection
    events = []

    async def execute(statement):
        events.append("ready")

    async def dispose():
        events.append("close")

    connection.execute.side_effect = execute
    engine.dispose.side_effect = dispose
    return PgRepositories(engine), engine, connection, events


def system_config(tmp_path, *, database_dsn=None):
    return HarnessConfig(database_dsn=database_dsn, artifact_root=tmp_path / "artifacts")


async def test_no_dsn_uses_memory_without_constructing_postgres(tmp_path, monkeypatch):
    factory = Mock(side_effect=AssertionError("PostgreSQL must not be constructed"))
    monkeypatch.setattr(PgRepositories, "from_dsn", factory)
    system = build_system(system_config(tmp_path), ScriptedModelAdapter([]))
    assert isinstance(system.repos, InMemoryRepositories)
    async with system.lifespan():
        assert isinstance(system.repos, InMemoryRepositories)
    factory.assert_not_called()


@pytest.mark.parametrize("postgres", [False, True])
async def test_injected_repository_wins_and_stays_caller_owned(tmp_path, monkeypatch, pg_boundary, postgres):
    pg, engine, _, events = pg_boundary
    repos = pg if postgres else InMemoryRepositories()
    factory = Mock(side_effect=AssertionError("injected repositories must take precedence"))
    monkeypatch.setattr(PgRepositories, "from_dsn", factory)
    system = build_system(system_config(tmp_path, database_dsn=DSN), ScriptedModelAdapter([]), repos=repos)
    assert system.repos is repos
    async with system.lifespan():
        assert system.repos is repos
    factory.assert_not_called()
    engine.connect.assert_not_called()
    engine.dispose.assert_not_awaited()
    assert events == []


async def test_configured_postgres_is_checked_then_closed(tmp_path, monkeypatch, pg_boundary):
    repos, engine, connection, events = pg_boundary
    factory = Mock(return_value=repos)
    monkeypatch.setattr(PgRepositories, "from_dsn", factory)
    system = build_system(system_config(tmp_path, database_dsn=DSN), ScriptedModelAdapter([]))
    assert system.repos is repos
    factory.assert_called_once_with(DSN)
    connection.execute.assert_not_awaited()
    async with system.lifespan():
        assert events == ["ready"]
        engine.dispose.assert_not_awaited()
        events.append("serving")
    assert events == ["ready", "serving", "close"]
    engine.dispose.assert_awaited_once()


async def test_postgres_construction_failure_does_not_fall_back_to_memory(tmp_path, monkeypatch):
    error = RuntimeError("cannot construct the configured database engine")
    monkeypatch.setattr(PgRepositories, "from_dsn", Mock(side_effect=error))
    with pytest.raises(RuntimeError) as caught:
        build_system(system_config(tmp_path, database_dsn=DSN), ScriptedModelAdapter([]))
    assert caught.value is error


async def test_owned_postgres_closes_when_the_body_raises(tmp_path, monkeypatch, pg_boundary):
    repos, engine, _, events = pg_boundary
    monkeypatch.setattr(PgRepositories, "from_dsn", Mock(return_value=repos))
    system = build_system(system_config(tmp_path, database_dsn=DSN), ScriptedModelAdapter([]))
    error = RuntimeError("worker failed")
    with pytest.raises(RuntimeError) as caught:
        async with system.lifespan():
            raise error
    assert caught.value is error
    assert events == ["ready", "close"]
    engine.dispose.assert_awaited_once()


async def test_readiness_failure_closes_owned_postgres_and_propagates(tmp_path, monkeypatch, pg_boundary):
    repos, engine, connection, events = pg_boundary
    error = RuntimeError("tasks table is unavailable")
    connection.execute.side_effect = error
    monkeypatch.setattr(PgRepositories, "from_dsn", Mock(return_value=repos))
    system = build_system(system_config(tmp_path, database_dsn=DSN), ScriptedModelAdapter([]))
    entered = False
    with pytest.raises(RuntimeError) as caught:
        async with system.lifespan():
            entered = True
    assert caught.value is error
    assert not entered
    assert system.repos is repos
    connection.execute.assert_awaited_once()
    engine.dispose.assert_awaited_once()
    assert events == ["close"]


async def test_check_ready_propagates_a_connection_failure(pg_boundary):
    repos, _, connection, _ = pg_boundary
    error = ConnectionError("database is unreachable")
    connection.__aenter__.side_effect = error
    with pytest.raises(ConnectionError) as caught:
        await repos.check_ready()
    assert caught.value is error
    connection.execute.assert_not_awaited()


async def test_api_only_enters_the_database_lifespan(tmp_path, monkeypatch, pg_boundary):
    import uvicorn

    from harness.api import __main__ as api_cli

    repos, engine, _, events = pg_boundary
    monkeypatch.setattr(PgRepositories, "from_dsn", Mock(return_value=repos))
    monkeypatch.setattr(HarnessConfig, "from_env", Mock(return_value=system_config(tmp_path, database_dsn=DSN)))
    server = Mock()
    monkeypatch.setattr(uvicorn, "run", server)
    assert api_cli.main([]) == 0
    app = server.call_args.args[0]
    async with app.router.lifespan_context(app):
        assert events == ["ready"]
        engine.dispose.assert_not_awaited()
    assert events == ["ready", "close"]


async def test_api_worker_stops_before_the_database_closes(tmp_path, monkeypatch, pg_boundary):
    import uvicorn

    from harness import worker as worker_cli
    from harness.api import __main__ as api_cli

    repos, engine, _, events = pg_boundary
    monkeypatch.setattr(PgRepositories, "from_dsn", Mock(return_value=repos))
    monkeypatch.setattr(HarnessConfig, "from_env", Mock(return_value=system_config(tmp_path, database_dsn=DSN)))
    monkeypatch.setattr(worker_cli, "_model", Mock(return_value=ScriptedModelAdapter([])))
    started = asyncio.Event()

    async def run_forever(*, stop):
        events.append("worker_started")
        started.set()
        await stop.wait()
        engine.dispose.assert_not_awaited()
        events.append("worker_stopped")

    monkeypatch.setattr(HarnessSystem, "worker", Mock(return_value=SimpleNamespace(run_forever=run_forever)))
    server = Mock()
    monkeypatch.setattr(uvicorn, "run", server)
    assert api_cli.main(["--with-worker", "--scripted", "unused.json"]) == 0
    app = server.call_args.args[0]
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), timeout=1)
        assert events == ["ready", "worker_started"]
    assert events == ["ready", "worker_started", "worker_stopped", "close"]


def worker_args(*, submit=None):
    return argparse.Namespace(
        once=True, submit=submit, lanes=["foreground"], scripted="unused.json",
        model_config=None, print_result=False, trace=None,
    )


async def test_worker_once_with_no_task_closes_the_database(tmp_path, monkeypatch, pg_boundary, capsys):
    from harness import worker as worker_cli

    repos, engine, connection, events = pg_boundary
    engine.begin.return_value = connection
    empty = Mock()
    empty.mappings.return_value.first.return_value = None

    async def execute(statement):
        events.append("ready" if not events else "claim")
        return empty

    connection.execute.side_effect = execute
    monkeypatch.setattr(PgRepositories, "from_dsn", Mock(return_value=repos))
    monkeypatch.setattr(HarnessConfig, "from_env", Mock(return_value=system_config(tmp_path, database_dsn=DSN)))
    monkeypatch.setattr(worker_cli, "_model", Mock(return_value=ScriptedModelAdapter([])))
    assert await worker_cli._main(worker_args()) == 1
    assert json.loads(capsys.readouterr().out) == {"claimed": None}
    assert events == ["ready", "claim", "close"]
    engine.dispose.assert_awaited_once()


async def test_worker_submit_read_failure_still_closes_the_database(tmp_path, monkeypatch, pg_boundary):
    from harness import worker as worker_cli

    repos, engine, _, events = pg_boundary
    monkeypatch.setattr(PgRepositories, "from_dsn", Mock(return_value=repos))
    monkeypatch.setattr(HarnessConfig, "from_env", Mock(return_value=system_config(tmp_path, database_dsn=DSN)))
    monkeypatch.setattr(worker_cli, "_model", Mock(return_value=ScriptedModelAdapter([])))
    with pytest.raises(FileNotFoundError):
        await worker_cli._main(worker_args(submit=str(tmp_path / "missing-task.json")))
    assert events == ["ready", "close"]
    engine.begin.assert_not_called()
    engine.dispose.assert_awaited_once()
