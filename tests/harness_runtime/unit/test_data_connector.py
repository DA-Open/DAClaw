"""tools/connectors/data.py: connect, query, and the SQL gate."""

import json

import pytest

from harness.contracts import AuthorizedCall, ErrorCode, ToolStatus
from harness.storage.memory import InMemoryRepositories
from harness.tools.connectors import data_connectors

from ..fakes import MemoryArtifactStore

pytestmark = pytest.mark.asyncio

CSV = "region,units,price\nnorth,3,10.0\nsouth,5,12.5\nnorth,2,10.0\n"


def call(tool, **args):
    return AuthorizedCall(
        action_id="act_1",
        task_id="task_1",
        attempt_id="att_1",
        scope_id="s1",
        tool=tool,
        args=args,
        logical_key="step-1.0",
    )


def session(tmp_path, text=CSV, name="sales.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    store = MemoryArtifactStore(InMemoryRepositories())
    connect, query, _list = data_connectors(store)
    return path, store, connect, query


async def test_connect_reports_columns_and_rows(tmp_path):
    path, store, connect, _ = session(tmp_path)
    out = await connect.execute(call("data.connect", path=str(path)))
    assert out.status is ToolStatus.SUCCEEDED
    assert out.data["table"] == "sales"
    assert out.data["row_count"] == 3
    assert [c["name"] for c in out.data["columns"]] == ["region", "units", "price"]


async def test_reconnecting_the_same_file_reuses_the_view(tmp_path):
    path, store, connect, _ = session(tmp_path)
    first = await connect.execute(call("data.connect", path=str(path)))
    second = await connect.execute(call("data.connect", path=str(path), name="other"))
    assert second.status is ToolStatus.SUCCEEDED
    assert "already connected" in second.summary
    assert second.data["table"] == first.data["table"]


async def test_query_reports_exact_count_and_writes_evidence(tmp_path):
    path, store, connect, query = session(tmp_path)
    await connect.execute(call("data.connect", path=str(path)))
    out = await query.execute(call("data.query", sql="select region, sum(units) as units from sales group by 1"))
    assert out.status is ToolStatus.SUCCEEDED, out.error
    assert out.data["row_count"] == 2
    assert out.data["row_count_capped"] is False
    assert out.summary.startswith("query_ref: ")
    # Two artifacts: the evidence JSON and the csv, both in the task's area.
    refs = out.artifact_refs
    assert len(refs) == 2
    evidence = json.loads(store.content[out.data["query_ref"]].decode())
    assert evidence["row_count"] == 2 and "select region" in evidence["sql"]
    assert evidence["csv_ref"] in refs


async def test_query_before_connect_is_refused(tmp_path):
    path, store, connect, query = session(tmp_path)
    out = await query.execute(call("data.query", sql="select 1"))
    assert out.status is ToolStatus.FAILED and out.error.code is ErrorCode.INVALID_REQUEST


@pytest.mark.parametrize(
    "sql",
    [
        "drop table sales",
        "insert into sales values ('x',1,1)",
        "copy (select 1) to '/tmp/leak.csv'",
        "pragma enable_external_access=true",
        "set memory_limit='1GB'",
        "select 1; drop table sales",
        "attach '/etc/passwd' as x",
    ],
)
async def test_non_read_statements_are_refused(tmp_path, sql):
    path, store, connect, query = session(tmp_path)
    await connect.execute(call("data.connect", path=str(path)))
    out = await query.execute(call("data.query", sql=sql))
    assert out.status is ToolStatus.FAILED
    assert out.error.code is ErrorCode.INVALID_REQUEST
    assert "SELECT" in out.error.message or "statement" in out.error.message


async def test_query_cannot_read_another_file(tmp_path):
    path, store, connect, query = session(tmp_path)
    secret = tmp_path / "secret.csv"
    secret.write_text("a,b\n1,2\n", encoding="utf-8")
    await connect.execute(call("data.connect", path=str(path)))
    out = await query.execute(call("data.query", sql=f"select * from read_csv_auto('{secret}')"))
    assert out.status is ToolStatus.FAILED
    assert out.error.code is ErrorCode.INVALID_REQUEST


async def test_sessions_are_per_task(tmp_path):
    path, store, connect, query = session(tmp_path)
    await connect.execute(call("data.connect", path=str(path)))
    other = call("data.query", sql="select count(*) from sales")
    other = other.model_copy(update={"task_id": "task_2"})
    out = await query.execute(other)
    assert out.status is ToolStatus.FAILED and out.error.code is ErrorCode.INVALID_REQUEST


async def test_unsupported_and_missing_files_are_refused(tmp_path):
    path, store, connect, query = session(tmp_path, name="notes.txt")
    out = await connect.execute(call("data.connect", path=str(path)))
    assert out.status is ToolStatus.FAILED and out.error.code is ErrorCode.INVALID_REQUEST
    missing = await connect.execute(call("data.connect", path=str(tmp_path / "nope.csv")))
    assert missing.status is ToolStatus.FAILED and missing.error.code is ErrorCode.NOT_FOUND


async def test_parquet_round_trip(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "numbers.parquet"
    con = duckdb.connect(":memory:")
    con.execute("COPY (SELECT * FROM (VALUES (1,'a'),(2,'b')) t(id, label)) TO ? (FORMAT PARQUET)", [str(path)])
    con.close()
    store = MemoryArtifactStore(InMemoryRepositories())
    connect, query, _list = data_connectors(store)
    connected = await connect.execute(call("data.connect", path=str(path)))
    assert connected.status is ToolStatus.SUCCEEDED, connected.error
    out = await query.execute(call("data.query", sql="select count(*) as n from numbers"))
    assert out.data["row_count"] == 1
    assert "2" in out.summary


# -- data.list -----------------------------------------------------------------


def listing(paths=(), **args):
    return AuthorizedCall(
        action_id="act_l",
        task_id="task_1",
        attempt_id="att_1",
        scope_id="s1",
        tool="data.list",
        args=args,
        logical_key="step-0.0",
        allowed_paths=[str(p) for p in paths],
    )


async def test_list_finds_data_files_under_the_allowed_dirs(tmp_path):
    root = tmp_path / "root"
    (root / "nested").mkdir(parents=True)
    (root / "a.csv").write_text("x\n1\n", encoding="utf-8")
    (root / "nested" / "b.parquet").write_bytes(b"")
    (root / "notes.txt").write_text("hi", encoding="utf-8")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "c.csv").write_text("x\n", encoding="utf-8")
    store = MemoryArtifactStore(InMemoryRepositories())
    _connect, _query, lister = data_connectors(store)
    out = await lister.execute(listing([root]))
    assert out.status is ToolStatus.SUCCEEDED
    names = [f.rsplit("/", 1)[-1] for f in out.data["files"]]
    assert names == ["a.csv", "b.parquet"]
    assert out.data["truncated"] is False
    assert "a.csv" in out.summary and "Pass one of these paths" in out.summary


async def test_list_filters_by_pattern_and_limit(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    for name in ("sales_2025.csv", "sales_2026.csv", "costs.csv"):
        (root / name).write_text("x\n", encoding="utf-8")
    store = MemoryArtifactStore(InMemoryRepositories())
    _connect, _query, lister = data_connectors(store)
    only = await lister.execute(listing([root], pattern="sales"))
    assert [f.rsplit("/", 1)[-1] for f in only.data["files"]] == ["sales_2025.csv", "sales_2026.csv"]
    capped = await lister.execute(listing([root], limit=1))
    assert len(capped.data["files"]) == 1 and capped.data["truncated"] is True


async def test_list_without_allowed_dirs_says_so(tmp_path):
    store = MemoryArtifactStore(InMemoryRepositories())
    _connect, _query, lister = data_connectors(store)
    out = await lister.execute(listing([]))
    assert out.status is ToolStatus.SUCCEEDED
    assert out.data["files"] == [] and "Ask the user" in out.summary


async def test_list_with_no_data_files_says_so(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "readme.md").write_text("x", encoding="utf-8")
    store = MemoryArtifactStore(InMemoryRepositories())
    _connect, _query, lister = data_connectors(store)
    out = await lister.execute(listing([root]))
    assert out.data["files"] == [] and "No CSV" in out.summary

