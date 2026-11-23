"""Data connectors: connect local tabular files and query them with read-only SQL.

Three tools:

* `data.list` finds tabular files within a user grant.

* `data.connect` registers one CSV / TSV / Parquet / DuckDB file as a view in
  this task's DuckDB session and reports its columns.
* `data.query` runs one SELECT (or WITH ... SELECT) over those views, returns a
  bounded preview, and stores the full result plus the SQL as artifacts so a
  later step (or the Verifier) can page through it and re-check the numbers.

Scope and safety:

* The path is checked by ToolGateway before this runs (`data.connect` is in
  `PATH_ARGS`); the connector re-checks that it is a regular file.
* A session is per task attempt: one task cannot see another task's
  sources, and a worker restart simply loses the views (the model reconnects).
* SQL must parse as exactly one SELECT and may not name a file: file-reading
  table functions (`read_csv`, `read_parquet`, `read_text`, `*_scan` ...),
  extension loading and path literals are refused before the query runs. The
  engine independently restricts external access to the files registered by data.connect.
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
    AttemptLease,
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
# data.list: how deep to walk a user directory and how many entries to look at.
LIST_DEPTH = 3
LIST_LIMIT = 50
LIST_MAX_SCAN = 5000

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


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _view_name(label: str, used: set[str]) -> str:
    base = _identifier(label)
    name = base
    index = 2
    while name in used:
        name = f"{base}_{index}"
        index += 1
    used.add(name)
    return name


def _literal(path: Path) -> str:
    """A single-quoted SQL string literal; DuckDB doubles the quote."""
    return "'" + str(path).replace("'", "''") + "'"


@dataclass
class _Session:
    """One task's DuckDB connection and the views registered in it."""

    connection: duckdb.DuckDBPyConnection
    views: dict[str, dict[str, Any]] = field(default_factory=dict)


DATA_LIST = ToolSpec(
    name="data.list",
    description=(
        "List the data files you are allowed to connect (CSV, TSV, Parquet, DuckDB), with their "
        "size. Use this when the task does not name a path, then pass one of the returned paths "
        "to data.connect."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Only return paths containing this text (case-insensitive).",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "How many paths to return (default 50).",
            },
        },
        "additionalProperties": False,
    },
    read_only=True,
    side_effects=False,
    idempotent=True,
)


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
        self._sessions: dict[tuple[str, str, str], _Session] = {}

    # -- session -----------------------------------------------------------

    def source(self, call: AuthorizedCall, name: str) -> tuple[duckdb.DuckDBPyConnection, dict[str, str]]:
        session = self._sessions[call.scope_id, call.task_id, call.attempt_id]
        return session.connection, session.views[name]["tables"]

    def release(self, lease: AttemptLease) -> None:
        session = self._sessions.pop((lease.scope_id, lease.task_id, lease.attempt_id), None)
        if session is not None:
            session.connection.close()

    def close(self) -> None:
        for session in self._sessions.values():
            session.connection.close()
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
        key = (call.scope_id, call.task_id, call.attempt_id)
        previous = self._sessions.get(key)
        sources = previous.views if previous else {}
        for name, meta in sources.items():
            if meta["path"] == str(path):
                return _connected(name, meta, reused=True)

        # DuckDB freezes the allow-list when external access is disabled. Rebuild
        # the catalog when a new authorized file arrives, replaying only trusted
        # view definitions. Existing source data and statistics are not recopied.
        paths = [meta["path"] for meta in sources.values()] + [str(path)]
        allowed = paths + [p + ".wal" for p in paths if Path(p).suffix.lower() in (".duckdb", ".ddb")]
        con = duckdb.connect(":memory:", config={"threads": "2", "memory_limit": "2GB",
                                                "allow_persistent_secrets": "false"})
        try:
            con.execute("SET allowed_paths = ?", [allowed])
            con.execute("SET enable_external_access = false")
            for meta in sources.values():
                for statement in meta["setup"]:
                    con.execute(statement)
            used = {view for meta in sources.values() for view in meta["tables"].values()}
            name = _view_name(str(call.args.get("name") or path.stem), used)
            meta = self._register(con, name, path, kind, used, len(sources))
        except duckdb.Error as exc:
            con.close()
            return _failed(ErrorCode.INVALID_REQUEST, _short(f"cannot connect {path.name}: {exc}"))
        self._sessions[key] = _Session(con, {**sources, name: meta})
        if previous is not None:
            previous.connection.close()
        return _connected(name, meta, reused=False)

    def _register(self, con, name: str, path: Path, kind: str, used: set[str], index: int) -> dict[str, Any]:
        setup = []
        tables = {}
        if kind == "duckdb":
            alias = f"source_db_{index}"
            attach = f"ATTACH {_literal(path)} AS {alias} (READ_ONLY)"
            con.execute(attach)
            setup.append(attach)
            rows = con.execute("SELECT schema_name, table_name FROM duckdb_tables() WHERE database_name = ? "
                               "ORDER BY schema_name, table_name", [alias]).fetchall()
            if not rows:
                raise duckdb.Error("the database has no tables")
            for schema, table in rows:
                label = table if schema == "main" else f"{schema}.{table}"
                view = _view_name(f"{name}_{label}", used) if tables else name
                tables[label] = view
                setup.append(f"CREATE VIEW {_quoted(view)} AS SELECT * FROM {alias}.{_quoted(schema)}.{_quoted(table)}")
            view_statements = setup[1:]
        else:
            reader = {"csv": "read_csv_auto", "tsv": "read_csv_auto", "parquet": "read_parquet"}[kind]
            options = ", delim='\t'" if kind == "tsv" else ""
            tables[name] = name
            setup.append(f"CREATE VIEW {_quoted(name)} AS SELECT * FROM {reader}({_literal(path)}{options})")
            view_statements = setup
        for statement in view_statements:
            con.execute(statement)
        columns, rows = _describe(con, name)
        return {"path": str(path), "kind": kind, "tables": tables, "primary": name,
                "columns": columns, "row_count": rows, "setup": setup}

    # -- data.query --------------------------------------------------------

    async def _query(self, call: AuthorizedCall) -> ToolResult:
        sql = str(call.args.get("sql", "")).strip()
        try:
            limit = _int_arg(call.args, "limit", INLINE_ROWS, 1, 200)
        except ValueError as exc:
            return _failed(ErrorCode.INVALID_REQUEST, str(exc))
        session = self._sessions.get((call.scope_id, call.task_id, call.attempt_id))
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


class DataListConnector(DataConnector):
    """@@data.list@@: the data files this user's directories contain."""

    spec = DATA_LIST

    def __init__(self, connect: DataConnector) -> None:
        self._sessions = connect._sessions
        self._artifacts = connect._artifacts

    async def execute(self, call: AuthorizedCall) -> ToolResult:
        try:
            limit = _int_arg(call.args, "limit", LIST_LIMIT, 1, 200)
        except ValueError as exc:
            return _failed(ErrorCode.INVALID_REQUEST, str(exc))
        pattern = str(call.args.get("pattern") or "").strip().lower()
        roots = [Path(root) for root in (call.allowed_paths or [])]
        if not roots:
            return ToolResult(
                status=ToolStatus.SUCCEEDED,
                summary=(
                    "No directories are allowed for this user, so there is nothing to list. "
                    "Ask the user to add one to their permissions."
                ),
                data={"files": [], "roots": [], "truncated": False},
            )
        found, truncated = _scan(roots, pattern, limit)
        if not found:
            where = ", ".join(str(root) for root in roots)
            suffix = f" matching {pattern!r}" if pattern else ""
            return ToolResult(
                status=ToolStatus.SUCCEEDED,
                summary=f"No CSV, TSV, Parquet or DuckDB files{suffix} under: {where}",
                data={"files": [], "roots": [str(r) for r in roots], "truncated": False},
            )
        lines = [f"{len(found)} data file(s)" + (" (list truncated; raise limit or add pattern)" if truncated else "") + ":", ""]
        lines.append("| path | size | modified |")
        lines.append("|---|---|---|")
        for path, size, modified in found:
            lines.append(f"| {path} | {_size(size)} | {modified} |")
        lines.append("")
        lines.append("Pass one of these paths to data.connect.")
        return ToolResult(
            status=ToolStatus.SUCCEEDED,
            summary="\n".join(lines),
            data={
                "files": [str(path) for path, _, _ in found],
                "roots": [str(root) for root in roots],
                "truncated": truncated,
            },
        )


def data_connectors(artifacts: ArtifactStore) -> list:
    """The data tools, sharing one session registry."""
    connect = DataConnector(artifacts)
    return [connect, DataQueryConnector(connect), DataListConnector(connect)]


# -- helpers -------------------------------------------------------------------


def _describe(con: duckdb.DuckDBPyConnection, name: str) -> tuple[list[dict[str, str]], int]:
    columns = [
        {"name": str(row[0]), "type": str(row[1])}
        for row in con.execute(f"DESCRIBE {_quoted(name)}").fetchall()
    ]
    rows = int(con.execute(f"SELECT count(*) FROM {_quoted(name)}").fetchone()[0]) if columns else 0
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
    tables = meta["tables"].values()
    if len(tables) > 1:
        body += f"\nAlso registered from this file: {', '.join(t for t in tables if t != meta['primary'])}"
    return ToolResult(
        status=ToolStatus.SUCCEEDED,
        summary=f"{head}\nColumns: {body}",
        data={"table": name, "columns": columns, "row_count": meta["row_count"]},
    )


def _scan(roots: list[Path], pattern: str, limit: int) -> tuple[list[tuple[Path, int, str]], bool]:
    """Data files under `roots`, sorted by path, at most `limit` of them."""
    found: list[tuple[Path, int, str]] = []
    scanned = 0
    for root in roots:
        candidates: list[Path] = []
        if root.is_file():
            candidates = [root] if root.suffix.lower() in _SUFFIXES else []
        else:
            try:
                stack = [(root, 0)]
                while stack and scanned < LIST_MAX_SCAN:
                    current, depth = stack.pop()
                    for entry in sorted(current.iterdir(), key=lambda e: e.name):
                        if entry.name.startswith(".") or entry.is_symlink():
                            continue
                        scanned += 1
                        if entry.is_dir():
                            if depth + 1 <= LIST_DEPTH:
                                stack.append((entry, depth + 1))
                        elif entry.suffix.lower() in _SUFFIXES and entry.is_file():
                            candidates.append(entry)
                        if scanned >= LIST_MAX_SCAN:
                            break
            except OSError:
                continue
        for path in candidates:
            if pattern and pattern not in str(path).lower():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            found.append((path, stat.st_size, _mtime(stat.st_mtime)))
    found.sort(key=lambda item: str(item[0]))
    return found[:limit], len(found) > limit


def _mtime(ts: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GB"


def _short(text: str, limit: int = 400) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "..."


__all__ = [
    "DATA_CONNECT",
    "DATA_LIST",
    "DATA_QUERY",
    "DataConnector",
    "DataListConnector",
    "DataQueryConnector",
    "data_connectors",
]
