"""Turn a finished data-analysis task into an ANALYSIS baseline.

Everything here is read from what the task already produced: the verification
record names the queries, the artifacts hold their SQL and values, the connect
actions hold the bytes that were read. No model call and no SQL runs, so the
answer has already reached the user before this starts.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ..contracts import (
    ActionStatus,
    AgentRole,
    HarnessError,
    MemoryKind,
    MemoryStatus,
    Task,
    TaskOutcome,
    TaskStatus,
    utcnow,
)
from ..interfaces import ArtifactStore
from ..results.verifier import _norm, markdown_headings
from ..storage.repositories import Repositories

log = logging.getLogger(__name__)

INLINE_VALUES = 20
ANSWER_CHARS = 2000
SUMMARY_CHARS = 300


class BaselineWriter:
    """AfterCommit hook: write one ANALYSIS memory per successful analysis task."""

    def __init__(self, repos: Repositories, memories, artifacts: ArtifactStore, *,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self._repos = repos
        self._memories = memories
        self._artifacts = artifacts
        self._clock = clock

    async def __call__(self, task: Task, outcome: TaskOutcome) -> None:
        try:
            await self.write(task, outcome)
        except (HarnessError, OSError, ValueError) as exc:
            log.warning("baseline for task %s skipped: %s", task.id, exc)

    async def write(self, task: Task, outcome: TaskOutcome) -> None:
        if not task.capability_ref.startswith("data_analysis@") or task.agent_role is not AgentRole.MAIN:
            return
        if outcome.status is not TaskStatus.SUCCEEDED or outcome.verification_ref is None:
            return
        queries = await self._queries(task, outcome)
        if not queries:
            return
        sources = await self._sources(task)
        if sources is None:
            return
        body = json.dumps({
            "task_id": task.id,
            "actor_id": task.actor_id,
            "question": task.goal,
            "sources": sources,
            "queries": queries,
            "answer": await self._answer(task, outcome),
        }, ensure_ascii=False, sort_keys=True)
        tables = [label for source in sources for label in source["tables"]]
        tags = list(dict.fromkeys([*tables, *[source["path"].rsplit("/", 1)[-1].split(".")[0] for source in sources]]))
        await self._memories.remember(
            task.scope_id,
            "harness",
            MemoryKind.ANALYSIS,
            body,
            status=MemoryStatus.CONFIRMED,
            summary=task.goal[:SUMMARY_CHARS],
            tags=tags[:16],
            source_ref=f"task:{task.id}",
            subject_ref=sources[0]["path"],
            expires_at=None,
        )

    async def _queries(self, task: Task, outcome: TaskOutcome) -> list[dict]:
        verification = await self._repos.verifications.get(outcome.verification_ref)
        check = next((c for c in verification.checks if c.id == "query_evidence"), None)
        refs = list(check.evidence_refs) if check is not None else []
        found = []
        for ref in refs:
            evidence = json.loads((await self._artifacts.read(ref, self._actor(task))).content)
            csv = (await self._artifacts.read(evidence["csv_ref"], self._actor(task))).content.decode("utf-8")
            rows = [line.split(",") for line in csv.splitlines()[1:1 + INLINE_VALUES]]
            found.append({
                "query_ref": ref,
                "sql": evidence["sql"],
                "columns": evidence["columns"],
                "row_count": evidence["row_count"],
                "row_count_capped": evidence.get("row_count_capped", False),
                "values": rows,
            })
        return found

    async def _sources(self, task: Task) -> list[dict] | None:
        sources = []
        for action in await self._repos.actions.list_for_task(task.id):
            if action.tool != "data.connect" or action.status is not ActionStatus.SUCCEEDED:
                continue
            if not action.args_ref or not action.result_ref:
                continue
            args = json.loads((await self._artifacts.read(action.args_ref, self._actor(task))).content)
            result = json.loads((await self._artifacts.read(action.result_ref, self._actor(task))).content)
            data = result.get("data") or {}
            if not data.get("sha256") or not data.get("tables"):
                log.warning("task %s connected %s before digests were recorded", task.id, args.get("path"))
                return None
            sources.append({
                "path": args["path"],
                "sha256": data["sha256"],
                "size_bytes": data["size_bytes"],
                "kind": Path(args["path"]).suffix.lower().lstrip("."),
                "table": data["table"],
                "tables": data["tables"],
                "row_count": data["row_count"],
            })
        return sources

    async def _answer(self, task: Task, outcome: TaskOutcome) -> str:
        text = (await self._artifacts.read(outcome.result_ref, self._actor(task))).content.decode("utf-8")
        inside, lines = False, []
        for line in text.splitlines():
            heading = markdown_headings(line)
            if heading:
                inside = _norm(heading[0]) == "answer"
                continue
            if inside:
                lines.append(line)
        return "\n".join(lines).strip()[:ANSWER_CHARS]

    def _actor(self, task: Task):
        from ..contracts import Actor

        return Actor(actor_id="harness", scope_id=task.scope_id)


__all__ = ["BaselineWriter"]
