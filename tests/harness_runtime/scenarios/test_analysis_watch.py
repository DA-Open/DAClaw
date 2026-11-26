"""analysis_watch end to end: admission, then recompute in the context builder."""

import json
from pathlib import Path

import pytest

from harness.contracts import Event, EventType, Subscription, TaskStatus, utcnow
from harness.storage.memory import InMemoryRepositories
from harness.tasks.proactive_controller import ProactiveController
from harness.tools.connectors.data import file_digest

from ..analysis_kit import GOAL, Kit
from ..fakes import ACTOR, TTL
from ..fixtures.analysis.build import write


@pytest.fixture(scope="session")
def datasets(tmp_path_factory):
    """Build v1 and v2 once per session; each test copies the file it needs."""
    root = tmp_path_factory.mktemp("analysis-data")
    v1 = write(root / "v1", "v1")
    v2 = write(root / "v2", "v2")
    return v1, v2


def place(tmp_path, source, name="data"):
    import shutil

    target = tmp_path / name / "orders.duckdb"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(source, target)
    return target

pytestmark = pytest.mark.asyncio

REVENUE_WATCH = {"query_index": 0, "column": "revenue", "min_relative_change": 0.05}
COUNT_WATCH = {"query_index": 1, "column": "orders", "min_relative_change": 0.05}


async def setup(tmp_path, *, watch=REVENUE_WATCH, datasets=()):
    repos = InMemoryRepositories()
    kit = Kit(tmp_path, repos)
    v1 = place(tmp_path, datasets[0])
    task, outcome, committed = await kit.run_analysis(v1)
    assert committed.status is TaskStatus.SUCCEEDED
    record = (await repos.memories.list("s1", now=kit.clock()))[0]
    controller = ProactiveController(repos, kit.service(), kit.policy, memories=kit.memory, clock=kit.clock)
    sub = await controller.create_subscription(Subscription(
        scope_id="s1", owner_id="u1", goal="盯住三月销售额", subject_ref=str(v1), resources=[str(v1)],
        capability_ref="analysis_watch@1", event_filter={"kind": "source_changed"},
        baseline_ref=f"memory:{record.id}@{record.version}", watch=watch,
    ))
    return kit, controller, sub, v1


def changed(path: Path, version: str):
    return Event(
        scope_id="s1", type=EventType.EXTERNAL, source="data_watch",
        source_event_id=f"{path}@{file_digest(path)}", subject_ref=str(path),
        payload={"kind": "source_changed", "path": str(path), "sha256": file_digest(path),
                 "size_bytes": path.stat().st_size},
        occurred_at=utcnow(), received_at=utcnow(),
    )


async def build_context(kit, ref):
    task = ref
    lease = await kit.repos.tasks.claim("w1", [task.run_lane], TTL, kit.clock())
    assert lease is not None
    current = await kit.repos.tasks.get(ACTOR.scope_id, task.task_id)
    profile = kit.agents.resolve_ref(current.agent_profile_ref)
    await kit.builder.build(current, profile, lease)
    return current


async def recompute_of(kit, task):
    for artifact in await kit.repos.artifacts.list_for_task(task.scope_id, task.id):
        if artifact.name == "recompute":
            content = await kit.store.read(artifact.id, ACTOR)
            return artifact.id, json.loads(content.content)
    return None, None


async def test_recompute_in_builder(tmp_path, datasets):
    kit, controller, sub, v1 = await setup(tmp_path, datasets=datasets)
    kit.close_sessions()
    v2 = place(tmp_path, datasets[1])
    created = await controller.on_event(changed(v2, "v2"))
    assert len(created) == 1
    task = await build_context(kit, created[0])
    ref, result = await recompute_of(kit, task)
    assert ref is not None
    assert result["error"] == ""
    threshold = result["threshold"]
    assert threshold["old"] == 219912.81 and threshold["new"] == 232944.31
    assert threshold["delta"] == pytest.approx(13031.50, abs=1e-6)
    assert threshold["relative_change"] == pytest.approx(0.0593, abs=5e-4)
    assert threshold["exceeds_threshold"] is True
    kinds = [e.type for e in await kit.repos.events.list("s1", task_id=task.id, limit=100)]
    assert EventType.RECOMPUTE_RECORDED in kinds
    # The new evidence is a normal query artifact.
    query_refs = result["queries"][0]["query_ref"]
    assert query_refs.startswith("query:")
    stored = await kit.store.read(query_refs.removeprefix("query:"), ACTOR)
    assert json.loads(stored.content)["row_count"] == 1


async def test_count_watch_unchanged(tmp_path, datasets):
    kit, controller, sub, v1 = await setup(tmp_path, watch=COUNT_WATCH, datasets=datasets)
    kit.close_sessions()
    v2 = place(tmp_path, datasets[1])
    created = await controller.on_event(changed(v2, "v2"))
    task = await build_context(kit, created[0])
    _ref, result = await recompute_of(kit, task)
    assert result["threshold"]["old"] == 169 and result["threshold"]["new"] == 169
    assert result["threshold"]["exceeds_threshold"] is False


async def test_same_sha_skips(tmp_path, datasets):
    kit, controller, sub, v1 = await setup(tmp_path, datasets=datasets)
    assert await controller.on_event(changed(v1, "v1")) == []
    decisions = await kit.repos.subscriptions.decisions("s1", sub.id)
    assert decisions[-1].decision == "skip" and decisions[-1].reason == "unchanged_source"


async def test_recompute_idempotent_across_attempts(tmp_path, datasets):
    kit, controller, sub, v1 = await setup(tmp_path, datasets=datasets)
    kit.close_sessions()
    v2 = place(tmp_path, datasets[1])
    created = await controller.on_event(changed(v2, "v2"))
    task = await build_context(kit, created[0])
    first, _ = await recompute_of(kit, task)
    again = await kit.recomputer.ensure(task)
    assert again == first


async def test_recompute_error_recorded(tmp_path, datasets):
    kit, controller, sub, v1 = await setup(tmp_path, datasets=datasets)
    kit.close_sessions()
    v2 = place(tmp_path, datasets[1])
    # The new version is missing the column the baseline sums: the event carries
    # its real digest, so recompute reaches the SQL and fails there.
    import duckdb

    con = duckdb.connect(str(v2))
    con.execute("ALTER TABLE order_items DROP COLUMN unit_price")
    con.close()
    created = await controller.on_event(changed(v2, "v2"))
    task = await build_context(kit, created[0])
    _ref, result = await recompute_of(kit, task)
    assert result["error"] and "unit_price" in result["error"]


async def test_file_changed_again_before_recompute(tmp_path, datasets):
    kit, controller, sub, v1 = await setup(tmp_path, datasets=datasets)
    kit.close_sessions()
    v2 = place(tmp_path, datasets[1])
    created = await controller.on_event(changed(v2, "v2"))
    kit.close_sessions()
    write(tmp_path / "data", "v1")  # the file is replaced again after admission
    task = await build_context(kit, created[0])
    _ref, result = await recompute_of(kit, task)
    assert "changed again" in result["error"]
