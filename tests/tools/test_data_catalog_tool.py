"""Tests for data catalog persistence and tool wrapper."""

from __future__ import annotations

import json

from agent.data_catalog import get_data_catalog_dir, get_relation, get_table
from tools.data_catalog_tool import DATA_CATALOG_SCHEMA, data_catalog_tool


def test_data_catalog_schema_shape():
    assert DATA_CATALOG_SCHEMA["name"] == "data_catalog"
    params = DATA_CATALOG_SCHEMA["parameters"]
    assert "action" in params["properties"]
    assert "spec" in params["properties"]


def test_upsert_and_get_table_round_trip():
    payload = {
        "description": "Orders fact table",
        "primary_key": "order_id",
        "time_fields": ["created_at"],
        "columns": [
            {"name": "order_id", "type": "string", "description": "Order identifier"},
            {"name": "uid", "type": "string", "description": "User identifier"},
            {"name": "status", "type": "string", "description": "Order status", "enum_values": {"paid": "payment succeeded"}},
        ],
    }

    result = json.loads(
        data_catalog_tool(
            action="upsert_table",
            source="warehouse",
            table_name="fact_orders",
            spec=payload,
        )
    )

    assert result["success"] is True
    assert result["record"]["table"] == "fact_orders"
    assert get_data_catalog_dir().exists()

    fetched = json.loads(
        data_catalog_tool(
            action="get_table",
            source="warehouse",
            table_name="fact_orders",
        )
    )
    assert fetched["record"]["primary_key"] == "order_id"
    assert len(fetched["record"]["columns"]) == 3
    assert get_table("warehouse", "fact_orders")["description"] == "Orders fact table"


def test_search_tables_returns_summary_rows():
    data_catalog_tool(
        action="upsert_table",
        source="warehouse",
        table_name="dim_users",
        spec={
            "description": "User dimension",
            "columns": [{"name": "uid", "type": "string", "description": "User id"}],
        },
    )

    result = json.loads(
        data_catalog_tool(
            action="search_tables",
            source="warehouse",
            query="user",
        )
    )

    assert result["success"] is True
    assert any(item["table"] == "dim_users" for item in result["results"])


def test_upsert_and_search_relations():
    data_catalog_tool(
        action="upsert_relation",
        source="warehouse",
        relation_name="users-orders",
        spec={
            "left_table": "dim_users",
            "left_key": "uid",
            "right_table": "fact_orders",
            "right_key": "uid",
            "cardinality": "one_to_many",
            "join_type_hint": "left",
            "notes": "Default user-to-order path",
        },
    )

    result = json.loads(
        data_catalog_tool(
            action="search_relations",
            source="warehouse",
            table="dim_users",
        )
    )

    assert result["success"] is True
    assert result["results"][0]["left_table"] == "dim_users"
    assert get_relation("warehouse", "users-orders")["right_table"] == "fact_orders"


def test_summary_and_sources():
    data_catalog_tool(
        action="upsert_table",
        source="mart",
        table_name="metrics_daily",
        spec={"description": "Daily metrics", "columns": [{"name": "dt"}]},
    )

    summary = json.loads(data_catalog_tool(action="summary"))
    assert summary["summary"]["total_sources"] >= 1
    assert summary["summary"]["total_tables"] >= 1

    sources = json.loads(data_catalog_tool(action="list_sources"))
    assert "mart" in sources["sources"]


def test_invalid_relation_payload_returns_error():
    result = json.loads(
        data_catalog_tool(
            action="upsert_relation",
            source="warehouse",
            relation_name="broken",
            spec={"left_table": "a"},
        )
    )
    assert "error" in result
