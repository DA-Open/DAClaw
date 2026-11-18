"""Bounded read-only data operations in a disposable, isolated engine process."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

from ...contracts import ArtifactInput, ErrorCode, ToolResult, ToolSpec, ToolStatus, canonical_json
from .files import _Base, _failed

PATH_SCHEMA = {"path": {"type": "string", "minLength": 1}}
DATA_PROFILE = ToolSpec(name="data.profile", description="Compute data-card statistics and quality findings for all tables in an authorized local dataset.",
    input_schema={"type": "object", "properties": PATH_SCHEMA, "required": ["path"], "additionalProperties": False},
    read_only=True, side_effects=False, idempotent=True)
DATA_QUERY = ToolSpec(name="data.query_readonly", description="Run one read-only SELECT on an authorized local dataset, limited to 1000 returned rows and 30 seconds.",
    input_schema={"type": "object", "properties": {**PATH_SCHEMA, "sql": {"type": "string", "maxLength": 16000},
        "max_rows": {"type": "integer", "minimum": 1, "maximum": 1000}},
        "required": ["path", "sql"], "additionalProperties": False}, read_only=True, side_effects=False, idempotent=True)


async def run_data(tool, args, *, timeout=30):
    env = dict(os.environ)
    root = Path(__file__).resolve().parents[4]
    env["PYTHONPATH"] = os.pathsep.join((str(root / "src"), str(root)))
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "harness.tools.connectors.data",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(canonical_json({"tool": tool, "args": args}).encode()), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise ValueError(stderr.decode("utf-8", errors="replace")[-2000:])
    return json.loads(stdout)


class DataConnector(_Base):
    def __init__(self, spec, artifacts):
        self.spec = spec
        self.artifacts = artifacts

    async def execute(self, call):
        try:
            data = await run_data(self.spec.name, call.args)
        except asyncio.TimeoutError:
            return _failed(ErrorCode.TOOL_ERROR, "data operation exceeded 30 seconds")
        except ValueError as exc:
            return _failed(ErrorCode.INVALID_REQUEST, str(exc))
        data = {"action_id": call.action_id, **data}
        text = canonical_json(data)
        if len(text) <= 4000:
            return ToolResult(status=ToolStatus.SUCCEEDED, data=data, summary=text)
        ref = await self.artifacts.put(ArtifactInput(scope_id=call.scope_id, task_id=call.task_id,
            name=f"data-result-{call.action_id}", media_type="application/json", content=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")))
        return ToolResult(status=ToolStatus.SUCCEEDED, data=data, artifact_refs=[ref.id],
            summary=canonical_json({"action_id": call.action_id, "data_ref": ref.id,
                                    "detail": "Read the complete result with artifacts.read before writing the card."}))


def _compute(tool, args):
    import duckdb
    from agent.data_agent.engine import DataEngine

    path = Path(args["path"]).resolve(strict=True)
    # Loading the legacy user's persisted sources would cross subscription boundaries.
    engine = DataEngine(restore_sources=False)
    try:
        engine.con.execute("SET memory_limit='512MB'")
        engine.con.execute("SET threads=2")
        source = engine.connect_source(str(path), persist=False)
        # CSV/Parquet sources are lazy views. Keep only the authorized source
        # readable after disabling all other external file/network access.
        engine.con.execute("SET allowed_paths = ?", [[str(path)]])
        engine.con.execute("SET enable_external_access=false")
        engine.con.execute("SET lock_configuration=true")
        if tool == "data.query_readonly":
            statements = engine.con.extract_statements(args["sql"])
            if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                raise ValueError("only one SELECT statement is allowed")
            result = engine.run_query(args["sql"], max_rows=min(args.get("max_rows", 1000), 1000))
            return {"columns": result.columns, "rows": result.rows, "row_count": result.row_count}
        if tool != "data.profile":
            raise ValueError("unsupported data operation")
        if len(source["tables"]) > 64:
            raise ValueError("data.profile supports at most 64 tables per source")
        tables, findings = [], []
        for table in sorted(source["tables"]):
            profile = engine.profile_table(table, sample_rows=0)
            if len(profile["columns"]) > 512:
                raise ValueError("data.profile supports at most 512 columns per table")
            profile.pop("sample_rows")
            n = profile["row_count"]
            profile["primary_key_candidates"] = []
            for name, stat in profile["column_stats"].items():
                stat.pop("semantic", None)
                if stat["null_count"] is None or stat["distinct_count"] is None:
                    raise ValueError(f"could not compute statistics for {table}.{name}")
                stat["null_rate"] = stat["null_count"] / n if n else 0
                stat["unique_rate"] = stat["distinct_count"] / n if n else 0
                quoted = '"' + name.replace('"', '""') + '"'
                stat["top_values"] = engine.run_query(
                    f'SELECT {quoted} AS value, COUNT(*) AS count FROM "{table}" GROUP BY 1 '
                    'ORDER BY count DESC, CAST(value AS VARCHAR) NULLS LAST LIMIT 5', max_rows=5).rows
                if n and stat["null_count"] == 0 and stat["distinct_count"] == n:
                    profile["primary_key_candidates"].append(name)
                if stat["null_count"]:
                    findings.append({"kind": "null_values", "table": table, "column": name, "impact": 1, "urgency": 0})
                if name.lower() in ("id", f"{table.lower()}_id") and stat["distinct_count"] < n - stat["null_count"]:
                    findings.append({"kind": "duplicate_key_candidate", "table": table, "column": name, "impact": 2, "urgency": 1})
            if n == 0:
                findings.append({"kind": "empty_table", "table": table, "column": "", "impact": 2, "urgency": 1})
            tables.append(profile)
        return {"profile": {"tables": tables, "findings": findings}}
    finally:
        engine.con.close()


if __name__ == "__main__":
    request = json.load(sys.stdin)
    print(canonical_json(_compute(request["tool"], request["args"])))
