#!/usr/bin/env python3
"""LLM-driven analysis report audit tool."""

from __future__ import annotations

import logging
from typing import Any, Dict

from agent.analysis_audit import audit_analysis_report
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)


ANALYSIS_AUDIT_SCHEMA: Dict[str, Any] = {
    "name": "analysis_audit",
    "description": (
        "Audit a data-analysis report against its organized trajectory before "
        "delivery. Use this to judge faithfulness, detect numeric drift or "
        "unsupported claims, and decide whether the report should be revised "
        "or the analysis should be continued."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "user_task": {
                "type": "string",
                "description": "Original user task or analysis goal.",
            },
            "final_report": {
                "type": "string",
                "description": "The candidate final report to audit.",
            },
            "trajectory_text": {
                "type": "string",
                "description": "Organized analysis trajectory text containing key steps and tool outputs.",
            },
        },
        "required": ["final_report", "trajectory_text"],
    },
}


def check_analysis_audit_requirements() -> bool:
    return True


def analysis_audit_tool(*, user_task: str = "", final_report: str = "", trajectory_text: str = "") -> str:
    if not final_report.strip():
        return tool_error("final_report is required.")
    if not trajectory_text.strip():
        return tool_error("trajectory_text is required.")
    try:
        result = audit_analysis_report(
            user_task=user_task,
            final_report=final_report,
            trajectory_text=trajectory_text,
        )
        return tool_result(result)
    except Exception as exc:
        logger.exception("analysis_audit tool failed: %s", exc)
        return tool_error(f"analysis_audit failed: {exc}")


def _handle_analysis_audit(args, **_kwargs):
    return analysis_audit_tool(
        user_task=args.get("user_task", ""),
        final_report=args.get("final_report", ""),
        trajectory_text=args.get("trajectory_text", ""),
    )


registry.register(
    name="analysis_audit",
    toolset="data",
    schema=ANALYSIS_AUDIT_SCHEMA,
    handler=_handle_analysis_audit,
    check_fn=check_analysis_audit_requirements,
    emoji="DATA",
    max_result_size_chars=200_000,
)
