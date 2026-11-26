"""Exercise decision verification through the scripted worker and its durable records."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import call, patch

import duckdb
import pytest

from harness.contracts import Event, EventType, new_id
from harness.tools.connectors.data import open_readonly, run_readonly
from ..unit.test_repositories import repos
from ..watch_kit import analysis_case, calendar_case

pytestmark = pytest.mark.asyncio


async def store_memory(case, kind, *, scope="s1"):
    record = await case.system.memory.remember(
        scope, "u1", kind, "内容", status="confirmed", summary="组会相关", tags=[],
        source_ref="user:u1", subject_ref=None, expires_at=None,
    )
    return f"memory:{record.id}@{record.version}"


@pytest.mark.parametrize("unresolved", [
    "foreign-memory", "outside-window", "unread-fact", "episode", "unknown-query",
    "wrong-version", "invalid-proposal", "other-baseline",
])
async def test_evidence_must_resolve(tmp_path, repos, unresolved):
    analysis = unresolved in {"unknown-query", "other-baseline"}
    case = await (analysis_case(tmp_path, repos) if analysis else calendar_case(tmp_path, repos))
    reads = []
    if unresolved == "outside-window":
        original = await repos.subscriptions.event("s1", case.record.input_snapshot["changes"][0][6:])
        outside = await repos.events.append(Event(
            scope_id="s1", type=EventType.EXTERNAL, source="mock_calendar", subject_ref=str(case.path),
            payload=original.payload, occurred_at=case.sub.created_at - timedelta(hours=1),
        ))
        reference = f"event:{outside.id}"
    elif unresolved == "unknown-query":
        reference = f"query:{new_id('art')}"
    elif unresolved == "other-baseline":
        reference = await store_memory(case, "analysis")
    elif unresolved == "foreign-memory":
        reference = await store_memory(case, "preference", scope="s2")
    elif unresolved == "wrong-version":
        preference = await store_memory(case, "preference")
        reads = [preference]
        reference = preference.rsplit("@", 1)[0] + "@2"
    elif unresolved == "episode":
        reference = await store_memory(case, "episode")
        reads = [reference]  # Even a successful explicit read cannot make an episode evidence.
    else:
        reference = await store_memory(case, "fact")

    def cite(decision, observation):
        if unresolved == "invalid-proposal":
            decision["memory_proposals"] = [{
                "kind": "fact", "summary": "组会地点", "text": "会议室", "evidence_refs": [reference],
            }]
        else:
            decision["evidence_refs"].append(reference)

    task = await case.run(cite, read_refs=reads)
    assert task.status == "failed", task
    check = (await case.checks())["decision_evidence"]
    assert check.status == "failed" and reference in check.detail
    assert await case.inbox() == []


async def test_evidence_via_memory_read_passes(tmp_path, repos):
    case = await calendar_case(tmp_path, repos)
    fact = await store_memory(case, "fact")
    preference = await store_memory(case, "preference")

    def cite(decision, observation):
        decision["evidence_refs"].extend([fact, preference])

    task = await case.run(cite, read_refs=[fact])
    assert task.status == "succeeded", task
    memory_reads = [a for a in await repos.actions.list_for_task(task.id) if a.tool == "memory.read"]
    assert len(memory_reads) == 1 and memory_reads[0].status == "succeeded"
    assert (await case.checks())["decision_evidence"].status == "passed"


async def test_refs_not_in_submit_evidence(tmp_path, repos):
    case = await calendar_case(tmp_path, repos)
    task = await case.run()
    assert task.status == "succeeded", task
    checks = await case.checks()
    assert "evidence_refs" not in checks
    stored = checks["decision_evidence"]
    assert stored.status == "passed"
    assert set(case.record.input_snapshot["changes"]) == set(stored.evidence_refs)


async def test_each_watch_checker_loads_its_task_actions(tmp_path, repos, monkeypatch):
    case = await analysis_case(tmp_path, repos)
    names = ("decision_schema", "decision_evidence", "recompute_replay")
    queries = {}

    def observe(checker):
        async def check(spec, ctx):
            assert not ctx.actions
            with patch.object(repos.actions, "list_for_task", wraps=repos.actions.list_for_task) as fetch:
                result = await checker(spec, ctx)
            queries[spec.id] = fetch.await_args_list
            return result
        return check

    registry = case.system.verifier._checkers
    for name in names:
        monkeypatch.setitem(registry, name, observe(registry[name]))
    task = await case.run()
    assert task.status == "succeeded", task
    assert queries == {name: [call(task.id)] for name in names}
    assert [action.tool for action in await repos.actions.list_for_task(task.id)] == ["artifacts.write"]


@pytest.mark.parametrize("field", ["new", "old", "delta", "column", "query_ref"])
async def test_claims_must_match_recompute(tmp_path, repos, field):
    case = await analysis_case(tmp_path, repos)

    def modify(decision, observation):
        claim = decision["claims"][0]
        if field == "column":
            claim[field] = "not_a_query_column"
        elif field == "query_ref":
            claim[field] = f"query:{new_id('art')}"
        else:
            claim[field] += 1

    task = await case.run(modify)
    assert task.status == "failed", task
    assert (await case.checks())["recompute_replay"].status == "failed"
    assert await case.inbox() == []


async def test_claim_rounding_tolerance(tmp_path, repos):
    case = await analysis_case(tmp_path, repos)

    def rounded(decision, observation):
        decision["claims"][0]["new"] += 0.0049

    task = await case.run(rounded)
    assert task.status == "succeeded", task
    assert (await case.checks())["recompute_replay"].status == "passed"


async def test_source_changed_again_fails_replay(tmp_path, repos):
    case = await analysis_case(tmp_path, repos)
    task = await case.run(before_submit=lambda: case.path.write_bytes(b"file replaced after recompute"))
    assert task.status == "failed", task
    check = (await case.checks())["recompute_replay"]
    assert check.status == "failed" and check.detail == "source changed again"
    assert await case.inbox() == []


async def test_replay_checks_the_sql_result_and_closes_connection(tmp_path, repos, monkeypatch):
    case = await analysis_case(tmp_path, repos)
    opened = []

    def track_connection(sources):
        con = open_readonly(sources)
        opened.append(con)
        return con

    def different_result(con, sql):
        result = run_readonly(con, sql)
        return replace(result, rows=[(42,)])

    def change_replay_result():
        # The recorded recompute and the scripted claim still agree with each other.
        monkeypatch.setattr("harness.personal.decision.open_readonly", track_connection)
        monkeypatch.setattr("harness.personal.decision.run_readonly", different_result)

    task = await case.run(before_submit=change_replay_result)
    assert task.status == "failed", task
    assert (await case.checks())["recompute_replay"].status == "failed"
    con, = opened
    with pytest.raises(duckdb.ConnectionException):
        con.execute("SELECT 1")


async def test_model_cannot_set_dedup_key(tmp_path, repos):
    case = await calendar_case(tmp_path, repos)

    def extra(decision, observation):
        decision["dedup_key"] = "chosen-by-model"

    task = await case.run(extra)
    assert task.status == "failed", task
    check = (await case.checks())["decision_schema"]
    assert check.status == "failed" and "dedup_key" in check.detail
    assert "Extra inputs are not permitted" in check.detail
    assert await case.inbox() == []


@pytest.mark.parametrize("boundary", ["creation", "expiry", "no-trigger"])
async def test_notification_needs_a_trigger_and_strict_expiry(tmp_path, repos, boundary):
    case = await calendar_case(tmp_path, repos)
    preference = await store_memory(case, "preference")

    def edit(decision, observation):
        if boundary == "no-trigger":
            decision["evidence_refs"] = [preference]
        else:
            seconds = 0 if boundary == "creation" else case.sub.result_ttl_seconds
            decision["valid_until"] = (case.task.created_at + timedelta(seconds=seconds)).isoformat()

    task = await case.run(edit)
    assert task.status == "failed", task
    assert (await case.checks())["decision_evidence"].status == "failed"


@pytest.mark.parametrize(("silent", "change"), [
    (False, {"draft": None}), (False, {"valid_until": None}),
    (False, {"valid_until": "2026-11-26T02:00:00"}), (False, {"claims": []}),
    (False, {"rationale": ""}), (False, {"evidence_refs": ["event:not-an-id"]}),
    (False, {"memory_proposals": [{"kind": "preference", "summary": "", "text": "x", "evidence_refs": ["event:evt_" + "1" * 32]}]}),
    (False, {"memory_proposals": [{"kind": "preference", "summary": "x", "text": "", "evidence_refs": ["event:evt_" + "1" * 32]}]}),
    (True, {"draft": {"title": "一条提醒", "body": "正文"}}),
    (True, {"valid_until": "2026-11-26T02:00:00Z"}),
])
async def test_invalid_calendar_decision_fails_schema(tmp_path, repos, silent, change):
    case = await calendar_case(tmp_path, repos)
    task = await case.run(lambda decision, observation: decision.update(change), silent=silent)
    assert task.status == "failed", task
    assert (await case.checks())["decision_schema"].status == "failed"


async def test_silent_decision_may_omit_null_delivery_fields(tmp_path, repos):
    case = await calendar_case(tmp_path, repos)

    def omit(decision, observation):
        decision.pop("draft")
        decision.pop("valid_until")

    task = await case.run(omit, silent=True)
    assert task.status == "succeeded", task
    assert (await case.checks())["decision_schema"].status == "passed"


async def test_normal_analysis_notification_requires_a_claim(tmp_path, repos):
    case = await analysis_case(tmp_path, repos)
    task = await case.run(lambda decision, observation: decision.update(claims=[]))
    assert task.status == "failed", task
    assert (await case.checks())["decision_schema"].status == "failed"


async def test_recompute_error_refuses_claims(tmp_path, repos):
    case = await analysis_case(tmp_path, repos, broken=True)

    def add_claim(decision, observation):
        decision["claims"] = [{"query_ref": f"query:{new_id('art')}", "column": "revenue",
                               "old": 1, "new": 2, "delta": 1}]

    task = await case.run(add_claim)
    assert task.status == "failed", task
    assert (await case.checks())["recompute_replay"].status == "failed"


async def test_recompute_error_cannot_be_silent(tmp_path, repos):
    case = await analysis_case(tmp_path, repos, broken=True)
    task = await case.run(silent=True)
    assert task.status == "failed", task
    checks = await case.checks()
    assert checks["decision_schema"].status == "failed"
    assert checks["recompute_replay"].status == "passed"
