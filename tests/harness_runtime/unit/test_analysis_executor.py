"""The shared read-only executor, the sha256 recorded at connect, and the fixture."""

import hashlib
from pathlib import Path

import pytest

from harness.contracts import AuthorizedCall, ErrorCode, ToolStatus
from harness.storage.memory import InMemoryRepositories
from harness.tools.connectors.data import (
    ReadonlySource,
    data_connectors,
    file_digest,
    open_readonly,
    run_readonly,
)

from ..fakes import MemoryArtifactStore

pytestmark = pytest.mark.asyncio

CSV = "region,units\nnorth,3\nsouth,5\n"


def call(tool, **args):
    return AuthorizedCall(
        action_id="act_1", task_id="task_1", attempt_id="att_1", scope_id="s1",
        tool=tool, args=args, logical_key="step-1.0",
    )


def tools():
    store = MemoryArtifactStore(InMemoryRepositories())
    return store, data_connectors(store)


async def test_readonly_executor_matches_query(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text(CSV, encoding="utf-8")
    _store, (connect, query, _list) = tools()
    connected = await connect.execute(call("data.connect", path=str(path)))
    assert connected.status is ToolStatus.SUCCEEDED
    sql = "select region, sum(units) as units from sales group by 1 order by 1"
    via_tool = await query.execute(call("data.query", sql=sql))

    con = open_readonly([ReadonlySource(path=path, kind="csv", primary="sales", tables={"sales": "sales"})])
    try:
        via_executor = run_readonly(con, sql)
    finally:
        con.close()
    assert via_executor.columns == via_tool.data["columns"]
    assert via_executor.row_count == via_tool.data["row_count"]
    assert sorted(via_executor.rows) == [("north", 3), ("south", 5)]

    with pytest.raises(ValueError):
        run_readonly(open_readonly([ReadonlySource(path=path, kind="csv", primary="sales",
                                                   tables={"sales": "sales"})]), "pragma version")


async def test_readonly_executor_refuses_changed_tables(tmp_path):
    import duckdb

    path = tmp_path / "two.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE a (x INTEGER)")
    con.execute("CREATE TABLE b (x INTEGER)")
    con.close()
    source = ReadonlySource(path=path, kind="duckdb", primary="two", tables={"a": "two", "b": "two_b"})
    open_readonly([source]).close()

    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE c (x INTEGER)")
    con.close()
    with pytest.raises(ValueError, match="source tables changed"):
        open_readonly([source])


async def test_connect_records_sha256(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text(CSV, encoding="utf-8")
    _store, (connect, _query, _list) = tools()
    out = await connect.execute(call("data.connect", path=str(path)))
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert out.data["sha256"] == expected == file_digest(path)
    assert out.data["size_bytes"] == path.stat().st_size


def _seed(out_dir: Path, version: str) -> Path:
    from tests.harness_runtime.fixtures.analysis.build import write

    return write(out_dir, version)


def _march(path: Path) -> tuple[int, int, float]:
    from harness.tools.connectors.data import ReadonlySource, open_readonly, run_readonly

    con = open_readonly([ReadonlySource(path=path, kind="duckdb", primary="orders",
                                        tables={"order_items": "orders", "orders": "orders_orders"})])
    try:
        orders = run_readonly(con, """
            SELECT count(*) AS n FROM orders_orders WHERE status='completed' AND order_date LIKE '2025-03%'
        """).rows[0][0]
        missing, revenue = run_readonly(con, """
            WITH dedup AS (
              SELECT * FROM orders oi
              WHERE order_item_id = (SELECT min(order_item_id) FROM orders x
                                     WHERE x.order_id = oi.order_id AND x.product_id = oi.product_id)
            )
            SELECT count(*) FILTER (WHERE unit_price IS NULL), round(sum(quantity * unit_price), 2)
            FROM dedup JOIN orders_orders o USING (order_id)
            WHERE o.status='completed' AND o.order_date LIKE '2025-03%'
        """).rows[0]
    finally:
        con.close()
    return orders, missing, revenue


async def test_fixture_numbers(tmp_path):
    v1 = _seed(tmp_path / "v1", "v1")
    v2 = _seed(tmp_path / "v2", "v2")
    assert _march(v1) == (169, 27, 219912.81)
    assert _march(v2) == (169, 0, 232944.31)
