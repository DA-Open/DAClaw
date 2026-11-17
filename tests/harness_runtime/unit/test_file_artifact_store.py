"""FileArtifactStore (results/artifacts.py): content on disk, row in the repository."""

import hashlib

import pytest

from harness.contracts import Actor, ArtifactInput, NotFound
from harness.results import ArtifactIntegrityError, FileArtifactStore
from harness.storage.memory import InMemoryRepositories

pytestmark = pytest.mark.asyncio

ME = Actor(actor_id="u1", scope_id="s1")
OTHER = Actor(actor_id="u2", scope_id="s2")


def store(tmp_path):
    repos = InMemoryRepositories()
    return FileArtifactStore(tmp_path / "artifacts", repos.artifacts), repos


def item(content=b"# Draft\nhello", **kw):
    data = dict(scope_id="s1", task_id="task_1", name="draft", media_type="text/markdown", content=content)
    data.update(kw)
    return ArtifactInput(**data)


async def test_put_writes_the_file_and_registers_the_row(tmp_path):
    s, repos = store(tmp_path)
    ref = await s.put(item(source_refs=["action:act_1"]))
    digest = hashlib.sha256(b"# Draft\nhello").hexdigest()
    on_disk = tmp_path / "artifacts" / "s1" / digest[:2] / digest
    assert on_disk.read_bytes() == b"# Draft\nhello"
    row = await repos.artifacts.get("s1", ref.id)
    assert row.content_hash == f"sha256:{digest}" and row.size_bytes == 13
    assert row.uri.startswith("file:") and row.source_refs == ["action:act_1"]
    assert [a.id for a in await repos.artifacts.list_for_task("s1", "task_1")] == [ref.id]


async def test_read_back_whole_and_a_range(tmp_path):
    s, _ = store(tmp_path)
    ref = await s.put(item())
    whole = await s.read(ref, ME)
    assert whole.content == b"# Draft\nhello" and whole.complete
    part = await s.read(ref.id, ME, offset=2, length=5)
    assert part.content == b"Draft" and not part.complete and part.offset == 2


async def test_other_scope_cannot_read(tmp_path):
    s, _ = store(tmp_path)
    ref = await s.put(item())
    with pytest.raises(NotFound):
        await s.read(ref, OTHER)


async def test_same_content_is_one_file_but_two_artifacts(tmp_path):
    s, repos = store(tmp_path)
    a = await s.put(item(name="a"))
    b = await s.put(item(name="b"))
    assert a.id != b.id and a.content_hash == b.content_hash
    files = [p for p in (tmp_path / "artifacts").rglob("*") if p.is_file()]
    assert len(files) == 1
    assert {x.name for x in await repos.artifacts.list_for_task("s1", "task_1")} == {"a", "b"}


async def test_changed_file_is_reported_not_returned(tmp_path):
    s, _ = store(tmp_path)
    ref = await s.put(item())
    path = next(p for p in (tmp_path / "artifacts").rglob("*") if p.is_file())
    path.write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError):
        await s.read(ref, ME)


async def test_missing_file_is_not_found(tmp_path):
    s, _ = store(tmp_path)
    ref = await s.put(item())
    next(p for p in (tmp_path / "artifacts").rglob("*") if p.is_file()).unlink()
    with pytest.raises(NotFound):
        await s.read(ref, ME)


async def test_scope_id_cannot_escape_the_root(tmp_path):
    s, _ = store(tmp_path)
    await s.put(item(scope_id="../../etc"))
    root = (tmp_path / "artifacts").resolve()
    for p in (tmp_path / "artifacts").rglob("*"):
        assert root in p.resolve().parents or p.resolve() == root
    assert not (tmp_path / "etc").exists()


async def test_no_temp_files_left_behind(tmp_path):
    s, _ = store(tmp_path)
    for i in range(3):
        await s.put(item(content=f"v{i}".encode()))
    assert not [p for p in (tmp_path / "artifacts").rglob(".tmp-*")]


async def test_works_with_the_gateway_connectors(tmp_path):
    """artifacts.write followed by artifacts.read through the real store."""
    from harness.contracts import AgentRole, ToolCall, ToolStatus
    from harness.tools import ToolGateway, UserGrant, WhitelistPolicy
    from harness.tools.connectors import p0_connectors

    from ..fakes import ACTOR, MAIN_PROFILE, Clock, claimed_task

    repos = InMemoryRepositories()
    fs = FileArtifactStore(tmp_path / "artifacts", repos.artifacts)
    policy = WhitelistPolicy(
        roles={MAIN_PROFILE.tool_policy_ref: {"artifacts.read", "artifacts.write"}},
        users={ACTOR.scope_id: {AgentRole.MAIN: UserGrant()}},
    )
    gw = ToolGateway(repos, fs, policy, p0_connectors(fs), profiles={"main@1": MAIN_PROFILE.tool_policy_ref}, clock=Clock())
    task, execution = await claimed_task(repos)
    wrote = await gw.invoke(execution, ToolCall(tool="artifacts.write", args={"name": "draft", "content": "# Draft\nok"}, logical_key="step-1.0"))
    assert wrote.status is ToolStatus.SUCCEEDED
    read = await gw.invoke(execution, ToolCall(tool="artifacts.read", args={"artifact_id": wrote.artifact_refs[0]}, logical_key="step-2.0"))
    assert read.status is ToolStatus.SUCCEEDED and "# Draft" in read.summary
    work = await repos.artifacts.list_for_task("s1", task.id)
    assert [a.name for a in work] == ["draft"]
    (row,) = work
    assert (await fs.read(row.id, ACTOR)).content == b"# Draft\nok"
