"""Re-run a baseline's SQL on the new bytes, before the model sees anything.

Called from the context builder, so the worker holds the lease and no storage
transaction is open. The result is deterministic: the model receives old and new
values, it is never asked to compute them.
"""
from __future__ import annotations

import csv
import io
import json
import logging

import duckdb
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from ..contracts import (
    AgentRole,
    ArtifactInput,
    Event,
    EventType,
    HarnessError,
    Task,
    new_id,
    utcnow,
)
from ..interfaces import ArtifactStore
from ..storage.repositories import Repositories
from ..tools.connectors.data import ReadonlySource, file_digest, open_readonly, run_readonly
from .memory import is_current
from .analysis_types import (
    AnalysisWatch,
    BaselineBody,
    RecomputeQuery,
    RecomputeResult,
    RecomputeThreshold,
)

log = logging.getLogger(__name__)

INLINE_ROWS = 100


class Recomputer:
    def __init__(self, repos: Repositories, memories, artifacts: ArtifactStore, policy, *,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self._repos = repos
        self._memories = memories
        self._artifacts = artifacts
        self._policy = policy
        self._clock = clock

    async def ensure(self, task: Task) -> str | None:
        """Make sure this task has its recompute artifact; return its id or None."""
        existing = await self._existing(task)
        if existing is not None:
            return existing
        record = await self._decision(task)
        if record is None:
            return None
        try:
            result, refs = await self._run(task, record)
        except (HarnessError, OSError, ValueError, KeyError, duckdb.Error) as exc:
            result, refs = RecomputeResult(new_sha256="", error=str(exc)), []
        stored = await self._store(task, result, refs)
        now = self._clock()
        await self._repos.events.append(Event(
            scope_id=task.scope_id,
            task_id=task.id,
            type=EventType.RECOMPUTE_RECORDED,
            payload={"recompute_ref": stored, "new_sha256": result.new_sha256,
                     "query_refs": [q.query_ref for q in result.queries], "error": result.error},
            occurred_at=now,
            received_at=now,
        ))
        return stored

    async def _existing(self, task: Task) -> str | None:
        for artifact in await self._repos.artifacts.list_for_task(task.scope_id, task.id):
            if artifact.name == "recompute":
                return artifact.id
        return None

    async def _decision(self, task: Task):
        if not task.subscription_id:
            return None
        records = await self._repos.subscriptions.decisions(task.scope_id, task.subscription_id)
        return next((r for r in reversed(records) if r.task_id == task.id), None)

    async def _run(self, task: Task, record):
        snapshot = record.input_snapshot
        memory_id, _, version = snapshot["baseline_ref"].removeprefix("memory:").partition("@")
        memory, text = await self._memories.read(task.scope_id, "harness", memory_id)
        if not memory.id == memory_id or str(memory.version) != version or not is_current(memory, self._clock()):
            raise ValueError("baseline is no longer current")
        sub = await self._repos.subscriptions.get(task.scope_id, task.subscription_id)
        if not _active(sub, self._clock()):
            raise ValueError("subscription is no longer active")
        body = BaselineBody.model_validate_json(text)
        paths = [Path(source.path).expanduser().resolve() for source in body.sources]
        if not all(self._policy.check_path(path, scope_id=task.scope_id, role=AgentRole.PROACTIVE).allowed
                   for path in paths):
            raise ValueError("resource permission revoked")
        for path in paths:
            if not path.is_file():
                raise ValueError(f"source is not readable: {path}")
        watched = next((path for path in paths if str(path) == snapshot["path"]), paths[0])
        if file_digest(watched) != snapshot["new_sha256"]:
            raise ValueError("source changed again before recompute")
        con = open_readonly([
            ReadonlySource(path=Path(source.path), kind=source.kind, primary=source.table, tables=source.tables)
            for source in body.sources
        ])
        try:
            results = [run_readonly(con, query.sql) for query in body.queries]
        finally:
            con.close()
        watch = (record.input_snapshot or {}).get("watch")
        if watch:
            index = AnalysisWatch.model_validate(watch).query_index
            rows = len(results[index].rows) if index < len(results) else 0
            if rows != 1:
                # A watched number must still be one number; otherwise the baseline
                # cannot be compared and the user has to hear about it.
                raise ValueError(f"watched query returned {rows} rows; a watch needs exactly one")
        queries, refs = [], []
        for index, (query, result) in enumerate(zip(body.queries, results)):
            artifact_id = await self._evidence(task, query.sql, result, snapshot["baseline_ref"])
            refs.append(artifact_id)
            queries.append(RecomputeQuery(
                query_index=index, query_ref=f"query:{artifact_id}", old_values=query.values,
                new_values=[[_plain(value) for value in row] for row in result.rows[:INLINE_ROWS]],
                row_count=result.row_count,
            ))
        threshold = _threshold(record, body, queries)
        return RecomputeResult(new_sha256=snapshot["new_sha256"], queries=queries, threshold=threshold), refs

    async def _evidence(self, task: Task, sql: str, result, baseline_ref: str) -> str:
        query_id = new_id("q")
        csv_ref = await self._artifacts.put(ArtifactInput(
            scope_id=task.scope_id, task_id=task.id, name=f"query/{query_id}.csv",
            media_type="text/csv", content=_csv(result).encode("utf-8"), source_refs=[baseline_ref],
        ))
        evidence = {
            "query_id": query_id,
            "sql": sql,
            "columns": result.columns,
            "row_count": result.row_count,
            "row_count_capped": result.capped,
            "csv_ref": csv_ref.id,
            "created_at": self._clock().isoformat(),
        }
        stored = await self._artifacts.put(ArtifactInput(
            scope_id=task.scope_id, task_id=task.id, name=f"query/{query_id}.json",
            media_type="application/json", content=json.dumps(evidence, ensure_ascii=False).encode("utf-8"),
            source_refs=[csv_ref.id],
        ))
        return stored.id

    async def _store(self, task: Task, result: RecomputeResult, refs: list[str]) -> str:
        stored = await self._artifacts.put(ArtifactInput(
            scope_id=task.scope_id, task_id=task.id, name="recompute", media_type="application/json",
            content=result.model_dump_json().encode("utf-8"), source_refs=refs,
        ))
        return stored.id


def _threshold(record, body: BaselineBody, queries: list[RecomputeQuery]) -> RecomputeThreshold | None:
    watch = (record.input_snapshot or {}).get("watch")
    if not watch:
        return None
    spec = AnalysisWatch.model_validate(watch)
    query = next((q for q in queries if q.query_index == spec.query_index), None)
    if query is None:
        return None
    columns = body.queries[spec.query_index].columns if spec.query_index < len(body.queries) else []
    if spec.column not in columns or not query.new_values:
        return None
    index = columns.index(spec.column)
    old = _float(query.old_values[0][index]) if query.old_values and len(query.old_values[0]) > index else None
    new = _float(query.new_values[0][index]) if len(query.new_values[0]) > index else None
    if old is None or new is None:
        return None
    delta = new - old
    relative = (delta / old) if old else None
    return RecomputeThreshold(
        query_index=spec.query_index, column=spec.column, old=old, new=new, delta=delta,
        relative_change=relative,
        exceeds_threshold=relative is not None and abs(relative) >= spec.min_relative_change,
    )


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _plain(value):
    return value.isoformat() if isinstance(value, (datetime, date)) else value


def _csv(result) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(result.columns)
    for row in result.rows:
        writer.writerow(["" if value is None else value for value in row])
    return buffer.getvalue()


def _active(sub, now) -> bool:
    from ..contracts import SubscriptionStatus

    return sub.status is SubscriptionStatus.ACTIVE and (sub.expires_at is None or sub.expires_at > now)


__all__ = ["Recomputer"]
