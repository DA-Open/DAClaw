"""Structured data catalog for data-analysis workflows.

Stores durable table metadata and table-to-table relationships under the
profile-scoped DACLAW_HOME so the agent can reuse schema knowledge across
sessions without polluting general memory.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from daclaw_constants import get_daclaw_home
from utils import atomic_yaml_write


CATALOG_VERSION = 1
_SAFE_NAME_RE = re.compile(r"[^a-z0-9._-]+")


def get_data_catalog_dir() -> Path:
    """Return the profile-scoped data catalog directory."""
    return get_daclaw_home() / "data_catalog"


def _slugify(value: str, *, fallback: str = "default") -> str:
    text = (value or "").strip().lower()
    text = _SAFE_NAME_RE.sub("-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or fallback


def _source_dir(source: str) -> Path:
    return get_data_catalog_dir() / _slugify(source, fallback="default")


def _table_path(source: str, table_name: str) -> Path:
    return _source_dir(source) / "tables" / f"{_slugify(table_name)}.yaml"


def _relation_path(source: str, relation_name: str) -> Path:
    return _source_dir(source) / "relations" / f"{_slugify(relation_name)}.yaml"


def ensure_catalog_layout() -> Dict[str, str]:
    """Create the top-level data catalog directory if missing."""
    root = get_data_catalog_dir()
    root.mkdir(parents=True, exist_ok=True)
    return {"root": str(root)}


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Catalog file must contain a YAML object: {path}")
    return data


def _normalize_column(column: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(column)
    item["name"] = str(item.get("name", "")).strip()
    if not item["name"]:
        raise ValueError("Each column entry must include a non-empty name.")
    if "type" in item and item["type"] is not None:
        item["type"] = str(item["type"]).strip()
    if "description" in item and item["description"] is not None:
        item["description"] = str(item["description"]).strip()
    if "enum_values" in item and item["enum_values"] is not None:
        enum_values = item["enum_values"]
        if isinstance(enum_values, dict):
            item["enum_values"] = enum_values
        elif isinstance(enum_values, list):
            item["enum_values"] = enum_values
        else:
            raise ValueError("column.enum_values must be a list or mapping.")
    return item


def normalize_table_spec(source: str, table_name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Return a normalized table catalog record."""
    if not isinstance(spec, dict):
        raise ValueError("table spec must be an object.")

    canonical_table = str(spec.get("table") or table_name or "").strip()
    if not canonical_table:
        raise ValueError("table_name is required.")

    columns = spec.get("columns", [])
    if columns is None:
        columns = []
    if not isinstance(columns, list):
        raise ValueError("columns must be a list.")

    normalized_columns = [_normalize_column(col) for col in columns]
    seen: set[str] = set()
    for col in normalized_columns:
        name = col["name"].lower()
        if name in seen:
            raise ValueError(f"Duplicate column name '{col['name']}'.")
        seen.add(name)

    data = {
        "catalog_version": CATALOG_VERSION,
        "kind": "table",
        "source": str(spec.get("source") or source or "default").strip() or "default",
        "table": canonical_table,
        "description": str(spec.get("description") or "").strip(),
        "primary_key": str(spec.get("primary_key") or "").strip(),
        "time_fields": list(spec.get("time_fields") or []),
        "owners": list(spec.get("owners") or []),
        "tags": list(spec.get("tags") or []),
        "grain": str(spec.get("grain") or "").strip(),
        "notes": str(spec.get("notes") or "").strip(),
        "columns": normalized_columns,
    }
    return data


def normalize_relation_spec(source: str, relation_name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Return a normalized relation catalog record."""
    if not isinstance(spec, dict):
        raise ValueError("relation spec must be an object.")

    left_table = str(spec.get("left_table") or "").strip()
    right_table = str(spec.get("right_table") or "").strip()
    left_key = str(spec.get("left_key") or "").strip()
    right_key = str(spec.get("right_key") or "").strip()
    if not all([left_table, right_table, left_key, right_key]):
        raise ValueError("left_table, right_table, left_key, and right_key are required.")

    canonical_name = str(spec.get("name") or relation_name or "").strip()
    if not canonical_name:
        canonical_name = f"{left_table}-{left_key}-to-{right_table}-{right_key}"

    data = {
        "catalog_version": CATALOG_VERSION,
        "kind": "relation",
        "source": str(spec.get("source") or source or "default").strip() or "default",
        "name": canonical_name,
        "left_table": left_table,
        "left_key": left_key,
        "right_table": right_table,
        "right_key": right_key,
        "cardinality": str(spec.get("cardinality") or "").strip(),
        "join_type_hint": str(spec.get("join_type_hint") or "").strip(),
        "confidence": str(spec.get("confidence") or "").strip(),
        "notes": str(spec.get("notes") or "").strip(),
        "filters": list(spec.get("filters") or []),
    }
    return data


def upsert_table(source: str, table_name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Create or replace a table catalog entry."""
    ensure_catalog_layout()
    data = normalize_table_spec(source, table_name, spec)
    path = _table_path(data["source"], data["table"])
    atomic_yaml_write(path, data, sort_keys=False)
    return {"path": str(path), "record": data}


def get_table(source: str, table_name: str) -> Dict[str, Any]:
    return _load_yaml(_table_path(source, table_name))


def search_tables(query: str = "", source: str = "") -> List[Dict[str, Any]]:
    ensure_catalog_layout()
    needle = (query or "").strip().lower()
    results: List[Dict[str, Any]] = []
    sources = [_source_dir(source)] if source else sorted(p for p in get_data_catalog_dir().iterdir() if p.is_dir())
    for source_dir in sources:
        tables_dir = source_dir / "tables"
        if not tables_dir.exists():
            continue
        for path in sorted(tables_dir.glob("*.yaml")):
            data = _load_yaml(path)
            searchable = " ".join(
                [
                    str(data.get("table", "")),
                    str(data.get("description", "")),
                    " ".join(str(col.get("name", "")) for col in data.get("columns", [])),
                    " ".join(str(col.get("description", "")) for col in data.get("columns", [])),
                ]
            ).lower()
            if needle and needle not in searchable:
                continue
            results.append(
                {
                    "source": data.get("source", source_dir.name),
                    "table": data.get("table", path.stem),
                    "description": data.get("description", ""),
                    "columns_count": len(data.get("columns", [])),
                    "path": str(path),
                }
            )
    return results


def upsert_relation(source: str, relation_name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Create or replace a relation catalog entry."""
    ensure_catalog_layout()
    data = normalize_relation_spec(source, relation_name, spec)
    path = _relation_path(data["source"], data["name"])
    atomic_yaml_write(path, data, sort_keys=False)
    return {"path": str(path), "record": data}


def get_relation(source: str, relation_name: str) -> Dict[str, Any]:
    return _load_yaml(_relation_path(source, relation_name))


def search_relations(
    *,
    source: str = "",
    table: str = "",
    query: str = "",
) -> List[Dict[str, Any]]:
    """Return relation entries filtered by source, table, or free text query."""
    ensure_catalog_layout()
    source_dirs = [_source_dir(source)] if source else sorted(p for p in get_data_catalog_dir().iterdir() if p.is_dir())
    table_needle = (table or "").strip().lower()
    query_needle = (query or "").strip().lower()
    results: List[Dict[str, Any]] = []
    for source_dir in source_dirs:
        rel_dir = source_dir / "relations"
        if not rel_dir.exists():
            continue
        for path in sorted(rel_dir.glob("*.yaml")):
            data = _load_yaml(path)
            haystack = " ".join(
                [
                    str(data.get("name", "")),
                    str(data.get("left_table", "")),
                    str(data.get("left_key", "")),
                    str(data.get("right_table", "")),
                    str(data.get("right_key", "")),
                    str(data.get("notes", "")),
                ]
            ).lower()
            if table_needle and table_needle not in {
                str(data.get("left_table", "")).lower(),
                str(data.get("right_table", "")).lower(),
            }:
                continue
            if query_needle and query_needle not in haystack:
                continue
            results.append(
                {
                    "source": data.get("source", source_dir.name),
                    "name": data.get("name", path.stem),
                    "left_table": data.get("left_table", ""),
                    "left_key": data.get("left_key", ""),
                    "right_table": data.get("right_table", ""),
                    "right_key": data.get("right_key", ""),
                    "cardinality": data.get("cardinality", ""),
                    "join_type_hint": data.get("join_type_hint", ""),
                    "path": str(path),
                }
            )
    return results


def list_sources() -> List[str]:
    """Return all configured data catalog sources."""
    ensure_catalog_layout()
    return sorted(path.name for path in get_data_catalog_dir().iterdir() if path.is_dir())


def catalog_summary(source: str = "") -> Dict[str, Any]:
    """Return a compact count summary for one source or all sources."""
    ensure_catalog_layout()
    summary: Dict[str, Any] = {"sources": []}
    for source_name in ([_slugify(source)] if source else list_sources()):
        source_dir = _source_dir(source_name)
        tables = list((source_dir / "tables").glob("*.yaml")) if (source_dir / "tables").exists() else []
        relations = list((source_dir / "relations").glob("*.yaml")) if (source_dir / "relations").exists() else []
        summary["sources"].append(
            {
                "source": source_name,
                "tables": len(tables),
                "relations": len(relations),
            }
        )
    summary["total_sources"] = len(summary["sources"])
    summary["total_tables"] = sum(item["tables"] for item in summary["sources"])
    summary["total_relations"] = sum(item["relations"] for item in summary["sources"])
    return summary
