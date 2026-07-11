"""Reproducible demo dataset generator for DataAgent.

Generates a small synthetic e-commerce dataset with a *fixed seed*, so anyone
who runs ``daclaw data demo-seed`` gets byte-identical data — demos never drift.

Tables:
    customers    (customer_id, name, segment, city, signup_date)
    products     (product_id, product_name, category, price)
    orders       (order_id, customer_id, order_date, channel, status)
    order_items  (order_item_id, order_id, product_id, quantity, unit_price)
    events       (event_id, customer_id, event_type, ts)

The data has clean, chart-friendly trends (monthly seasonality, category mix)
so the "upload → beautiful dashboard" line looks great out of the box. A few
mild, realistic quality quirks are included and documented so the trust /
validation scenario has something to catch — but they don't spoil the visuals.

Only stdlib + (optional) pandas/duckdb for output; no network.
"""

from __future__ import annotations

import csv
import random
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

SEED = 20260706

_SEGMENTS = ["VIP", "Regular", "New"]
_CITIES = ["Beijing", "Shanghai", "Shenzhen", "Hangzhou", "Chengdu", "Guangzhou"]
_CHANNELS = ["web", "app", "store"]
_STATUSES = ["completed", "completed", "completed", "refunded", "cancelled"]
_EVENT_TYPES = ["view", "view", "view", "add_to_cart", "purchase", "search"]

_CATEGORIES = {
    "Electronics": (299, 4999),
    "Apparel": (49, 799),
    "Home": (29, 1299),
    "Beauty": (39, 599),
    "Grocery": (5, 199),
}

_PRODUCT_WORDS = {
    "Electronics": ["Pro Earbuds", "Smart Watch", "4K Monitor", "Power Bank", "Mini Speaker", "Webcam"],
    "Apparel": ["Cotton Tee", "Denim Jacket", "Running Shoes", "Wool Scarf", "Rain Coat"],
    "Home": ["Ceramic Mug", "Desk Lamp", "Throw Blanket", "Knife Set", "Air Purifier"],
    "Beauty": ["Face Serum", "Lip Balm", "Sheet Mask", "Hair Oil", "Sunscreen"],
    "Grocery": ["Green Tea", "Dark Chocolate", "Olive Oil", "Granola", "Sparkling Water"],
}


@dataclass
class SeedTables:
    customers: List[Dict[str, Any]]
    products: List[Dict[str, Any]]
    orders: List[Dict[str, Any]]
    order_items: List[Dict[str, Any]]
    events: List[Dict[str, Any]]

    def as_dict(self) -> Dict[str, List[Dict[str, Any]]]:
        return {
            "customers": self.customers,
            "products": self.products,
            "orders": self.orders,
            "order_items": self.order_items,
            "events": self.events,
        }


def generate_tables(
    *,
    n_customers: int = 800,
    n_orders: int = 6000,
    inject_quality_issues: bool = True,
) -> SeedTables:
    """Build the dataset in memory (deterministic given the module SEED)."""
    rng = random.Random(SEED)

    # -- products -----------------------------------------------------------
    products: List[Dict[str, Any]] = []
    pid = 1
    for category, (lo, hi) in _CATEGORIES.items():
        for word in _PRODUCT_WORDS[category]:
            price = round(rng.uniform(lo, hi), 2)
            products.append(
                {
                    "product_id": pid,
                    "product_name": word,
                    "category": category,
                    "price": price,
                }
            )
            pid += 1

    # -- customers ----------------------------------------------------------
    start = date(2025, 1, 1)
    customers: List[Dict[str, Any]] = []
    for cid in range(1, n_customers + 1):
        signup = start + timedelta(days=rng.randint(0, 364))
        customers.append(
            {
                "customer_id": cid,
                "name": f"Customer {cid:04d}",
                "segment": rng.choices(_SEGMENTS, weights=[1, 4, 2])[0],
                "city": rng.choice(_CITIES),
                "signup_date": signup.isoformat(),
            }
        )

    # -- orders + order_items ----------------------------------------------
    orders: List[Dict[str, Any]] = []
    order_items: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    oi_id = 1
    ev_id = 1

    for oid in range(1, n_orders + 1):
        cust = rng.choice(customers)
        # seasonal monthly weighting: ramp up towards Q4 (nice upward trend)
        month = rng.choices(
            range(1, 13),
            weights=[6, 6, 7, 8, 9, 10, 11, 12, 13, 15, 20, 24],
        )[0]
        day = rng.randint(1, 28)
        odate = date(2025, month, day)
        channel = rng.choices(_CHANNELS, weights=[5, 6, 2])[0]
        status = rng.choice(_STATUSES)
        orders.append(
            {
                "order_id": oid,
                "customer_id": cust["customer_id"],
                "order_date": odate.isoformat(),
                "channel": channel,
                "status": status,
            }
        )

        # 1-4 line items per order
        n_items = rng.choices([1, 2, 3, 4], weights=[5, 4, 2, 1])[0]
        chosen = rng.sample(products, k=min(n_items, len(products)))
        for prod in chosen:
            qty = rng.choices([1, 2, 3], weights=[7, 2, 1])[0]
            unit_price = prod["price"]
            order_items.append(
                {
                    "order_item_id": oi_id,
                    "order_id": oid,
                    "product_id": prod["product_id"],
                    "quantity": qty,
                    "unit_price": unit_price,
                }
            )
            oi_id += 1

        # a couple of events per order
        for _ in range(rng.randint(1, 3)):
            etype = rng.choice(_EVENT_TYPES)
            hour = rng.randint(0, 23)
            ts = datetime(2025, month, day, hour, rng.randint(0, 59))
            events.append(
                {
                    "event_id": ev_id,
                    "customer_id": cust["customer_id"],
                    "event_type": etype,
                    "ts": ts.isoformat(),
                }
            )
            ev_id += 1

    if inject_quality_issues:
        _inject_issues(rng, orders, order_items, events)

    return SeedTables(customers, products, orders, order_items, events)


def _inject_issues(rng, orders, order_items, events) -> None:
    """Add mild, realistic data-quality quirks for the trust/validation demo.

    Documented in the demo plan (scenario C):
      1. Duplicate order_items rows -> naive SUM inflates GMV.
      2. Some NULL unit_price in one month -> silent row-drop risk.
      3. A slice of events carry a +08:00 offset -> timezone mixing.
    These are subtle enough not to ruin the default dashboards.
    """
    # 1. duplicate ~1.5% of order_items (same natural key, new surrogate id)
    dup_count = max(1, len(order_items) // 66)
    next_id = max(oi["order_item_id"] for oi in order_items) + 1
    for _ in range(dup_count):
        src = rng.choice(order_items[: len(order_items) // 2])
        clone = dict(src)
        clone["order_item_id"] = next_id
        next_id += 1
        order_items.append(clone)

    # 2. NULL out unit_price for a small slice of March items
    march_orders = {o["order_id"] for o in orders if o["order_date"].startswith("2025-03")}
    nulled = 0
    for oi in order_items:
        if oi["order_id"] in march_orders and rng.random() < 0.12:
            oi["unit_price"] = None
            nulled += 1

    # 3. tag ~10% of events with an explicit +08:00 offset (tz mixing)
    for ev in events:
        if rng.random() < 0.10:
            ev["ts"] = ev["ts"] + "+08:00"


# ---------------------------------------------------------------------------
# writers
# ---------------------------------------------------------------------------


def _write_csv(rows: List[Dict[str, Any]], path: Path) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_sqlite(tables: Dict[str, List[Dict[str, Any]]], path: Path) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        for name, rows in tables.items():
            if not rows:
                continue
            cols = list(rows[0].keys())
            col_defs = ", ".join(f'"{c}"' for c in cols)
            placeholders = ", ".join("?" for _ in cols)
            conn.execute(f'CREATE TABLE "{name}" ({col_defs})')
            conn.executemany(
                f'INSERT INTO "{name}" ({col_defs}) VALUES ({placeholders})',
                [tuple(r[c] for c in cols) for r in rows],
            )
        conn.commit()
    finally:
        conn.close()


def _write_parquet(tables: Dict[str, List[Dict[str, Any]]], out_dir: Path) -> List[Path]:
    try:
        import pandas as pd
    except Exception:
        return []
    written: List[Path] = []
    for name, rows in tables.items():
        df = pd.DataFrame(rows)
        p = out_dir / f"{name}.parquet"
        try:
            df.to_parquet(p, index=False)
            written.append(p)
        except Exception:
            # pyarrow/fastparquet missing — skip parquet, CSV/SQLite still work
            return written
    return written


def _write_excel(tables: Dict[str, List[Dict[str, Any]]], path: Path) -> Optional[Path]:
    try:
        import pandas as pd
    except Exception:
        return None
    try:
        with pd.ExcelWriter(path) as xl:
            for name, rows in tables.items():
                pd.DataFrame(rows).to_excel(xl, sheet_name=name[:31], index=False)
        return path
    except Exception:
        return None


def write_dataset(
    out_dir: str | Path,
    *,
    formats: Optional[List[str]] = None,
    n_customers: int = 800,
    n_orders: int = 6000,
    inject_quality_issues: bool = True,
) -> Dict[str, Any]:
    """Generate the dataset and write it in the requested formats.

    Returns a manifest describing what was written.
    """
    formats = formats or ["csv", "sqlite", "parquet", "excel"]
    out = Path(out_dir).expanduser()
    if not out.is_absolute():
        out = (Path.cwd() / out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    tables = generate_tables(
        n_customers=n_customers,
        n_orders=n_orders,
        inject_quality_issues=inject_quality_issues,
    )
    data = tables.as_dict()

    written: Dict[str, Any] = {"dir": str(out), "formats": {}}

    if "csv" in formats:
        csv_dir = out / "csv"
        csv_dir.mkdir(exist_ok=True)
        paths = []
        for name, rows in data.items():
            p = csv_dir / f"{name}.csv"
            _write_csv(rows, p)
            paths.append(str(p))
        written["formats"]["csv"] = paths

    if "sqlite" in formats:
        p = out / "ecommerce.sqlite"
        _write_sqlite(data, p)
        written["formats"]["sqlite"] = [str(p)]

    if "parquet" in formats:
        pq_dir = out / "parquet"
        pq_dir.mkdir(exist_ok=True)
        paths = _write_parquet(data, pq_dir)
        written["formats"]["parquet"] = [str(p) for p in paths]

    if "excel" in formats:
        p = out / "ecommerce.xlsx"
        r = _write_excel(data, p)
        if r:
            written["formats"]["excel"] = [str(r)]

    written["row_counts"] = {name: len(rows) for name, rows in data.items()}
    written["quality_issues_injected"] = inject_quality_issues
    return written


if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "./daclaw_demo_data"
    print(json.dumps(write_dataset(target), ensure_ascii=False, indent=2))
