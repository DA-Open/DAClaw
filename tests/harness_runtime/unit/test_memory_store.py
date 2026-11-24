"""C1 storage contract, on the existing shared repository fixture."""
from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from harness.contracts import InvalidTransition, MemoryRecord, NotFound
from ..fakes import T0
from .test_repositories import repos  # noqa: F401

pytestmark = pytest.mark.asyncio


def record(**kw):
    return MemoryRecord(**dict(dict(scope_id="s1", kind="preference", content_ref="art_body",
        source_ref="user:u1", summary="周组会 汇报", tags=["meeting"], created_at=T0, updated_at=T0), **kw))


async def test_put_get_scope_isolated(repos):
    original = record()
    stored = await repos.memories.put(original)
    assert stored == original
    original.tags.append("changed")
    assert (await repos.memories.get("s1", stored.id)).tags == ["meeting"]
    stored.tags.append("also changed")
    assert (await repos.memories.get("s1", stored.id)).tags == ["meeting"]
    for scope, mid in [("s2", stored.id), ("s1", "missing")]:
        with pytest.raises(NotFound):
            await repos.memories.get(scope, mid)


async def test_search_any_term_ranked(repos):
    one = await repos.memories.put(record(updated_at=T0 + timedelta(seconds=3)))
    two = await repos.memories.put(record(summary="组会 课程"))
    tie = await repos.memories.put(record(summary="组会", tags=[], updated_at=T0 + timedelta(seconds=1)))
    assert [r.id for r in await repos.memories.search("s1", "组会 课程", now=T0)] == [two.id, one.id, tie.id]
    assert [r.id for r in await repos.memories.search("s1", "MEETING", now=T0, limit=1)] == [one.id]
    assert await repos.memories.search("s1", "meet", now=T0) == []
    assert await repos.memories.search("s2", "组会", now=T0) == []
    with pytest.raises(ValueError):
        await repos.memories.search("s1", " \t\n", now=T0)
    literal = await repos.memories.put(record(summary=r"100% x_y c:\docs", tags=[]))
    for term in ["%", "_", "\\"]:
        assert [r.id for r in await repos.memories.search("s1", term, now=T0)] == [literal.id]


async def test_search_filters_kinds_ended_expired(repos):
    live = await repos.memories.put(record(expires_at=T0 + timedelta(seconds=1)))
    await repos.memories.put(record(expires_at=T0))
    await repos.memories.put(record(ended_at=T0, end_reason="retracted"))
    await repos.memories.put(record(kind="fact"))
    assert [r.id for r in await repos.memories.search("s1", "组会", now=T0, kinds=["preference"])] == [live.id]
    assert await repos.memories.search("s1", "组会", now=T0, kinds=[]) == []


async def test_current_by_subject_ref(repos):
    first = await repos.memories.put(record(subject_ref="x"))
    second = await repos.memories.put(record(subject_ref="y", updated_at=T0 + timedelta(seconds=1)))
    await repos.memories.put(record(subject_ref="x", expires_at=T0))
    await repos.memories.put(record(scope_id="s2", subject_ref="x"))
    assert [r.id for r in await repos.memories.current("s1", now=T0, kinds=["preference"], subject_ref="x")] == [first.id]
    assert [r.id for r in await repos.memories.current("s1", now=T0, kinds=["preference"], limit=1)] == [second.id]
    assert await repos.memories.current("s1", now=T0, kinds=[]) == []


async def test_list_filters(repos):
    live = await repos.memories.put(record(status="confirmed", source_ref="user:live"))
    rejected = await repos.memories.put(record(ended_at=T0, end_reason="rejected", source_ref="model:rejected"))
    expired = await repos.memories.put(record(expires_at=T0, updated_at=T0 + timedelta(seconds=1)))
    await repos.memories.put(record(scope_id="s2"))
    assert await repos.memories.list("s1", now=T0) == [live]
    assert (await repos.memories.list("s1", now=T0, include_ended=True, limit=1)) == [expired]
    assert await repos.memories.list("s1", now=T0, include_ended=True, statuses=["confirmed"]) == [live]
    assert await repos.memories.list("s1", now=T0, include_ended=True, end_reasons=["rejected"]) == [rejected]
    assert await repos.memories.list("s1", now=T0, include_ended=True, source_ref="model:rejected") == [rejected]
    assert await repos.memories.list("s1", now=T0, kinds=["fact"]) == []
    assert await repos.memories.list("s1", now=T0, statuses=[]) == []
    assert await repos.memories.list("s1", now=T0, include_ended=True, end_reasons=[]) == []


async def test_supersede_keeps_history(repos):
    old = await repos.memories.put(record())
    new = await repos.memories.supersede("s1", old.id, record(summary="新摘要"), T0)
    assert new.version == 2 and new.supersedes_id == old.id
    assert await repos.memories.current("s1", now=T0, kinds=["preference"]) == [new]
    ended = await repos.memories.get("s1", old.id)
    assert ended.ended_at == T0 and ended.end_reason == "superseded"
    assert ended.content_ref == old.content_ref and ended.summary == old.summary
    forgotten = await repos.memories.retract("s1", new.id, "retracted", T0)
    assert forgotten.model_dump(exclude={"ended_at", "end_reason"}) == new.model_dump(exclude={"ended_at", "end_reason"})
    assert await repos.memories.current("s1", now=T0, kinds=["preference"]) == []


@pytest.mark.parametrize("first", ["supersede", "retract", "expired", "foreign", "missing"])
async def test_supersede_or_retract_twice_conflicts(repos, first):
    old = await repos.memories.put(record(expires_at=T0 if first == "expired" else None))
    scope, mid = "s1", old.id
    if first == "supersede":
        await repos.memories.supersede(scope, mid, record(), T0)
    elif first == "retract":
        await repos.memories.retract(scope, mid, "rejected", T0)
    elif first == "foreign":
        scope = "s2"
    elif first == "missing":
        mid = "missing"
    with pytest.raises(InvalidTransition):
        await repos.memories.supersede(scope, mid, record(), T0)
    with pytest.raises(InvalidTransition):
        await repos.memories.retract(scope, mid, "retracted", T0)


async def test_invalid_replacement_is_atomic(repos):
    old = await repos.memories.put(record())
    for replacement in [record(scope_id="s2"), record(id=old.id), record(ended_at=T0, end_reason="rejected")]:
        with pytest.raises(InvalidTransition):
            await repos.memories.supersede("s1", old.id, replacement, T0)
        assert await repos.memories.get("s1", old.id) == old


async def test_insert_conflicts_keep_history(repos):
    old = await repos.memories.put(record())
    other = await repos.memories.put(record())
    with pytest.raises((InvalidTransition, IntegrityError)):
        await repos.memories.put(old)
    with pytest.raises((InvalidTransition, IntegrityError)):
        await repos.memories.supersede("s1", old.id, other, T0)
    assert await repos.memories.get("s1", old.id) == old
    await repos.memories.supersede("s1", old.id, record(), T0)
    with pytest.raises((InvalidTransition, IntegrityError)):
        await repos.memories.put(record(supersedes_id=old.id))
    assert len(await repos.memories.list("s1", now=T0, include_ended=True)) == 3


async def test_task_key_conflict_rolls_back_supersede(repos):
    old = await repos.memories.put(record(source_ref="task:x"))
    with pytest.raises((InvalidTransition, IntegrityError)):
        await repos.memories.supersede("s1", old.id, record(source_ref="task:x"), T0)
    assert await repos.memories.get("s1", old.id) == old
    assert await repos.memories.list("s1", now=T0, include_ended=True) == [old]
