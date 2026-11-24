"""C1 trusted lifecycle and proposal deduplication on both repositories."""
import asyncio
from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from harness.contracts import InvalidTransition, NotFound
from harness.personal.memory import MemoryRevision, MemoryService
from harness.results import FileArtifactStore
from ..fakes import Clock, T0
from .test_repositories import repos  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest.fixture
def service(repos, tmp_path):
    return MemoryService(repos.memories, FileArtifactStore(tmp_path / "artifacts", repos.artifacts), Clock())


async def remember(service, **kw):
    args = dict(scope_id="s1", actor_id="u1", kind="preference", text="组会改期请提醒", status="confirmed",
                summary="组会提醒", tags=[], source_ref="user:u1", subject_ref=None, expires_at=None)
    args.update(kw)
    return await service.remember(**args)


async def propose(service, **kw):
    args = dict(scope_id="s1", kind="preference", text="组会改期，请提醒！", summary="组会提醒", tags=[], source_ref="model:1")
    args.update(kw)
    return await service.propose(**args)


async def test_remember_read_and_task_idempotency(service, repos):
    a, b = await asyncio.gather(remember(service, source_ref="task:x"), remember(service, source_ref="task:x"))
    assert a == b
    assert await service.read("s1", "u1", a.id) == (a, "组会改期请提醒")
    artifact = await repos.artifacts.get("s1", a.content_ref)
    assert artifact.name == f"memory/{a.id}" and artifact.task_id is None and artifact.media_type == "text/plain"
    await service.forget("s1", "u1", a.id)
    assert (await remember(service, source_ref="task:x")).id == a.id
    assert (await remember(service, source_ref="task:x", kind="fact")).id != a.id
    assert (await remember(service, source_ref="task:x", scope_id="s2")).id != a.id
    with pytest.raises(NotFound):
        await service.read("s2", "u2", a.id)


async def test_revise_confirm_reject_preserve_audit(service, repos):
    old = await remember(service, status="candidate", source_ref="task:proposal")
    new = await service.revise("s1", "u1", old.id, MemoryRevision(text="new", summary="new", tags=["pinned"]))
    assert new.status == "candidate" and new.version == 2
    assert (await service.read("s1", "u1", old.id))[1] == "组会改期请提醒"
    confirmed = await service.confirm("s1", "u1", new.id)
    assert confirmed.status == "confirmed" and confirmed.version == 3 and confirmed.content_ref == new.content_ref
    assert (await service.read("s1", "u1", confirmed.id))[1] == "new"
    with pytest.raises(InvalidTransition):
        await service.reject("s1", "u1", confirmed.id)
    forgotten = await service.forget("s1", "u1", confirmed.id)
    assert forgotten.end_reason == "retracted"
    assert (await repos.memories.get("s1", new.id)).end_reason == "superseded"
    # The original task remains idempotent after user-created versions.
    assert (await remember(service, source_ref="task:proposal")).id == old.id


async def test_propose_dedup_and_cap(service, repos, caplog):
    first = await propose(service)
    assert first.status == "candidate"
    assert await propose(service) is None
    assert await propose(service, summary="组会　提醒！", text="组会改期 请提醒") is None
    assert await propose(service, text="组会改期不要提醒") is not None
    await service.reject("s1", "u1", first.id)
    assert await propose(service) is None
    other_kind = await propose(service, kind="fact")
    assert other_kind is not None
    await service.confirm("s1", "u1", other_kind.id)
    assert await propose(service, kind="fact") is None
    fullwidth = await propose(service, summary="ＡＢＣ", text="ＦＯＯ，BAR")
    assert fullwidth is not None
    assert await propose(service, summary="abc", text="foo bar") is None
    for i in range(8):
        assert await propose(service, summary=f"unique {i}", text=str(i)) is not None
    assert await propose(service, summary="eleventh") is None
    assert "10" in caplog.text
    rows = await repos.memories.list("s1", now=T0, statuses=["candidate"])
    assert len(rows) == 10
    assert await propose(service, scope_id="s2") is not None


async def test_proposal_ignores_expired_and_retracted(service):
    expired = await propose(service)
    await service.revise("s1", "u1", expired.id, MemoryRevision(
        text="组会改期，请提醒！", summary="组会提醒", tags=[], expires_at=T0 + timedelta(seconds=1)))
    service.clock.advance(1)
    live = await propose(service)
    assert live is not None
    await service.forget("s1", "u1", live.id)
    assert await propose(service) is not None


async def test_revise_expiry_three_states(service):
    deadline = T0 + timedelta(days=7)
    old = await remember(service, expires_at=deadline)
    assert old.tags == ["temporary"]
    new = await service.revise("s1", "u1", old.id, MemoryRevision(text="v2", summary="v2", tags=[]))
    assert new.expires_at == deadline and "temporary" in new.tags
    stable = await service.revise("s1", "u1", new.id, MemoryRevision(text="v3", summary="v3", tags=["temporary"], expires_at=None))
    assert stable.expires_at is None and "temporary" not in stable.tags
    for bad in [T0, T0.replace(tzinfo=None)]:
        with pytest.raises(ValueError):
            await service.revise("s1", "u1", stable.id, MemoryRevision(text="bad", summary="bad", tags=[], expires_at=bad))
    renewed = await service.revise("s1", "u1", stable.id, MemoryRevision(text="v4", summary="v4", tags=[], expires_at=deadline))
    assert renewed.expires_at == deadline
    service.clock.advance(7 * 86400)
    for operation in [service.confirm, service.reject, service.forget]:
        with pytest.raises(InvalidTransition):
            await operation("s1", "u1", renewed.id)
    with pytest.raises(InvalidTransition):
        await service.revise("s1", "u1", renewed.id, MemoryRevision(text="x", summary="x", tags=[]))


async def test_remember_validates_expiry_and_tag_budget(service):
    for deadline in [T0, T0.replace(tzinfo=None)]:
        with pytest.raises(ValueError):
            await remember(service, expires_at=deadline)
    with pytest.raises(ValueError):
        await remember(service, expires_at=T0 + timedelta(days=1), tags=[str(i) for i in range(16)])
    fact = await remember(service, kind="fact", expires_at=T0 + timedelta(days=1))
    assert fact.tags == []


async def test_remember_propagates_unrelated_integrity_errors(service, monkeypatch):
    failure = IntegrityError("insert", {}, RuntimeError("non-task constraint"))

    async def fail(record):
        raise failure

    monkeypatch.setattr(service.memories, "put", fail)
    for source_ref in ["user:u1", "task:missing"]:
        with pytest.raises(IntegrityError) as caught:
            await remember(service, source_ref=source_ref)
        assert caught.value is failure
