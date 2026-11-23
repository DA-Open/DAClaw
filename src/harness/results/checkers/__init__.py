"""Checkers beyond the document ones (registered in results/verifier.py).

Each checker has the same signature as the document checkers: it receives the
check spec and a CheckContext, and returns a CheckResult.
"""

from __future__ import annotations

import json
import re

from ...contracts import ActionStatus, CheckResult, HarnessError, NotFound, ToolResult, ToolStatus, VerificationStatus

_READ_ONLY = re.compile(r"^\s*(?:select|with)\b", re.IGNORECASE)
_COMMENT = re.compile(r"^\s*(?:--[^\n]*\n|/\*.*?\*/\s*)", re.DOTALL)


def _result(spec_id: str, status: VerificationStatus, detail: str = "", evidence: list[str] | None = None) -> CheckResult:
    return CheckResult(id=spec_id, status=status, detail=detail, evidence_refs=evidence or [])


def _strip_comments(sql: str) -> str:
    text = sql
    while True:
        match = _COMMENT.match(text)
        if not match:
            return text
        text = text[match.end():]


async def query_evidence(spec, ctx) -> CheckResult:
    """Require each citation to be the record returned by a successful query on this task."""
    refs = list(ctx.candidate.evidence_refs)
    if not refs:
        return _result(spec.id, VerificationStatus.FAILED, "the result cites no query evidence")
    problems: list[str] = []
    evidence: list[str] = []
    returned_refs: set[str] = set()
    for action in ctx.actions:
        if (action.task_id != ctx.task.id or action.tool != "data.query" or not action.read_only
                or action.status != ActionStatus.SUCCEEDED or not action.result_ref):
            continue
        stored = await ctx.artifacts.read(action.result_ref, ctx.actor)
        result = ToolResult.model_validate_json(stored.content)
        query_ref = (result.data or {}).get("query_ref")
        if result.status == ToolStatus.SUCCEEDED and isinstance(query_ref, str) and query_ref in result.artifact_refs:
            returned_refs.add(query_ref)
    for ref in refs:
        if ref not in returned_refs:
            problems.append(f"evidence {ref} was not returned by a successful data.query on this task")
            continue
        try:
            content = await ctx.artifacts.read(ref, ctx.actor)
        except NotFound:
            problems.append(f"evidence {ref} does not exist in this scope")
            continue
        except HarnessError as exc:
            problems.append(f"evidence {ref} cannot be read: {exc.message}")
            continue
        if not content.ref.media_type.startswith(("application/json", "text/")):
            problems.append(f"evidence {ref} is {content.ref.media_type}, not a stored query")
            continue
        try:
            payload = json.loads(content.content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            problems.append(f"evidence {ref} is not JSON")
            continue
        if not isinstance(payload, dict):
            problems.append(f"evidence {ref} is not a query record")
            continue
        sql = str(payload.get("sql") or "").strip()
        if not sql:
            problems.append(f"evidence {ref} carries no SQL")
            continue
        if not _READ_ONLY.match(_strip_comments(sql)):
            problems.append(f"evidence {ref} is not a read: {sql[:60]!r}")
            continue
        row_count = payload.get("row_count")
        if not isinstance(row_count, int) or row_count < 0:
            problems.append(f"evidence {ref} has no row_count")
            continue
        evidence.append(ref)
    if problems:
        return _result(spec.id, VerificationStatus.FAILED, "; ".join(problems), evidence)
    return _result(spec.id, VerificationStatus.PASSED, f"{len(evidence)} query record(s) cited", evidence)


__all__ = ["query_evidence"]
