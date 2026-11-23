"""results/checkers: query_evidence accepts only stored, read-only queries."""

import json

import pytest

from harness.contracts import Action, ActionStatus, ArtifactInput, CheckSpec, ToolResult, ToolStatus, VerificationStatus
from harness.results import CheckContext, query_evidence
from harness.storage.memory import InMemoryRepositories
from harness.tools.connectors import data_connectors

from ..fakes import ACTOR, MemoryArtifactStore, claimed_task

pytestmark = pytest.mark.asyncio

SPEC = CheckSpec(id="query_evidence")


async def make(tmp_path):
    repos = InMemoryRepositories()
    store = MemoryArtifactStore(repos)
    task, _execution = await claimed_task(repos)
    return repos, store, task


async def run_check(store, task, candidate, actions=()):
    ctx = CheckContext(task=task, candidate=candidate, pack=None, actor=ACTOR, artifacts=store, actions=actions)
    return await query_evidence(SPEC, ctx)


async def recorded_action(store, task, result):
    artifact = await store.put(ArtifactInput(
        scope_id=task.scope_id, name="tool_result/query", media_type="application/json",
        content=result.model_dump_json().encode(),
    ))
    return Action(task_id=task.id, attempt_id=task.current_attempt_id, logical_key="query",
                  tool="data.query", request_hash="hash", read_only=True,
                  status=ActionStatus.SUCCEEDED, result_ref=artifact.id)


async def check_records(store, task, refs):
    actions = []
    for ref in refs:
        result = ToolResult(status=ToolStatus.SUCCEEDED, data={"query_ref": ref}, artifact_refs=[ref])
        actions.append(await recorded_action(store, task, result))
    return await run_check(store, task, candidate(refs), actions)


def candidate(evidence):
    from harness.contracts import CandidateResult

    return CandidateResult(summary="done", artifact_refs={"analysis": "a1"}, evidence_refs=evidence)


async def put_query(store, *, sql="select 1", row_count=1, media="application/json", body=None):
    payload = body if body is not None else json.dumps({"sql": sql, "row_count": row_count})
    ref = await store.put(
        ArtifactInput(
            scope_id=ACTOR.scope_id,
            task_id="t1",
            name="query/q1.json",
            media_type=media,
            content=payload.encode("utf-8"),
        )
    )
    return ref.id


async def test_no_evidence_fails(tmp_path):
    repos, store, task = await make(tmp_path)
    result = await run_check(store, task, candidate([]))
    assert result.status is VerificationStatus.FAILED
    assert "no query evidence" in result.detail


async def test_missing_reference_fails(tmp_path):
    repos, store, task = await make(tmp_path)
    result = await check_records(store, task, ["art_missing"])
    assert result.status is VerificationStatus.FAILED
    assert "does not exist" in result.detail


async def test_reference_without_sql_fails(tmp_path):
    repos, store, task = await make(tmp_path)
    ref = await put_query(store, body=json.dumps({"row_count": 3}))
    result = await check_records(store, task, [ref])
    assert result.status is VerificationStatus.FAILED
    assert "no SQL" in result.detail


async def test_reference_with_a_write_fails(tmp_path):
    repos, store, task = await make(tmp_path)
    ref = await put_query(store, sql="delete from sales")
    result = await check_records(store, task, [ref])
    assert result.status is VerificationStatus.FAILED
    assert "not a read" in result.detail


async def test_reference_without_row_count_fails(tmp_path):
    repos, store, task = await make(tmp_path)
    ref = await put_query(store, body=json.dumps({"sql": "select 1"}))
    result = await check_records(store, task, [ref])
    assert result.status is VerificationStatus.FAILED
    assert "row_count" in result.detail


async def test_stored_select_passes(tmp_path):
    repos, store, task = await make(tmp_path)
    one = await put_query(store, sql="select region from sales", row_count=2)
    two = await put_query(store, sql="with t as (select 1) select * from t")
    result = await check_records(store, task, [one, two])
    assert result.status is VerificationStatus.PASSED, result.detail
    assert result.evidence_refs == [one, two]


async def test_real_query_output_verifies(tmp_path):
    """The JSON data.query writes is accepted without any massaging."""
    csv = tmp_path / "sales.csv"
    csv.write_text("region,units\nnorth,3\nsouth,5\n", encoding="utf-8")
    repos = InMemoryRepositories()
    store = MemoryArtifactStore(repos)
    task, _execution = await claimed_task(repos)
    connect, query, _list = data_connectors(store)
    from harness.contracts import AuthorizedCall

    def call(tool, **args):
        return AuthorizedCall(
            action_id="a", task_id=task.id, attempt_id=task.current_attempt_id, scope_id=ACTOR.scope_id,
            tool=tool, args=args, logical_key="k",
        )

    await connect.execute(call("data.connect", path=str(csv)))
    out = await query.execute(call("data.query", sql="select count(*) as n from sales"))
    action = await recorded_action(store, task, out)
    result = await run_check(store, task, candidate(out.artifact_refs[:1]), [action])
    assert result.status is VerificationStatus.PASSED, result.detail


@pytest.mark.parametrize("change", [
    {"task_id": "other-task"}, {"tool": "data.connect"}, {"status": ActionStatus.FAILED},
])
async def test_only_successful_queries_on_this_task_supply_evidence(tmp_path, change):
    _, store, task = await make(tmp_path)
    ref = await put_query(store)
    returned = ToolResult(status=ToolStatus.SUCCEEDED, data={"query_ref": ref}, artifact_refs=[ref])
    action = await recorded_action(store, task, returned)
    result = await run_check(store, task, candidate([ref]), [action.model_copy(update=change)])
    assert result.status is VerificationStatus.FAILED


async def test_uncited_query_output_cannot_substitute_for_cited_record(tmp_path):
    _, store, task = await make(tmp_path)
    first, second = await put_query(store), await put_query(store)
    returned = ToolResult(status=ToolStatus.SUCCEEDED, data={"query_ref": first}, artifact_refs=[first, second])
    action = await recorded_action(store, task, returned)
    result = await run_check(store, task, candidate([second]), [action])
    assert result.status is VerificationStatus.FAILED
