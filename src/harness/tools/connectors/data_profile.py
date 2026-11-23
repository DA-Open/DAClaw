"""Deterministic data cards over the task's authorized data sources."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from ...contracts import ArtifactInput, AuthorizedCall, ErrorCode, ToolResult, ToolSpec, ToolStatus, canonical_json
from ...interfaces import ArtifactStore
from .data import DataConnector
from .files import _Base, _failed

PROFILE_TIMEOUT_SECONDS = 30
DATA_PROFILE = ToolSpec(
    name="data.profile",
    description=(
        "Compute data-card statistics and quality findings for an authorized local dataset. "
        "To run SQL afterward, connect the file separately with data.connect."
    ),
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

    def __init__(self, artifacts: ArtifactStore):
        self._artifacts = artifacts

    async def execute(self, call: AuthorizedCall) -> ToolResult:
        try:
            profile = await _profile_dataset(Path(call.args["path"]))
        except TimeoutError:
            return _failed(ErrorCode.TOOL_ERROR, f"data profile exceeded {PROFILE_TIMEOUT_SECONDS:g} seconds")
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
        return await _profile_dataset(Path(call.args["path"]))


async def _profile_dataset(path: Path) -> dict:
    # Both data loading and statistics can block inside native code. The
    # disposable process keeps heartbeat/cancellation running and can be killed.
    env = dict(os.environ)
    package_root = Path(__file__).resolve().parents[3]
    import_roots = [package_root]
    # A source checkout has harness under src and agent alongside src. An
    # installed distribution has both packages in the same import root.
    if (package_root.parent / "agent" / "data_agent" / "engine.py").is_file():
        import_roots.append(package_root.parent)
    env["PYTHONPATH"] = os.pathsep.join(map(str, import_roots))
    request = canonical_json({"path": str(path.resolve())}).encode()
    with TemporaryDirectory(prefix="daraos-profile-") as scratch:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "harness.tools.connectors.data_profile",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=scratch,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(request), PROFILE_TIMEOUT_SECONDS)
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.communicate()
            raise
    if process.returncode:
        raise ValueError(stderr.decode("utf-8", errors="replace")[-2000:])
    return json.loads(stdout)


async def _profile_file(path: Path) -> dict:
    from agent.data_agent.engine import DataEngine, _EXCEL_SUFFIXES, _SQLITE_SUFFIXES

    if path.suffix.lower() in _EXCEL_SUFFIXES | _SQLITE_SUFFIXES | {".txt", ".pq"}:
        return _profile_legacy_file(path)

    from ...results.artifacts import FileArtifactStore
    from ...storage.memory import InMemoryRepositories

    # This child only connects the authorized source. No query artifacts or
    # attempt-local aliases are visible to it. Any spill files are disposable.
    connect = DataConnector(FileArtifactStore(Path.cwd(), InMemoryRepositories().artifacts))
    try:
        call = AuthorizedCall(
            scope_id="profile", task_id="profile", attempt_id="profile", action_id="profile",
            tool="data.connect", args={"path": str(path)}, logical_key="profile",
        )
        connected = await connect.execute(call)
        if connected.status != ToolStatus.SUCCEEDED:
            raise ValueError(connected.error.message if connected.error else "could not connect the source")
        assert connected.data is not None
        connection, views = connect.source(call, connected.data["table"])
        return _statistics(DataEngine(restore_sources=False, connection=connection), views)
    finally:
        connect.close()


def _profile_legacy_file(path: Path) -> dict:
    from agent.data_agent.engine import DataEngine

    engine = DataEngine(restore_sources=False)
    try:
        engine.con.execute("SET memory_limit='512MB'")
        engine.con.execute("SET threads=2")
        path = path.resolve(strict=True)
        source = engine.connect_source(str(path), persist=False)
        engine.con.execute("SET allowed_paths = ?", [[str(path)]])
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
        quoted_view = '"' + view.replace('"', '""') + '"'
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
                f"SELECT {quoted} AS value, COUNT(*) AS count FROM {quoted_view} GROUP BY 1 "
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
    print(canonical_json(asyncio.run(_profile_file(Path(request["path"])))))
