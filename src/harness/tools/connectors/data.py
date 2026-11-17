"""Data connectors: connect local tabular files and query them with read-only SQL.

Two tools:

* `data.connect` registers one CSV / TSV / Parquet / DuckDB file as a view in
  this task's DuckDB session and reports its columns.
* `data.query` runs one SELECT (or WITH ... SELECT) over those views, returns a
  bounded preview, and stores the full result plus the SQL as artifacts so a
  later step (or the Verifier) can page through it and re-check the numbers.

Scope and safety:

* The path is checked by ToolGateway before this runs (`data.connect` is in
  `PATH_ARGS`); the connector re-checks that it is a regular file.
* A session is per (scope_id, task_id): one task cannot see another task's
  sources, and a worker restart simply loses the views (the model reconnects).
* SQL must parse as exactly one SELECT and may not name a file: file-reading
  table functions (`read_csv`, `read_parquet`, `read_text`, `*_scan` ...),
  extension loading and path literals are refused before the query runs. The
  sources are the views `data.connect` registered, nothing else.
* At most `MAX_ROWS` rows are materialised; the reported `row_count` is the
  exact count when the query allows it, and `row_count_capped` says so.

Artifacts written by `data.query` (in the task's artifact area):

* `query/<id>.json` -- SQL, columns, row_count and the first rows. This is the
  evidence reference the result should cite.
* `query/<id>.csv` -- up to `MAX_ROWS` rows, readable with `artifacts.read`.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from ...contracts import (
    ActionRef,
    ArtifactInput,
    ArtifactRef,
    AuthorizedCall,
    ErrorCode,
    ReconcileResult,
    TaskError,
    ToolResult,
    ToolSpec,
    ToolStatus,
    new_id,
    utcnow,
)
from ...interfaces import ArtifactStore

INLINE_ROWS = 20
MAX_ROWS = 10_000
INLINE_CHARS = 6_000
MAX_SQL_CHARS = 20_000
MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB, a sanity bound only

_SUFFIXES = {".csv": "csv", ".tsv": "tsv", ".parquet": "parquet", ".duckdb": "duckdb", ".ddb": "duckdb"}
# Keywords that parse as SELECT in DuckDB but are not reads (PRAGMA can change
# settings such as enable_external_access). Refused before the query runs.
_NOT_A_READ = re.compile(
    r"^\s*(?:pragma|call|set|attach|detach|copy|export|import|install|load|use|begin|commit|rollback)\b",
    re.IGNORECASE,
)
_COMMENT = re.compile(r"^\s*(?:--[^\n]*\n|/\*.*?\*/\s*)", re.DOTALL)
# DuckDB can read files from SQL. The task may query the views data.connect
# registered, nothing else, so these names and path literals are refused before
# the statement runs (the connector never opens a file on the model's behalf).
_FILE_FUNCS = re.compile(
    r"\b(?:read_csv|read_csv_auto|read_parquet|read_json|read_json_auto|read_ndjson|read_text|read_blob|"
    r"read_arrow|parquet_scan|csv_scan|json_scan|ndjson_scan|sqlite_scan|postgres_scan|mysql_scan|"
    r"iceberg_scan|delta_scan|glob|sniff_csv|parquet_metadata|parquet_schema|query_table)\b",
    re.IGNORECASE,
)
_PATH_LITERAL = re.compile(
    r"'(?:[^']|'')*(?:[/\\~]|\.(?:csv|tsv|parquet|json|jsonl|ndjson|db|duckdb|xlsx|gz|zst))(?:[^']|'')*'",
    re.IGNORECASE,
)


def _failed(code: ErrorCode, message: str) -> ToolResult:
    return ToolResult(status=ToolStatus.FAILED, error=TaskError(code=code, message=message))


def _int_arg(args: dict[str, Any], name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(args.get(name, default))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    return max(low, min(value, high))


def _identifier(text: str) -> str:
    """A safe view name: ascii letters, digits and underscore, not starting with a digit."""
    name = re.sub(r"[^0-9a-zA-Z_]+", "_", text).strip("_").lower() or "data"
    if name[0].isdigit():
        name = "t_" + name
    return name[:48]


def _literal(path: Path) -> str:
    """A single-quoted SQL string literal; DuckDB doubles the quote."""
    return "'" + str(path).replace("'", "''") + "'"


@dataclass
class _Session:
    """One task's DuckDB connection and the views registered in it."""

    connection: duckdb.DuckDBPyConnection
    views: dict[str, dict[str, Any]] = field(default_factory=dict)


DATA_CONNECT = ToolSpec(
    name="data.connect",
    description=(
        "Register a local CSV, TSV, Parquet or DuckDB file as a table this task can query. "
        "Returns the table name and columns; use data.query to read rows."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path of the file to connect."},
            "name": {"type": "string", "description": "Optional table name; default from the file name."},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    read_only=True,
    side_effects=False,
    idempotent=True,
)

DATA_QUERY = ToolSpec(
    name="data.query",
    description=(
        "Run one read-only SQL query (SELECT or WITH ... SELECT) over the tables connected in this task. "
        "Returns columns, the exact row count and a preview; the full result and the SQL are stored as "
        "artifacts. Cite the returned query_ref in the result as evidence."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "sql": {"type": "string", "minLength": 1, "maxLength": MAX_SQL_CHARS},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": f"Preview rows to return (default {INLINE_ROWS}).",
            },
        },
        "required": ["sql"],
        "additionalProperties": False,
    },
    read_only=True,
    side_effects=False,
    idempotent=True,
)


class _Base:
    spec: ToolSpec

    async def reconcile(self, action: ActionRef) -> ReconcileResult:
        # Local and synchronous: a call either finished or raised.
        return ReconcileResult(status="unsupported", detail=f"{self.spec.name} has no remote state")


class DataConnector(_Base):
    """`data.connect`; the query connector shares this connector's sessions."""

    spec = DATA_CONNECT

    def __init__(self, artifacts: ArtifactStore) -> None:
        self._artifacts = artifacts
        self._sessions: dict[tuple[str, str], _Session] = {}

    # -- session -----------------------------------------------------------

    def _session(self, call: AuthorizedCall) -> _Session:
        key = (call.scope_id, call.task_id)
        session = self._sessions.get(key)
        if session is None:
            con = duckdb.connect(":memory:")
            con.execute("SET memory_limit='2GB'")
            # File access stays on so the registered views can lazily scan their
            # source; the SQL gate (_check_select) refuses any other path.
            session = _Session(connection=con)
            self._sessions[key] = session
        return session

    def close(self) -> None:
        for session in self._sessions.values():
            try:
                session.connection.close()
            except Exception:  # pragma: no cover - best effort
                pass
        self._sessions.clear()

    # -- one entry point for both tools ------------------------------------

    async def execute(self, call: AuthorizedCall) -> ToolResult:
        if call.tool == DATA_QUERY.name:
            return await self._query(call)
        return await self._connect(call)

    async def _connect(self, call: AuthorizedCall) -> ToolResult:
        path = Path(str(call.args.get("path", ""))).expanduser()
        if not path.is_file():
            return _failed(ErrorCode.NOT_FOUND, f"no such file: {call.args.get('path')}")
        kind = _SUFFIXES.get(path.suffix.lower())
        if kind is None:
            return _failed(
                ErrorCode.INVALID_REQUEST,
                f"unsupported file type {path.suffix!r}; connect CSV, TSV, Parquet or DuckDB",
            )
        if path.stat().st_size > MAX_SOURCE_BYTES:
            return _failed(ErrorCode.INVALID_REQUEST, f"file is larger than {MAX_SOURCE_BYTES} bytes")
        session = self._session(call)
        wanted = str(call.args.get("name") or path.stem)
        name = _identifier(wanted)
        # Reconnecting the same file is idempotent: reuse the earlier view.
        for existing, meta in session.views.items():
            if meta["path"] == str(path):
                return _connected(existing, meta, reused=True)
        base, i = name, 2
        while name in session.views:
            name = f"{base}_{i}"
            i += 1
        try:
            meta = self._register(session, name, path, kind)
        except duckdb.Error as exc:
            return _failed(ErrorCode.INVALID_REQUEST, _short(f"cannot read {path.name}: {exc}"))
        session.views[name] = meta
        return _connected(name, meta, reused=False)

    def _register(self, session: _Session, name: str, path: Path, kind: str) -> dict[str, Any]:
        """Create the view(s) for one source. Raises duckdb.Error on a bad file."""
        con = session.connection
        if kind == "duckdb":
            return self._register_database(con, name, path)
        reader = {"csv": "read_csv_auto", "tsv": "read_csv_auto", "parquet": "read_parquet"}[kind]
        options = ", delim='\t'" if kind == "tsv" else ""
        con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM {reader}({_literal(path)}{options})")
        columns, rows = _describe(con, name)
        if not columns:
            raise duckdb.Error("file has no columns")
        return {"path": str(path), "kind": kind, "tables": [name], "primary": name,
                "columns": columns, "row_count": rows}

    def _register_database(self, con: duckdb.DuckDBPyConnection, name: str, path: Path) -> dict[str, Any]:
        """Attach a DuckDB file read-only and expose its tables as views."""
        alias = _identifier(name + "_db")
        con.execute(f"ATTACH {_literal(path)} AS {alias} (READ_ONLY)")
        tables = [
            row[0]
            for row in con.execute(
                "SELECT table_name FROM duckdb_tables() WHERE database_name = ? ORDER BY table_name",
                [alias],
            ).fetchall()
        ]
        if not tables:
            raise duckdb.Error("the database has no tables")
        primary = tables[0]
        con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM {alias}.main.{primary}")
        for table in tables[1:]:
            con.execute(
                f"CREATE OR REPLACE VIEW {_identifier(name + '_' + table)} AS SELECT * FROM {alias}.main.{table}"
            )
        columns, rows = _describe(con, name)
        return {"path": str(path), "kind": "duckdb", "tables": tables, "primary": name,
                "columns": columns, "row_count": rows}

    # -- data.query --------------------------------------------------------

    async def _query(self, call: AuthorizedCall) -> ToolResult:
        sql = str(call.args.get("sql", "")).strip()
        try:
            limit = _int_arg(call.args, "limit", INLINE_ROWS, 1, 200)
        except ValueError as exc:
            return _failed(ErrorCode.INVALID_REQUEST, str(exc))
        session = self._sessions.get((call.scope_id, call.task_id))
        if session is None or not session.views:
            return _failed(ErrorCode.INVALID_REQUEST, "connect a table with data.connect before querying")
        problem = _check_select(session.connection, sql)
        if problem is not None:
            return _failed(ErrorCode.INVALID_REQUEST, problem)
        try:
            rows, columns = _run(session.connection, sql, MAX_ROWS)
            total, capped = _count(session.connection, sql, len(rows))
        except duckdb.Error as exc:
            return _failed(ErrorCode.TOOL_ERROR, _short(f"query failed: {exc}"))
        query_id = new_id("q")
        preview = rows[:limit]
        csv_ref = await self._artifacts.put(
            ArtifactInput(
                scope_id=call.scope_id,
                task_id=call.task_id,
                name=f"query/{query_id}.csv",
                media_type="text/csv",
                content=_to_csv(columns, rows).encode("utf-8"),
                source_refs=[f"sql:{query_id}"],
            )
        )
        evidence = {
            "query_id": query_id,
            "sql": sql,
            "columns": columns,
            "row_count": total,
            "row_count_capped": capped,
            "csv_ref": csv_ref.id,
            "created_at": utcnow().isoformat(),
        }
        json_ref = await self._artifacts.put(
            ArtifactInput(
                scope_id=call.scope_id,
                task_id=call.task_id,
                name=f"query/{query_id}.json",
                media_type="application/json",
                content=json.dumps(evidence, ensure_ascii=False, indent=2).encode("utf-8"),
                source_refs=[csv_ref.id],
            )
        )
        # The model needs the reference to cite it; the preview alone does not carry it.
        summary = f"query_ref: {json_ref.id}\n" + _render(columns, preview, total, capped)
        if len(summary) > INLINE_CHARS:
            summary = summary[:INLINE_CHARS] + "\n[preview truncated; read the csv artifact for the rows]"
        return ToolResult(
            status=ToolStatus.SUCCEEDED,
            summary=summary,
            data={
                "query_id": query_id,
                "query_ref": json_ref.id,
                "columns": columns,
                "row_count": total,
                "row_count_capped": capped,
            },
            artifact_refs=[json_ref.id, csv_ref.id],
        )


class DataQueryConnector(DataConnector):
    """The query tool, sharing one session registry with the connect tool."""

    spec = DATA_QUERY

    def __init__(self, connect: DataConnector) -> None:
        self._sessions = connect._sessions
        self._artifacts = connect._artifacts


def data_connectors(artifacts: ArtifactStore) -> list:
    """The data tools, sharing one session registry."""
    connect = DataConnector(artifacts)
    return [connect, DataQueryConnector(connect)]


# -- helpers -------------------------------------------------------------------


def _describe(con: duckdb.DuckDBPyConnection, name: str) -> tuple[list[dict[str, str]], int]:
    columns = [
        {"name": str(row[0]), "type": str(row[1])}
        for row in con.execute(f"DESCRIBE {name}").fetchall()
    ]
    rows = int(con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]) if columns else 0
    return columns, rows


def _strip_comments(sql: str) -> str:
    text = sql
    while True:
        match = _COMMENT.match(text)
        if not match:
            return text
        text = text[match.end():]


def _check_select(con: duckdb.DuckDBPyConnection, sql: str) -> str | None:
    if not sql:
        return "sql is required"
    if len(sql) > MAX_SQL_CHARS:
        return f"sql is longer than {MAX_SQL_CHARS} characters"
    head = _strip_comments(sql)
    if _NOT_A_READ.match(head):
        return "only SELECT queries are allowed"
    if _FILE_FUNCS.search(sql):
        return "sql may not read files: query the tables registered by data.connect"
    if _PATH_LITERAL.search(sql):
        return "sql may not name a file path: query the tables registered by data.connect"
    try:
        statements = con.extract_statements(sql)
    except duckdb.Error as exc:
        return f"cannot parse sql: {_short(str(exc))}"
    if len(statements) != 1:
        return f"run one statement at a time (got {len(statements)})"
    if statements[0].type != duckdb.StatementType.SELECT:
        return f"only SELECT queries are allowed (got {statements[0].type.name})"
    return None


def _run(con: duckdb.DuckDBPyConnection, sql: str, cap: int) -> tuple[list[tuple], list[str]]:
    cursor = con.execute(sql)
    columns = [str(d[0]) for d in (cursor.description or [])]
    rows = cursor.fetchmany(cap + 1)
    return rows[:cap], columns


def _count(con: duckdb.DuckDBPyConnection, sql: str, fetched: int) -> tuple[int, bool]:
    """Exact row count when the query can be wrapped, else what was fetched."""
    try:
        total = int(con.execute(f"SELECT count(*) FROM ({sql}) AS _q").fetchone()[0])
    except duckdb.Error:
        return fetched, fetched >= MAX_ROWS
    return total, total > fetched


def _to_csv(columns: list[str], rows: list[tuple]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if value is None else value for value in row])
    return buffer.getvalue()


def _cell(value: Any, width: int = 40) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\n", " ").replace("|", "\\|")
    return text if len(text) <= width else text[: width - 1] + "..."


def _render(columns: list[str], rows: list[tuple], total: int, capped: bool) -> str:
    count = f"{total:,} rows" + (" (capped)" if capped else "")
    if not rows:
        return f"{count}; no rows returned. Columns: {', '.join(columns) or '(none)'}"
    lines = [f"{count}; showing {len(rows)}. Columns: {', '.join(columns)}", ""]
    lines.append("| " + " | ".join(columns) + " |")
    lines.append("|" + "|".join("---" for _ in columns) + "|")
    for row in rows:
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    return "\n".join(lines)


def _connected(name: str, meta: dict[str, Any], *, reused: bool) -> ToolResult:
    columns = meta.get("columns") or []
    head = f"Table {name} ({meta['kind']}, {meta['row_count']:,} rows)"
    if reused:
        head += " -- already connected"
    body = ", ".join(f"{c['name']} {c['type']}" for c in columns) or "(no columns)"
    tables = meta.get("tables") or []
    if len(tables) > 1:
        body += f"\nAlso registered from this file: {', '.join(t for t in tables if t != meta['primary'])}"
    return ToolResult(
        status=ToolStatus.SUCCEEDED,
        summary=f"{head}\nColumns: {body}",
        data={"table": name, "columns": columns, "row_count": meta["row_count"]},
    )


def _short(text: str, limit: int = 400) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "..."


__all__ = ["DATA_CONNECT", "DATA_QUERY", "DataConnector", "DataQueryConnector", "data_connectors"]
