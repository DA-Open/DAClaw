#!/usr/bin/env python3
"""Data analysis tool for local tabular sources.

Initial scope: local CSV/TSV profiling.
Design keeps a single tool entry point while delegating source-specific logic
to adapter classes (see ``agent/data_sources.py``), so SQL adapters can be
added later without changing the tool contract.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from agent.data_sources import DataSourceError, profile_data_source
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)


DATA_PROFILE_SCHEMA: Dict[str, Any] = {
    "name": "data_profile",
    "description": (
        "Profile a local tabular dataset (CSV/TSV) and return schema, nulls, "
        "basic numeric stats, and sample rows. Use this as the first step before "
        "deeper analysis."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Path to the local dataset file (CSV/TSV).",
            },
            "sample_rows": {
                "type": "integer",
                "description": "Number of preview rows to include in output (default: 10, max: 100).",
                "default": 10,
                "minimum": 1,
                "maximum": 100,
            },
            "max_rows": {
                "type": "integer",
                "description": (
                    "Maximum data rows to scan while profiling (default: 100000, "
                    "max: 1000000). Larger files are truncated."
                ),
                "default": 100000,
                "minimum": 1,
                "maximum": 1000000,
            },
        },
        "required": ["source"],
    },
}


def check_data_requirements() -> bool:
    """Local CSV profiling has no external requirements."""
    return True


def data_profile_tool(
    source: str,
    sample_rows: int = 10,
    max_rows: int = 100_000,
) -> str:
    """Return a JSON profile for a local CSV/TSV source."""
    try:
        result = profile_data_source(
            source,
            sample_rows=sample_rows,
            max_rows=max_rows,
        )
        return tool_result(result)
    except DataSourceError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        logger.exception("data_profile tool failed: %s", exc)
        return tool_error(f"data_profile failed: {exc}")


def _handle_data_profile(args, **_kwargs):
    return data_profile_tool(
        source=args.get("source", ""),
        sample_rows=args.get("sample_rows", 10),
        max_rows=args.get("max_rows", 100000),
    )


registry.register(
    name="data_profile",
    toolset="data",
    schema=DATA_PROFILE_SCHEMA,
    handler=_handle_data_profile,
    check_fn=check_data_requirements,
    emoji="DATA",
    max_result_size_chars=200_000,
)

