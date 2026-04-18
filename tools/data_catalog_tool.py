#!/usr/bin/env python3
"""Data catalog maintenance tool for structured analysis knowledge."""

from __future__ import annotations

import logging
from typing import Any, Dict

from agent.data_catalog import (
    catalog_summary,
    ensure_catalog_layout,
    get_relation,
    get_table,
    list_sources,
    search_relations,
    search_tables,
    upsert_relation,
    upsert_table,
)
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)


DATA_CATALOG_SCHEMA: Dict[str, Any] = {
    "name": "data_catalog",
    "description": (
        "Maintain and query the structured data catalog for data-analysis work. "
        "Use it to persist durable schema knowledge such as table definitions, "
        "field meanings, enum explanations, and table-to-table relationships."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "upsert_table",
                    "get_table",
                    "search_tables",
                    "upsert_relation",
                    "get_relation",
                    "search_relations",
                    "list_sources",
                    "summary",
                ],
                "description": "Catalog operation to perform.",
            },
            "source": {
                "type": "string",
                "description": "Logical data source or warehouse name. Optional for global search/summary actions.",
            },
            "table_name": {
                "type": "string",
                "description": "Table name for table operations.",
            },
            "relation_name": {
                "type": "string",
                "description": "Relation name for relation operations. If omitted on upsert_relation, a name is synthesized.",
            },
            "query": {
                "type": "string",
                "description": "Free-text search over table or relation metadata.",
            },
            "table": {
                "type": "string",
                "description": "Filter relations touching this table.",
            },
            "spec": {
                "type": "object",
                "description": (
                    "Structured catalog payload. For tables: include fields like description, primary_key, "
                    "time_fields, and columns[]. For relations: include left_table, left_key, right_table, "
                    "right_key, cardinality, join_type_hint, and notes."
                ),
            },
        },
        "required": ["action"],
    },
}


def check_data_catalog_requirements() -> bool:
    return True


def data_catalog_tool(
    *,
    action: str,
    source: str = "",
    table_name: str = "",
    relation_name: str = "",
    query: str = "",
    table: str = "",
    spec: Dict[str, Any] | None = None,
) -> str:
    """Execute a data catalog maintenance or lookup action."""
    try:
        ensure_catalog_layout()
        if action == "upsert_table":
            if not table_name:
                return tool_error("table_name is required for upsert_table.")
            result = upsert_table(source or "default", table_name, spec or {})
            return tool_result(success=True, action=action, **result)
        if action == "get_table":
            if not table_name:
                return tool_error("table_name is required for get_table.")
            return tool_result(success=True, action=action, record=get_table(source or "default", table_name))
        if action == "search_tables":
            return tool_result(success=True, action=action, results=search_tables(query=query, source=source))
        if action == "upsert_relation":
            result = upsert_relation(source or "default", relation_name, spec or {})
            return tool_result(success=True, action=action, **result)
        if action == "get_relation":
            if not relation_name:
                return tool_error("relation_name is required for get_relation.")
            return tool_result(success=True, action=action, record=get_relation(source or "default", relation_name))
        if action == "search_relations":
            return tool_result(
                success=True,
                action=action,
                results=search_relations(source=source, table=table, query=query),
            )
        if action == "list_sources":
            return tool_result(success=True, action=action, sources=list_sources())
        if action == "summary":
            return tool_result(success=True, action=action, summary=catalog_summary(source))
        return tool_error(f"Unknown action '{action}'.")
    except FileNotFoundError as exc:
        return tool_error(f"Catalog entry not found: {exc}")
    except ValueError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        logger.exception("data_catalog tool failed: %s", exc)
        return tool_error(f"data_catalog failed: {exc}")


def _handle_data_catalog(args, **_kwargs):
    return data_catalog_tool(
        action=args.get("action", ""),
        source=args.get("source", ""),
        table_name=args.get("table_name", ""),
        relation_name=args.get("relation_name", ""),
        query=args.get("query", ""),
        table=args.get("table", ""),
        spec=args.get("spec"),
    )


registry.register(
    name="data_catalog",
    toolset="data",
    schema=DATA_CATALOG_SCHEMA,
    handler=_handle_data_catalog,
    check_fn=check_data_catalog_requirements,
    emoji="DATA",
    max_result_size_chars=200_000,
)
