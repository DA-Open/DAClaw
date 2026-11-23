"""Deterministic data cards over the task's authorized data sources."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

from ...contracts import ArtifactInput, AuthorizedCall, ErrorCode, ToolResult, ToolSpec, ToolStatus, canonical_json
from .data import DataConnector
from .files import _Base, _failed

DATA_PROFILE = ToolSpec(
    name="data.profile",
    description="Compute data-card statistics and quality findings for an authorized local dataset.",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string", "minLength": 1}},
        "required": ["path"],
        "additionalProperties": False,
    },
    read_only=True,
    side_effects=False,
    idempotent=True,
)


class DataProfileConnector(_Base):
    spec = DATA_PROFILE

    def __init__(self, connect: DataConnector, artifacts):
        self._connect = connect
        self._artifacts = artifacts

    async def execute(self, call: AuthorizedCall) -> ToolResult:
        try:
            profile = await _profile(call, self._connect)
        except TimeoutError:
            return _failed(ErrorCode.TOOL_ERROR, "data profile exceeded 30 seconds")
        except ValueError as exc:
            return _failed(ErrorCode.INVALID_REQUEST, str(exc))
        data = {"action_id": call.action_id, "profile": profile}
        text = canonical_json(data)
        if len(text) <= 4000:
            return ToolResult(status=ToolStatus.SUCCEEDED, data=data, summary=text)
        ref = await self._artifacts.put(ArtifactInput(
            scope_id=call.scope_id, task_id=call.task_id, name=f"data-result-{call.action_id}",
            media_type="application/json", content=text.encode("utf-8"),
        ))
        return ToolResult(
            status=ToolStatus.SUCCEEDED,
            data=data,
            artifact_refs=[ref.id],
            summary=canonical_json({
                "action_id": call.action_id,
                "data_ref": ref.id,
                "detail": "Read the complete result with artifacts.read before writing the card.",
            }),
        )

    async def replay(self, call: AuthorizedCall) -> dict:
        # Use a fresh session so replay sees file changes even if the execution
        # cached a registered table in its attempt-local session.
        connect = DataConnector(self._artifacts)
        try:
            return await _profile(call, connect)
        finally:
            connect.close()


async def _profile(call: AuthorizedCall, connect: DataConnector) -> dict:
    from agent.data_agent.engine import DataEngine, _EXCEL_SUFFIXES, _SQLITE_SUFFIXES

    path = Path(call.args["path"])
    if path.suffix.lower() in _EXCEL_SUFFIXES | _SQLITE_SUFFIXES:
        # These formats are supported by the original data-card profiler.
        # They have no SQL connector in the shared data-access pack.
        return await _legacy_profile(path)
    connected = await connect.execute(call.model_copy(update={"tool": "data.connect"}))
    if connected.status != ToolStatus.SUCCEEDED:
        raise ValueError(connected.error.message if connected.error else "could not connect the source")
    assert connected.data is not None
    connection, views = connect.source(call, connected.data["table"])
    engine = DataEngine(restore_sources=False, connection=connection)
    return _statistics(engine, views)


async def _legacy_profile(path: Path, *, timeout: float = 30) -> dict:
    env = dict(os.environ)
    root = Path(__file__).resolve().parents[4]
    env["PYTHONPATH"] = os.pathsep.join((str(root / "src"), str(root)))
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "harness.tools.connectors.data_profile",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(canonical_json({"path": str(path)}).encode()), timeout
        )
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise ValueError(stderr.decode("utf-8", errors="replace")[-2000:])
    return json.loads(stdout)


def _profile_legacy_file(path: Path) -> dict:
    from agent.data_agent.engine import DataEngine

    engine = DataEngine(restore_sources=False)
    try:
        engine.con.execute("SET memory_limit='512MB'")
        engine.con.execute("SET threads=2")
        source = engine.connect_source(str(path.resolve(strict=True)), persist=False)
        engine.con.execute("SET enable_external_access=false")
        engine.con.execute("SET lock_configuration=true")
        return _statistics(engine, {table: table for table in source["tables"]})
    finally:
        engine.con.close()


def _statistics(engine, views: dict[str, str]) -> dict:
    if len(views) > 64:
        raise ValueError("data.profile supports at most 64 tables per source")
    tables, findings = [], []
    for table, view in sorted(views.items()):
        profile = engine.profile_table(view, sample_rows=0)
        if len(profile["columns"]) > 512:
            raise ValueError("data.profile supports at most 512 columns per table")
        profile["table"] = table
        profile.pop("sample_rows")
        row_count = profile["row_count"]
        profile["primary_key_candidates"] = []
        for name, stat in profile["column_stats"].items():
            stat.pop("semantic", None)
            if stat["null_count"] is None or stat["distinct_count"] is None:
                raise ValueError(f"could not compute statistics for {table}.{name}")
            stat["null_rate"] = stat["null_count"] / row_count if row_count else 0
            stat["unique_rate"] = stat["distinct_count"] / row_count if row_count else 0
            quoted = '"' + name.replace('"', '""') + '"'
            stat["top_values"] = engine.run_query(
                f'SELECT {quoted} AS value, COUNT(*) AS count FROM "{view}" GROUP BY 1 '
                'ORDER BY count DESC, CAST(value AS VARCHAR) NULLS LAST LIMIT 5',
                max_rows=5,
            ).rows
            if row_count and stat["null_count"] == 0 and stat["distinct_count"] == row_count:
                profile["primary_key_candidates"].append(name)
            if stat["null_count"]:
                findings.append({"kind": "null_values", "table": table, "column": name, "impact": 1, "urgency": 0})
            if name.lower() in ("id", f"{table.lower()}_id") and stat["distinct_count"] < row_count - stat["null_count"]:
                findings.append({
                    "kind": "duplicate_key_candidate", "table": table, "column": name, "impact": 2, "urgency": 1,
                })
        if row_count == 0:
            findings.append({"kind": "empty_table", "table": table, "column": "", "impact": 2, "urgency": 1})
        tables.append(profile)
    return {"tables": tables, "findings": findings}


if __name__ == "__main__":
    request = json.load(sys.stdin)
    print(canonical_json(_profile_legacy_file(Path(request["path"]))))
