"""Checkers beyond the document ones (registered in results/verifier.py).

Each checker has the same signature as the document checkers: it receives the
check spec and a CheckContext, and returns a CheckResult.
"""

from __future__ import annotations

import json
import re

from ...contracts import CheckResult, HarnessError, NotFound, VerificationStatus

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
    """Every cited evidence reference is a stored query, and the SQL is a read.

    This is the machine-checkable half of "numbers must come from real queries":
    it cannot prove the prose used the number, but it proves the cited evidence
    exists, belongs to this task's scope, carries the SQL and that the SQL only
    reads. A result with no evidence fails rather than passing silently.
    """
    refs = list(ctx.candidate.evidence_refs)
    if not refs:
        return _result(spec.id, VerificationStatus.FAILED, "the result cites no query evidence")
    problems: list[str] = []
    evidence: list[str] = []
    for ref in refs:
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
