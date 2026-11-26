"""The ANALYSIS baseline: written after commit, from verified artifacts only."""

import json

import pytest
import pytest_asyncio

from harness.contracts import Event, EventType, NotFound, TaskStatus
from harness.storage.memory import InMemoryRepositories
from harness.tasks.proactive_controller import ProactiveController

from ..analysis_kit import GOAL, Kit, _refs
from ..fakes import ACTOR
from ..fixtures.analysis.build import write

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(params=["memory", "postgres"])
async def repos(request):
    if request.param == "memory":
        yield InMemoryRepositories()
        return
    from harness.storage.postgres import PgRepositories

    from ..pg import clean

    store = PgRepositories.from_dsn(request.getfixturevalue("pg_dsn"))
    await clean(store.engine)
    try:
        yield store
    finally:
        await store.close()


async def _dataset(tmp_path):
    return write(tmp_path / "data", "v1")


async def test_baseline_written_after_commit(repos, tmp_path):
    kit = Kit(tmp_path, repos)
    dataset = await _dataset(tmp_path)
    task, outcome, committed = await kit.run_analysis(dataset)
    assert committed.status is TaskStatus.SUCCEEDED
    records = await repos.memories.list("s1", now=kit.clock())
    assert len(records) == 1 and records[0].kind.value == "analysis"
    _record, text = await kit.memory.read("s1", "harness", records[0].id)
    body = json.loads(text)
    assert body["queries"][0]["sql"].lstrip().startswith("WITH dedup")
    assert body["queries"][0]["values"] == [["219912.81"]]
    assert body["queries"][1]["values"] == [["169"]]
    assert body["sources"][0]["sha256"] and body["sources"][0]["tables"] == {
        "order_items": "orders", "orders": "orders_orders"}


async def test_baseline_idempotent_and_failure_tolerant(repos, tmp_path):
    kit = Kit(tmp_path, repos)
    dataset = await _dataset(tmp_path)
    task, outcome, committed = await kit.run_analysis(dataset)
    await kit.baseline(task, outcome)
    assert len(await repos.memories.list("s1", now=kit.clock())) == 1

    class Boom:
        async def remember(self, *args, **kwargs):
            raise ValueError("nope")

    kit.baseline._memories = Boom()
    await kit.baseline(task, outcome)  # a failing memory service must not break commit


async def test_baseline_adds_no_model_or_query_calls(repos, tmp_path):
    kit = Kit(tmp_path, repos)
    dataset = await _dataset(tmp_path)
    task, outcome, committed = await kit.run_analysis(dataset)
    before = len(await repos.actions.list_for_task(task.id))
    await kit.baseline(task, outcome)
    assert len(await repos.actions.list_for_task(task.id)) == before


async def test_no_baseline_without_evidence(repos, tmp_path):
    from harness.contracts import CheckResult, VerificationResult, VerificationStatus

    kit = Kit(tmp_path, repos)
    dataset = await _dataset(tmp_path)
    task, outcome, committed = await kit.run_analysis(dataset)
    assert committed.status is TaskStatus.SUCCEEDED
    assert len(await repos.memories.list("s1", now=kit.clock())) == 1

    # Not an analysis task.
    await kit.baseline(task.model_copy(update={"capability_ref": "document_draft@1"}), outcome)
    # Not a successful task.
    await kit.baseline(task, outcome.model_copy(update={"status": TaskStatus.FAILED}))
    # A verification whose query_evidence check cites nothing.
    empty = await repos.verifications.add(VerificationResult(
        task_id=task.id, result_ref=outcome.result_ref, checker_version="verifier@1",
        status=VerificationStatus.PASSED,
        checks=[CheckResult(id="query_evidence", status=VerificationStatus.PASSED)],
    ))
    await kit.baseline(task, outcome.model_copy(update={"verification_ref": empty.id}))
    assert len(await repos.memories.list("s1", now=kit.clock())) == 1


async def test_baseline_on_inbox_branch(repos, tmp_path):
    kit = Kit(tmp_path, repos)
    dataset = await _dataset(tmp_path)
    calls = []

    class Hook:
        async def __call__(self, task, outcome):
            calls.append(task.id)

    from harness.contracts import Subscription
    from harness.proactive_types import InboxItem

    subscription = await repos.subscriptions.create(Subscription(
        scope_id="s1", owner_id="u1", goal="watch the file", subject_ref=str(dataset),
        resources=[str(dataset)], capability_ref="data_card@1",
        event_filter={"source": "data_watch"},
    ))

    class Inbox:
        async def prepare(self, task, outcome):
            return InboxItem(
                scope_id=task.scope_id, subscription_id=subscription.id, task_id=task.id,
                result_ref=outcome.result_ref or "", subject_ref=str(dataset), data_version="v1",
                topic="t", mode="digest", score=5, dedup_key="d",
                valid_until=kit.clock().replace(year=2099), created_at=kit.clock(),
            )

    service = kit.service(hooks=[Hook()])
    service._delivery = Inbox()
    task, outcome, committed = await kit.run_analysis(dataset, service=service)
    assert calls == [task.id]


async def test_analysis_subscription_validation(repos, tmp_path):
    kit = Kit(tmp_path, repos)
    dataset = await _dataset(tmp_path)
    task, outcome, committed = await kit.run_analysis(dataset)
    record = (await repos.memories.list("s1", now=kit.clock()))[0]
    controller = ProactiveController(repos, kit.service(), kit.policy, memories=kit.memory, clock=kit.clock)

    from harness.contracts import Subscription

    def sub(**kw):
        data = dict(
            scope_id="s1", owner_id="u1", goal=GOAL, subject_ref=str(dataset), resources=[str(dataset)],
            capability_ref="analysis_watch@1", event_filter={"kind": "source_changed"},
            baseline_ref=f"memory:{record.id}@{record.version}",
            watch={"query_index": 0, "column": "revenue", "min_relative_change": 0.05},
        )
        data.update(kw)
        return Subscription(**data)

    created = await controller.create_subscription(sub())
    assert created.baseline_ref == f"memory:{record.id}@{record.version}"

    with pytest.raises((ValueError, NotFound)):
        await controller.create_subscription(sub(baseline_ref="memory:missing@1"))
    with pytest.raises(ValueError):
        await controller.create_subscription(sub(watch={"query_index": 0, "column": "nope", "min_relative_change": 0.05}))
    with pytest.raises(ValueError):
        await controller.create_subscription(sub(watch={"query_index": 5, "column": "revenue", "min_relative_change": 0.05}))
    with pytest.raises(PermissionError):
        await controller.create_subscription(sub(resources=["/etc/passwd"], subject_ref="/etc/passwd"))


async def test_source_change_admits_without_io(repos, tmp_path):
    kit = Kit(tmp_path, repos)
    dataset = await _dataset(tmp_path)
    task, outcome, committed = await kit.run_analysis(dataset)
    record = (await repos.memories.list("s1", now=kit.clock()))[0]
    controller = ProactiveController(repos, kit.service(), kit.policy, memories=kit.memory, clock=kit.clock)
    from harness.contracts import Subscription

    subscription = await controller.create_subscription(Subscription(
        scope_id="s1", owner_id="u1", goal=GOAL, subject_ref=str(dataset), resources=[str(dataset)],
        capability_ref="analysis_watch@1", event_filter={"kind": "source_changed"},
        baseline_ref=f"memory:{record.id}@{record.version}",
        watch={"query_index": 0, "column": "revenue", "min_relative_change": 0.05},
    ))
    before = len(await repos.actions.list_for_task(task.id))
    event = Event(
        scope_id="s1", type=EventType.EXTERNAL, source="data_watch",
        source_event_id=f"{dataset}@v2", subject_ref=str(dataset),
        payload={"kind": "source_changed", "path": str(dataset), "sha256": "b" * 64, "size_bytes": 10},
        occurred_at=kit.clock(), received_at=kit.clock(),
    )
    created = await controller.on_event(event)
    assert len(created) == 1
    decisions = await repos.subscriptions.decisions("s1", subscription.id)
    assert decisions[-1].decision == "admit" and decisions[-1].reason == "source_changed"
    assert "recompute" not in json.dumps(decisions[-1].input_snapshot)
    assert len(await repos.actions.list_for_task(task.id)) == before


async def test_admission_order_and_ended_baseline(repos, tmp_path):
    """A revoked permission or a dead baseline outranks "nothing changed"."""
    from harness.contracts import AdmissionDecision, Subscription, Usage
    from harness.personal.analysis_admission import decide_analysis

    kit = Kit(tmp_path, repos)
    event = Event(
        scope_id="s1", type=EventType.EXTERNAL, source="data_watch", source_event_id="e1",
        subject_ref="/tmp/data.duckdb",
        payload={"kind": "source_changed", "path": "/tmp/data.duckdb", "sha256": "a" * 64, "size_bytes": 1},
        occurred_at=kit.clock(), received_at=kit.clock(),
    )
    sub = Subscription(scope_id="s1", owner_id="u1", goal=GOAL, subject_ref="/tmp/data.duckdb",
                       resources=["/tmp/data.duckdb"], capability_ref="analysis_watch@1",
                       event_filter={"kind": "source_changed"},
                       baseline_ref="memory:mem_1@1",
                       watch={"query_index": 0, "column": "revenue", "min_relative_change": 0.05})
    baseline = {"sources": [{"path": "/tmp/data.duckdb", "sha256": "a" * 64}]}

    revoked = decide_analysis(sub, event, baseline, [], [], Usage(), kit.clock(), 2, allowed=False)
    assert revoked.decision == "skip" and revoked.reason == "resource_permission_revoked"
    ended = decide_analysis(sub, event, None, [], [], Usage(), kit.clock(), 2, allowed=True)
    assert ended.decision == "skip" and ended.reason == "baseline_ended"
    unchanged = decide_analysis(sub, event, baseline, [], [], Usage(), kit.clock(), 2, allowed=True)
    assert unchanged.reason == "unchanged_source"
    assert unchanged.input_snapshot["daily_budget"]["tool_calls"] == sub.daily_budget.tool_calls
