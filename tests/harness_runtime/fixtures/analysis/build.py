"""Build orders.duckdb for the analysis-watch fixtures.

    python -m tests.harness_runtime.fixtures.analysis.build <out_dir> v1
    python -m tests.harness_runtime.fixtures.analysis.build <out_dir> v2

v1 is the seed data with its quality issues. v2 starts from v1 and fills the
null unit_price rows from the clean run, by order_item_id: "上个月补齐了缺价"
without adding orders. Nothing else changes, so a recompute must show a price
revision and not new sales.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import duckdb

from agent.data_agent.seed import generate_tables


def tables(version: str):
    dirty = generate_tables(inject_quality_issues=True)
    if version == "v1":
        return dirty
    clean = generate_tables(inject_quality_issues=False)
    # The injected duplicate rows carry new order_item_ids, so look the price up
    # by the natural key and keep the id map as a fallback.
    by_id = {row["order_item_id"]: row["unit_price"] for row in clean.order_items}
    by_key = {(row["order_id"], row["product_id"]): row["unit_price"] for row in clean.order_items}
    fixed = copy.deepcopy(dirty)
    for item in fixed.order_items:
        if item["unit_price"] is None:
            item["unit_price"] = by_id.get(item["order_item_id"]) or by_key[(item["order_id"], item["product_id"])]
    return fixed


def _dump(rows, path: Path, header: list[str]) -> None:
    import csv

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            writer.writerow(["" if value is None else value for value in row])


def write(out_dir: Path, version: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "orders.duckdb"
    target.unlink(missing_ok=True)
    data = tables(version)
    orders_csv = out_dir / "_orders.csv"
    items_csv = out_dir / "_items.csv"
    _dump([[o["order_id"], o["customer_id"], o["order_date"], o["channel"], o["status"]] for o in data.orders],
          orders_csv, ["order_id", "customer_id", "order_date", "channel", "status"])
    _dump([[i["order_item_id"], i["order_id"], i["product_id"], i["quantity"], i["unit_price"]]
           for i in data.order_items],
          items_csv, ["order_item_id", "order_id", "product_id", "quantity", "unit_price"])
    con = duckdb.connect(str(target))
    try:
        con.execute(
            "CREATE TABLE orders (order_id BIGINT, customer_id BIGINT, order_date VARCHAR,"
            " channel VARCHAR, status VARCHAR)"
        )
        con.execute(f"COPY orders FROM '{orders_csv}' (HEADER)")
        con.execute(
            "CREATE TABLE order_items (order_item_id BIGINT, order_id BIGINT, product_id BIGINT,"
            " quantity BIGINT, unit_price DOUBLE)"
        )
        con.execute(f"COPY order_items FROM '{items_csv}' (HEADER)")
    finally:
        con.close()
        orders_csv.unlink(missing_ok=True)
        items_csv.unlink(missing_ok=True)
    return target


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[2] not in ("v1", "v2"):
        print(__doc__)
        return 2
    path = write(Path(argv[1]), argv[2])
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
