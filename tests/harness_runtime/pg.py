"""A PostgreSQL database for the harness tests.

Run with ``python -m pytest tests/harness_runtime -m integration -n0`` (the default
run skips them). ``DARAOS_HARNESS_TEST_DSN`` points the tests at a database you already run
(it is wiped: every table the migrations create is truncated). Without it a
throwaway ``postgres:16`` container is started through the docker CLI, once
per test session (per xdist worker), and removed at the end. With neither,
or without the harness extra, PostgreSQL tests are skipped.

Each test gets its own engine (pytest-asyncio gives each test its own event
loop, and asyncpg connections cannot cross loops) and clean tables.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import shutil
import socket
import subprocess
import time
import uuid

import pytest

IMAGE = os.environ.get("DARAOS_HARNESS_TEST_PG_IMAGE", "postgres:16")
TABLES = ("approvals", "memories", "delivery_counters", "inbox_items", "subscription_events", "subscriptions", "usage_reservations", "verifications", "artifacts", "actions", "events", "attempts", "tasks")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class _Container:
    def __init__(self) -> None:
        self.name = f"harness-test-pg-{uuid.uuid4().hex[:8]}"
        self.port = _free_port()
        self.password = uuid.uuid4().hex

    @property
    def dsn(self) -> str:
        return f"postgresql://postgres:{self.password}@127.0.0.1:{self.port}/postgres"

    def start(self) -> None:
        subprocess.run(
            [
                "docker", "run", "-d", "--rm", "--name", self.name,
                "-e", f"POSTGRES_PASSWORD={self.password}",
                "-p", f"127.0.0.1:{self.port}:5432",
                IMAGE,
                # Tests do not need durability; this makes them faster.
                "-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off",
            ],
            check=True, capture_output=True, timeout=120,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = subprocess.run(
                ["docker", "exec", self.name, "pg_isready", "-U", "postgres", "-h", "127.0.0.1"],
                capture_output=True,
            )
            if ready.returncode == 0:
                return
            time.sleep(0.5)
        self.stop()
        raise RuntimeError("postgres container did not become ready in 60 s")

    def stop(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)


def _missing_modules() -> list[str]:
    import importlib.util

    return [m for m in ("sqlalchemy", "asyncpg", "alembic", "greenlet") if importlib.util.find_spec(m) is None]


@pytest.fixture(scope="session")
def pg_dsn():
    """DSN of a migrated, empty database; skips when none is available.

    The PostgreSQL tests carry the ``integration`` mark, so the repo's default
    run (``-m 'not integration'``) does not reach this fixture at all. Run
    them with ``-m integration -n0`` (or ``-o addopts=""``) and the harness extra
    installed (``pip install -e '.[harness]'``).
    """
    missing = _missing_modules()
    if missing:
        pytest.skip(f"PostgreSQL tests need the harness extra (missing: {', '.join(missing)})")
    dsn = os.environ.get("DARAOS_HARNESS_TEST_DSN")
    container = None
    if not dsn:
        if not _docker_ok():
            pytest.skip("no PostgreSQL: set DARAOS_HARNESS_TEST_DSN or make docker available")
        container = _Container()
    try:
        if container is not None:
            try:
                container.start()
            except (subprocess.SubprocessError, RuntimeError) as exc:
                pytest.skip(f"could not start {IMAGE}: {exc}")
            dsn = container.dsn
        from harness.storage.migrate import upgrade

        # The fixture may be first requested from inside a running event loop
        # (an async test), so the migration gets a thread and loop of its own.
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            pool.submit(asyncio.run, upgrade(dsn)).result(timeout=120)
        yield dsn
    finally:
        if container is not None:
            container.stop()


async def clean(engine) -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
