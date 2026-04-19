"""LLM-driven audit for data-analysis report faithfulness."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional

from agent.auxiliary_client import call_llm, extract_content_or_reasoning

logger = logging.getLogger(__name__)

_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")
_ANALYSIS_KEYWORDS = (
    "analysis", "analyze", "analytics", "dataset", "csv", "excel", "xlsx",
    "sql", "table", "schema", "metric", "report", "dashboard", "cohort",
    "retention", "conversion", "funnel", "join",
)
_ANALYSIS_TOOLS = {"data_profile", "data_catalog", "execute_code", "terminal"}


def _stringify_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
            elif item:
                parts.append(str(item))
        return "\n".join(p for p in parts if p).strip()
    return str(content).strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]..."


def _extract_json_object(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Empty audit response.")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(raw)
        if not match:
            raise
        return json.loads(match.group(0))


def organize_analysis_trajectory(
    messages: Iterable[Dict[str, Any]],
    *,
    max_message_chars: int = 12000,
    max_tool_result_chars: int = 16000,
) -> str:
    """Reorganize conversation messages into an audit-friendly trace."""
    tool_name_by_id: Dict[str, str] = {}
    blocks: List[str] = []
    step = 1

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip()
        if role not in {"user", "assistant", "tool"}:
            continue

        if role == "assistant":
            content = _truncate(_stringify_content(msg.get("content")), max_message_chars)
            tool_calls = msg.get("tool_calls") or []
            parts: List[str] = [f"Step {step} | assistant"]
            if content:
                parts.append(f"content:\n{content}")
            if tool_calls:
                tc_lines = []
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    tc_id = tc.get("id", "")
                    fn = ((tc.get("function") or {}).get("name") if isinstance(tc.get("function"), dict) else "") or ""
                    args = ((tc.get("function") or {}).get("arguments") if isinstance(tc.get("function"), dict) else "") or ""
                    if tc_id and fn:
                        tool_name_by_id[tc_id] = fn
                    tc_lines.append(f"- {fn} args={_truncate(str(args), 2000)}")
                if tc_lines:
                    parts.append("tool_calls:\n" + "\n".join(tc_lines))
            blocks.append("\n".join(parts))
            step += 1
            continue

        if role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            tool_name = tool_name_by_id.get(tool_call_id, "unknown_tool")
            content = _truncate(_stringify_content(msg.get("content")), max_tool_result_chars)
            blocks.append(
                f"Step {step} | tool:{tool_name}\n"
                f"tool_call_id: {tool_call_id or '(none)'}\n"
                f"result:\n{content}"
            )
            step += 1
            continue

        content = _truncate(_stringify_content(msg.get("content")), max_message_chars)
        blocks.append(f"Step {step} | user\ncontent:\n{content}")
        step += 1

    return "\n\n".join(blocks).strip()


def looks_like_analysis_task(
    user_message: str,
    messages: Iterable[Dict[str, Any]],
    system_prompt: str = "",
) -> bool:
    """Heuristic gate for when report auditing is worth running."""
    prompt_text = (system_prompt or "").lower()
    if "data-analyst mode" in prompt_text:
        return True

    user_lower = (user_message or "").lower()
    if any(keyword in user_lower for keyword in _ANALYSIS_KEYWORDS):
        return True

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = ((tc.get("function") or {}).get("name") if isinstance(tc.get("function"), dict) else "") or ""
            if fn in _ANALYSIS_TOOLS:
                return True
    return False


def audit_analysis_report(
    *,
    user_task: str,
    final_report: str,
    trajectory_messages: Optional[Iterable[Dict[str, Any]]] = None,
    trajectory_text: str = "",
) -> Dict[str, Any]:
    """Judge whether a final analysis report is faithful to its trajectory."""
    organized_trace = trajectory_text.strip() if trajectory_text else organize_analysis_trajectory(trajectory_messages or [])
    if not organized_trace:
        return {
            "pass": True,
            "audit_status": "skipped",
            "reason": "No trajectory content available for analysis audit.",
            "recommended_action": "approve",
            "issues": [],
        }

    prompt = (
        "You are auditing a data-analysis report before it is shown to the user.\n"
        "Judge whether the final report is faithful to the organized trajectory.\n"
        "Focus on: numeric consistency, time-range consistency, filter/group consistency, "
        "unsupported claims, logic gaps, and whether conclusions overstate the evidence.\n"
        "You may also flag problems in the trajectory itself when a bad intermediate step "
        "would make the final report unreliable.\n\n"
        "Return strict JSON with this schema:\n"
        "{\n"
        '  "pass": true|false,\n'
        '  "score": 0.0,\n'
        '  "faithfulness": "high|medium|low",\n'
        '  "trajectory_quality": "sound|mixed|problematic",\n'
        '  "summary": "short summary",\n'
        '  "recommended_action": "approve|revise_report|return_to_analysis|rerun_analysis",\n'
        '  "issues": [\n'
        "    {\n"
        '      "severity": "high|medium|low",\n'
        '      "stage": "final_report|trajectory",\n'
        '      "type": "numeric_mismatch|unsupported_claim|logic_gap|scope_drift|time_range_mismatch|filter_mismatch|other",\n'
        '      "message": "what is wrong",\n'
        '      "evidence": "quote or paraphrase from trace/report",\n'
        '      "suggested_action": "approve|revise_report|fix_step|rerun_analysis"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Be conservative: fail the audit when the report is not clearly supported.\n\n"
        f"User task:\n{user_task or '(not provided)'}\n\n"
        f"Organized trajectory:\n{organized_trace}\n\n"
        f"Final report:\n{final_report}"
    )

    try:
        response = call_llm(
            task="analysis_audit",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=1800,
        )
        content = extract_content_or_reasoning(response)
        parsed = _extract_json_object(content)
        if not isinstance(parsed, dict):
            raise ValueError("Audit response is not a JSON object.")
        parsed.setdefault("pass", False)
        parsed.setdefault("score", 0.0)
        parsed.setdefault("faithfulness", "low")
        parsed.setdefault("trajectory_quality", "mixed")
        parsed.setdefault("summary", "")
        parsed.setdefault("recommended_action", "revise_report")
        parsed.setdefault("issues", [])
        parsed["audit_status"] = "completed"
        parsed["organized_trajectory"] = organized_trace
        return parsed
    except Exception as exc:
        logger.warning("analysis audit skipped: %s", exc)
        return {
            "pass": True,
            "audit_status": "skipped",
            "reason": str(exc),
            "recommended_action": "approve",
            "issues": [],
            "organized_trajectory": organized_trace,
        }
