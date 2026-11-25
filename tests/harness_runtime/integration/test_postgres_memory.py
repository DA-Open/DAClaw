"""C1 races use independent pools, and migration 0004 round-trips."""
import asyncio

import pytest
from sqlalchemy import inspect, text

from harness.contracts import InvalidTransition
from harness.personal.memory import MemoryService
from harness.results import FileArtifactStore
from harness.storage.migrate import heads, run
from ..fakes import Clock, T0
from ..unit.test_memory_store import record
from ..unit.test_memory_service import remember
from .test_postgres import pools  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_concurrent_supersede_once(pools):
    old = await pools[0].memories.put(record())
    results = await asyncio.gather(*(pools[i].memories.supersede("s1", old.id, record(), T0)
                                    for i in range(2)), return_exceptions=True)
    assert sum(isinstance(r, InvalidTransition) for r in results) == 1
    rows = await pools[0].memories.list("s1", now=T0, include_ended=True)
    assert len(rows) == 2 and sum(r.ended_at is None for r in rows) == 1


async def test_concurrent_remember_same_task_once(pools, tmp_path):
    services = [MemoryService(p.memories, FileArtifactStore(tmp_path / "artifacts", p.artifacts), Clock()) for p in pools[:2]]
    results = await asyncio.gather(*(remember(s, source_ref="task:x") for s in services))
    assert results[0] == results[1]
    assert len(await pools[0].memories.list("s1", now=T0, include_ended=True)) == 1


async def test_migration_0004_round_trip(pools):
    # 0005 (approvals) is the head now; downgrading past 0004 still drops memories.
    assert heads() == ["0005"]
    engine = pools[0].engine
    async with engine.connect() as conn:
        columns = await conn.run_sync(lambda c: {v["name"]: str(v["type"]) for v in inspect(c).get_columns("memories")})
        indexes = dict((await conn.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename='memories'"))).all())
    assert columns["summary_lc"] == "TEXT" and columns["tags_lc"] == "ARRAY"
    assert any("UNIQUE" in i and "supersedes_id IS NOT NULL" in i for i in indexes.values())
    assert any("UNIQUE" in i and "source_ref" in i and "task:%" in i for i in indexes.values())
    await run(engine, "downgrade", "0003")
    async with engine.connect() as conn:
        assert "memories" not in await conn.run_sync(lambda c: inspect(c).get_table_names())
    await run(engine, "upgrade", "head")
    await pools[0].check_ready()
    old = await pools[0].memories.put(record())
    assert await pools[1].memories.get("s1", old.id) == old
