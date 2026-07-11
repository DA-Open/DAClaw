"""DuckDB-backed unified execution layer for DataAgent.

One杠杆点 (leverage point): DuckDB can query CSV / TSV / Parquet natively, and
we bridge Excel (via pandas/openpyxl) and SQLite (via stdlib ``sqlite3`` +
pandas) into the same connection. So every source — file or database — ends up
behind ONE ``run_query(sql)`` interface returning a :class:`ResultTable`.

Everything here is *offline*: we only use DuckDB's built-in readers and stdlib
``sqlite3``; we never auto-install a DuckDB extension (which would need network).

This module degrades gracefully: if ``duckdb`` is unavailable, :func:`engine_available`
returns False and tools that depend on it can advertise themselves as disabled.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.data_agent.artifacts import ResultTable, _short_hash, _slugify
from daclaw_constants import get_daclaw_home

try:  # pragma: no cover - import guard
    import duckdb  # type: ignore

    _DUCKDB_OK = True
except Exception:  # pragma: no cover
    duckdb = None  # type: ignore
    _DUCKDB_OK = False


def engine_available() -> bool:
    return _DUCKDB_OK


class DataEngineError(ValueError):
    """Raised when a source cannot be attached or a query fails."""


_CSV_SUFFIXES = {".csv", ".tsv", ".txt"}
_PARQUET_SUFFIXES = {".parquet", ".pq"}
_EXCEL_SUFFIXES = {".xlsx", ".xls", ".xlsm"}
_SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".db3"}
_DUCKDB_SUFFIXES = {".duckdb", ".ddb"}

# Characters that are unsafe in an unquoted SQL identifier. We keep Unicode
# letters/digits (so Chinese sheet & column names survive — DuckDB supports
# Unicode identifiers) and only collapse whitespace/punctuation to "_".
_ident_re = re.compile(r"[^\w]+", re.UNICODE)


def _safe_ident(name: str) -> str:
    """Turn an arbitrary name into a safe SQL identifier, preserving Unicode.

    ``\\w`` with ``re.UNICODE`` keeps CJK and other letters, so a sheet named
    "审计发现清单" stays "审计发现清单" instead of collapsing to "t". Only
    spaces and punctuation become "_".
    """
    cleaned = _ident_re.sub("_", (name or "").strip()).strip("_")
    if not cleaned:
        cleaned = "t"
    if cleaned[0].isdigit():
        cleaned = "t_" + cleaned
    # Do NOT lowercase — that would mangle nothing for CJK but destroys
    # case in ASCII names; keep the author's original casing.
    return cleaned


# SQLAlchemy dialect+driver per db type (drivers installed on demand)
_SA_DIALECTS = {
    "postgres": "postgresql+psycopg2",
    "postgresql": "postgresql+psycopg2",
    "mysql": "mysql+pymysql",
    "mariadb": "mysql+pymysql",
    "mssql": "mssql+pyodbc",
    "sqlserver": "mssql+pyodbc",
    "snowflake": "snowflake",
    "sqlite": "sqlite",
}
_DEFAULT_PORTS = {"postgres": 5432, "postgresql": 5432, "mysql": 3306, "mariadb": 3306, "mssql": 1433, "sqlserver": 1433}


def _build_sqlalchemy_url(
    dbtype: str,
    host: "Optional[str]",
    port: "Optional[int]",
    user: "Optional[str]",
    password: "Optional[str]",
    database: "Optional[str]",
) -> "Optional[str]":
    """Assemble a SQLAlchemy URL from structured params (creds URL-encoded)."""
    import urllib.parse as _up

    dbtype = (dbtype or "").strip().lower()
    dialect = _SA_DIALECTS.get(dbtype)
    if not dialect:
        return None
    if dbtype == "sqlite":
        return f"sqlite:///{database or host or ''}"
    if dbtype == "snowflake":
        # snowflake://user:pass@account/db  (host carries the account id)
        u = _up.quote(user or "", safe=""); p = _up.quote(password or "", safe="")
        acct = host or ""
        return f"snowflake://{u}:{p}@{acct}/{database or ''}"
    u = _up.quote(user or "", safe=""); p = _up.quote(password or "", safe="")
    prt = port or _DEFAULT_PORTS.get(dbtype, 0)
    auth = f"{u}:{p}@" if user else ""
    hostport = f"{host or 'localhost'}" + (f":{prt}" if prt else "")
    return f"{dialect}://{auth}{hostport}/{database or ''}"


def _reheader_excel_sheet(raw: "object") -> "object":
    """Detect the real header row of a header-less sheet DataFrame and apply it.

    Business spreadsheets often prepend a merged "title banner" row (one filled
    cell, rest blank) before the actual column headers. Reading with
    ``header=None`` gives us the raw grid; we then pick the first row that looks
    like a header (mostly non-empty, mostly text, low duplication) and use it.

    Returns a new DataFrame with proper columns and the banner/pre-header rows
    dropped, or ``None`` if the sheet is effectively empty.
    """
    import pandas as pd

    if raw is None or raw.empty:
        return None

    n_cols = raw.shape[1]
    best_row = 0
    scan = min(len(raw), 8)  # only look at the first few rows
    best_score = -1.0
    for r in range(scan):
        vals = raw.iloc[r].tolist()
        non_empty = [v for v in vals if pd.notna(v) and str(v).strip() != ""]
        if not non_empty:
            continue
        fill = len(non_empty) / n_cols
        # unique fraction of the non-empty cells (headers rarely repeat)
        uniq = len(set(str(v).strip() for v in non_empty)) / len(non_empty)
        # fraction that is NOT purely numeric (headers are usually labels)
        non_numeric = sum(
            0 if _looks_numeric(v) else 1 for v in non_empty
        ) / len(non_empty)
        score = fill * 0.5 + uniq * 0.3 + non_numeric * 0.2
        # a single-cell banner row scores low on fill; a real header scores high
        if score > best_score:
            best_score = score
            best_row = r

    header_vals = raw.iloc[best_row].tolist()
    body = raw.iloc[best_row + 1 :].reset_index(drop=True)
    if body.empty:
        # header row was the last row — treat the whole thing as headerless data
        body = raw.reset_index(drop=True)
        header_vals = [f"col_{i+1}" for i in range(n_cols)]

    # Build clean, de-duplicated column names.
    cols: List[str] = []
    seen: Dict[str, int] = {}
    for i, h in enumerate(header_vals):
        name = "" if (h is None or (isinstance(h, float) and pd.isna(h))) else str(h).strip()
        if not name or name.lower().startswith("unnamed"):
            name = f"col_{i+1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        cols.append(name)
    body.columns = cols
    # drop fully-empty rows and columns
    body = body.dropna(axis=0, how="all").dropna(axis=1, how="all")
    return body


def _looks_numeric(v: "object") -> bool:
    import pandas as pd

    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return not pd.isna(v)
    try:
        float(str(v).replace(",", "").strip())
        return True
    except (ValueError, TypeError):
        return False


class DataEngine:
    """A DuckDB connection with helpers to attach heterogeneous sources."""

    def __init__(self):
        if not _DUCKDB_OK:
            raise DataEngineError(
                "duckdb is not installed. Install with: pip install duckdb"
            )
        self.con = duckdb.connect(database=":memory:")
        self._lock = threading.RLock()
        # slug -> {path, kind, tables:[names]}
        self.sources: Dict[str, Dict[str, Any]] = {}
        # table name -> source slug
        self._table_source: Dict[str, str] = {}
        self._restore_file_sources()

    def _reserve_table(self, preferred: str) -> str:
        """Return a table name that doesn't collide with an existing one."""
        name = _safe_ident(preferred)
        base, i = name, 2
        while name in self._table_source:
            name = f"{base}_{i}"
            i += 1
        # reserve immediately so subsequent calls in the same source see it
        self._table_source[name] = "__pending__"
        return name

    # -- source attachment --------------------------------------------------

    def connect_source(
        self, source: str, *, name: Optional[str] = None, persist: bool = True
    ) -> Dict[str, Any]:
        """Attach a data source and return a descriptor with its table list."""
        path = Path(source).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if not path.exists():
            raise DataEngineError(f"Data source not found: {path}")

        # Idempotent: if this exact file is already connected, reuse it rather
        # than creating slug_2 / slug_3 duplicates on repeated connects.
        for existing in self.sources.values():
            if existing.get("path") == str(path):
                return existing

        suffix = path.suffix.lower()
        slug = _safe_ident(name or path.stem)
        # de-dup slug
        base_slug, i = slug, 2
        while slug in self.sources:
            slug = f"{base_slug}_{i}"
            i += 1

        if suffix in _CSV_SUFFIXES:
            tables = self._attach_csv(path, slug)
            kind = "csv"
        elif suffix in _PARQUET_SUFFIXES:
            tables = self._attach_parquet(path, slug)
            kind = "parquet"
        elif suffix in _EXCEL_SUFFIXES:
            tables = self._attach_excel(path, slug)
            kind = "excel"
        elif suffix in _SQLITE_SUFFIXES:
            tables = self._attach_sqlite(path, slug)
            kind = "sqlite"
        elif suffix in _DUCKDB_SUFFIXES:
            tables = self._attach_duckdb(path, slug)
            kind = "duckdb"
        else:
            raise DataEngineError(
                f"Unsupported source format: {suffix or '(none)'}. "
                "Supported: csv, tsv, parquet, xlsx, sqlite, duckdb."
            )

        for t in tables:
            self._table_source[t] = slug
        descriptor = {
            "slug": slug,
            "path": str(path),
            "kind": kind,
            "tables": tables,
        }
        self.sources[slug] = descriptor
        if persist:
            self._persist_file_sources()
        return descriptor

    def _source_manifest_path(self) -> Path:
        return get_daclaw_home() / "data_sources.json"

    def _persist_file_sources(self) -> None:
        """Persist only reconnectable local files; never store live DB credentials."""
        import json

        records = [
            {"path": source["path"], "name": source["slug"], "kind": source["kind"]}
            for source in self.sources.values()
            if source.get("kind") != "database" and Path(str(source.get("path", ""))).is_file()
        ]
        path = self._source_manifest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "sources": records}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _restore_file_sources(self) -> None:
        import json

        path = self._source_manifest_path()
        if not path.is_file():
            return
        try:
            records = json.loads(path.read_text(encoding="utf-8")).get("sources") or []
        except Exception:
            return
        for record in records[:40]:
            source_path = Path(str(record.get("path") or "")).expanduser()
            if not source_path.is_file():
                continue
            try:
                self.connect_source(str(source_path), name=record.get("name"), persist=False)
            except Exception:
                continue

    def _attach_csv(self, path: Path, slug: str) -> List[str]:
        table = self._reserve_table(slug)
        delim = "\t" if path.suffix.lower() == ".tsv" else None
        p = str(path).replace("'", "''")
        opts = "SAMPLE_SIZE=-1, header=true"
        if delim:
            opts += f", delim='{delim}'"
        self.con.execute(
            f"CREATE OR REPLACE VIEW {table} AS "
            f"SELECT * FROM read_csv_auto('{p}', {opts})"
        )
        return [table]

    def _attach_parquet(self, path: Path, slug: str) -> List[str]:
        table = self._reserve_table(slug)
        p = str(path).replace("'", "''")
        self.con.execute(
            f"CREATE OR REPLACE VIEW {table} AS SELECT * FROM read_parquet('{p}')"
        )
        return [table]

    def _attach_excel(self, path: Path, slug: str) -> List[str]:
        try:
            import pandas as pd  # noqa: F401
        except Exception as exc:  # pragma: no cover
            raise DataEngineError(
                "Reading Excel needs pandas + openpyxl (pip install pandas openpyxl)"
            ) from exc
        import pandas as pd

        # Read with NO header first so we can find the real header row. Many
        # business spreadsheets put a merged "title banner" on row 0 (e.g.
        # "2026年专项审计整改资料包"), which pandas' default header=0 would
        # wrongly treat as column names (producing "title", "title.1", ...).
        raw_sheets = pd.read_excel(path, sheet_name=None, header=None)
        tables: List[str] = []
        multi = len(raw_sheets) > 1
        for sheet_name, raw in raw_sheets.items():
            df = _reheader_excel_sheet(raw)
            if df is None or df.empty:
                continue
            preferred = f"{slug}_{_safe_ident(sheet_name)}" if multi else slug
            tname = self._reserve_table(preferred)
            self.con.register(f"_tmp_{tname}", df)
            self.con.execute(
                f'CREATE OR REPLACE TABLE "{tname}" AS SELECT * FROM _tmp_{tname}'
            )
            self.con.unregister(f"_tmp_{tname}")
            tables.append(tname)
        return tables

    def _attach_sqlite(self, path: Path, slug: str) -> List[str]:
        """Load every SQLite table into DuckDB via stdlib sqlite3 (offline)."""
        try:
            import pandas as pd
        except Exception as exc:  # pragma: no cover
            raise DataEngineError(
                "Reading SQLite needs pandas (pip install pandas)"
            ) from exc

        conn = sqlite3.connect(str(path))
        try:
            names = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            tables: List[str] = []
            for tbl in names:
                df = pd.read_sql_query(f'SELECT * FROM "{tbl}"', conn)
                tname = self._reserve_table(tbl)
                self.con.register(f"_tmp_{tname}", df)
                self.con.execute(
                    f"CREATE OR REPLACE TABLE {tname} AS SELECT * FROM _tmp_{tname}"
                )
                self.con.unregister(f"_tmp_{tname}")
                tables.append(tname)
            return tables
        finally:
            conn.close()

    def _attach_duckdb(self, path: Path, slug: str) -> List[str]:
        p = str(path).replace("'", "''")
        self.con.execute(f"ATTACH '{p}' AS {slug} (READ_ONLY)")
        rows = self.con.execute(
            "SELECT table_name FROM information_schema.tables "
            f"WHERE table_catalog = '{slug}'"
        ).fetchall()
        tables: List[str] = []
        multi = len(rows) > 1
        for (tbl,) in rows:
            # expose as top-level view for convenience
            preferred = f"{slug}_{tbl}" if multi else tbl
            view = self._reserve_table(preferred)
            self.con.execute(
                f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM {slug}.{tbl}"
            )
            tables.append(view)
        return tables

    # -- live database connections -----------------------------------------

    def connect_database(
        self,
        *,
        dbtype: str,
        dsn: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
        name: Optional[str] = None,
        schema: Optional[str] = None,
        max_tables: int = 60,
    ) -> Dict[str, Any]:
        """Connect a live external database (postgres/mysql/sqlite/duckdb/
        snowflake/…) and expose its tables in the DuckDB workspace.

        Strategy: prefer DuckDB's native ATTACH for postgres/mysql (fast, lazy);
        fall back to SQLAlchemy + pandas materialization for everything else.
        Credentials are NEVER stored in the returned descriptor.
        """
        dbtype = (dbtype or "").strip().lower()
        slug = _safe_ident(name or database or dbtype or "db")
        base_slug, i = slug, 2
        while slug in self.sources:
            slug = f"{base_slug}_{i}"
            i += 1

        url = dsn or _build_sqlalchemy_url(dbtype, host, port, user, password, database)
        if not url:
            raise DataEngineError(
                f"Could not build a connection string for {dbtype!r}. "
                "Provide a DSN or host/user/database."
            )

        tables: List[str] = []
        via = None
        # 1) DuckDB native ATTACH for postgres / mysql (best: lazy, pushdown)
        if dbtype in ("postgres", "postgresql", "mysql"):
            try:
                tables = self._attach_via_duckdb_extension(dbtype, url, slug, schema)
                via = "duckdb_attach"
            except Exception:
                tables = []  # fall through to SQLAlchemy
        # 2) SQLAlchemy fallback (postgres/mysql without ext, snowflake, mssql, …)
        if not tables:
            tables = self._attach_via_sqlalchemy(url, slug, schema, max_tables)
            via = via or "sqlalchemy"

        for t in tables:
            self._table_source[t] = slug
        descriptor = {
            "slug": slug,
            "path": f"{dbtype}://…",  # redacted — no credentials
            "kind": "database",
            "dbtype": dbtype,
            "via": via,
            "tables": tables,
        }
        self.sources[slug] = descriptor
        return descriptor

    def _attach_via_duckdb_extension(
        self, dbtype: str, url: str, slug: str, schema: Optional[str]
    ) -> List[str]:
        ext = "postgres" if dbtype in ("postgres", "postgresql") else "mysql"
        # Loading a preinstalled extension is safe/offline. Installing one may
        # access the network, so require an explicit opt-in.
        try:
            self.con.execute(f"LOAD {ext}")
        except Exception:
            if os.getenv("DACLAW_ALLOW_DUCKDB_INSTALL", "").lower() not in {"1", "true", "yes"}:
                raise DataEngineError(
                    f"DuckDB {ext} extension is not installed. Set "
                    "DACLAW_ALLOW_DUCKDB_INSTALL=1 to allow a network install, "
                    "or use the SQLAlchemy fallback."
                )
            self.con.execute(f"INSTALL {ext}")
            self.con.execute(f"LOAD {ext}")
        typ = "POSTGRES" if ext == "postgres" else "MYSQL"
        esc = url.replace("'", "''")
        self.con.execute(f"ATTACH '{esc}' AS {slug} (TYPE {typ}, READ_ONLY)")
        rows = self.con.execute(
            "SELECT table_name FROM information_schema.tables "
            f"WHERE table_catalog = '{slug}'"
            + (f" AND table_schema = '{schema}'" if schema else "")
        ).fetchall()
        tables: List[str] = []
        multi = len(rows) > 1
        for (tbl,) in rows:
            preferred = f"{slug}_{tbl}" if multi else tbl
            view = self._reserve_table(preferred)
            self.con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM {slug}.{tbl}")
            tables.append(view)
        return tables

    def _attach_via_sqlalchemy(
        self, url: str, slug: str, schema: Optional[str], max_tables: int
    ) -> List[str]:
        try:
            import pandas as pd
            from sqlalchemy import MetaData, Table, create_engine, inspect as sa_inspect, select
        except Exception as exc:  # pragma: no cover
            raise DataEngineError(
                "Connecting this database needs SQLAlchemy + a driver "
                "(e.g. psycopg2-binary for Postgres, pymysql for MySQL)."
            ) from exc

        eng = None
        try:
            eng = create_engine(url)
            insp = sa_inspect(eng)
            names = insp.get_table_names(schema=schema)
        except Exception as exc:
            if eng is not None:
                eng.dispose()
            raise DataEngineError(f"Could not connect: {_redact_connection_error(exc)}") from exc

        tables: List[str] = []
        max_rows = max(1, int(os.getenv("DACLAW_DB_IMPORT_MAX_ROWS", "200000")))
        with eng.connect() as conn:
            for tbl in names[: int(max_tables)]:
                try:
                    table_obj = Table(tbl, MetaData(), schema=schema, autoload_with=eng)
                    df = pd.read_sql(select(table_obj).limit(max_rows), conn)
                except Exception:
                    continue
                tname = self._reserve_table(f"{slug}_{tbl}" if len(names) > 1 else tbl)
                self.con.register(f"_tmp_{tname}", df)
                self.con.execute(f"CREATE OR REPLACE TABLE {tname} AS SELECT * FROM _tmp_{tname}")
                self.con.unregister(f"_tmp_{tname}")
                tables.append(tname)
        try:
            eng.dispose()
        except Exception:
            pass
        if not tables:
            raise DataEngineError("Connected, but found no readable tables.")
        return tables

    # -- inspection ---------------------------------------------------------

    def list_tables(self) -> List[str]:
        return sorted(self._table_source.keys())

    def source_path_for_table(self, table: str) -> Optional[str]:
        """Return the on-disk path of the source file a table came from.

        Used by Excel-editing tools that need to reopen the originally uploaded
        workbook. Returns None if the table isn't from a file source.
        """
        slug = self._table_source.get(table)
        if not slug:
            return None
        desc = self.sources.get(slug)
        return desc.get("path") if desc else None

    def excel_sources(self) -> List[Dict[str, str]]:
        """List connected Excel sources as [{slug, path}] (kind == 'excel')."""
        return [
            {"slug": s["slug"], "path": s["path"]}
            for s in self.sources.values()
            if s.get("kind") == "excel"
        ]

    def table_schema(self, table: str) -> List[Dict[str, str]]:
        with self._lock:
            rows = self.con.execute(f"DESCRIBE {_safe_ident(table)}").fetchall()
        # DESCRIBE columns: column_name, column_type, null, key, default, extra
        return [{"name": r[0], "type": str(r[1])} for r in rows]

    # -- execution ----------------------------------------------------------

    def run_query(
        self, sql: str, *, max_rows: int = 100_000, artifact_id: Optional[str] = None
    ) -> ResultTable:
        """Execute a read-only SQL query and return a ResultTable."""
        if not _is_readonly(sql):
            raise DataEngineError(
                "Only read-only queries are allowed (SELECT / WITH / DESCRIBE / "
                "SHOW / PRAGMA / EXPLAIN)."
            )
        t0 = time.perf_counter()
        try:
            with self._lock:
                cur = self.con.execute(sql)
                description = cur.description or []
                columns = [{"name": d[0], "type": _duck_type(d)} for d in description]
                rows = cur.fetchmany(max_rows)
        except Exception as exc:
            raise DataEngineError(f"Query failed: {exc}") from exc
        # normalize values to JSON-friendly python types
        norm_rows = [[_json_safe(v) for v in row] for row in rows]
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

        # per-column null counts (over fetched rows)
        null_counts: Dict[str, int] = {}
        for idx, col in enumerate(columns):
            null_counts[col["name"]] = sum(1 for r in norm_rows if r[idx] is None)

        aid = artifact_id or f"res_{_short_hash(sql)}"
        return ResultTable(
            artifact_id=aid,
            kind="sql_result",
            columns=columns,
            rows=norm_rows,
            row_count=len(norm_rows),
            stats={"elapsed_ms": elapsed_ms, "null_counts": null_counts},
        )

    def profile_table(
        self, table: str, *, sample_rows: int = 10
    ) -> Dict[str, Any]:
        """Return a structured profile for one attached table."""
        t = _safe_ident(table)
        schema = self.table_schema(t)
        with self._lock:
            row_count = self.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        sample = self.run_query(f"SELECT * FROM {t} LIMIT {int(sample_rows)}")
        column_stats: Dict[str, Any] = {}
        for col in schema:
            name = col["name"]
            ctype = col["type"].lower()
            qname = f'"{name}"'
            try:
                with self._lock:
                    nulls, distinct = self.con.execute(
                        f"SELECT COUNT(*) FILTER (WHERE {qname} IS NULL), "
                        f"COUNT(DISTINCT {qname}) FROM {t}"
                    ).fetchone()
            except Exception:
                nulls, distinct = None, None
            stat: Dict[str, Any] = {
                "type": col["type"],
                "null_count": nulls,
                "distinct_count": distinct,
                "semantic": _semantic_type(name, ctype),
            }
            if any(k in ctype for k in ("int", "double", "float", "decimal", "hugeint")):
                try:
                    with self._lock:
                        mn, mx, avg = self.con.execute(
                            f"SELECT MIN({qname}), MAX({qname}), AVG({qname}) FROM {t}"
                        ).fetchone()
                    stat["numeric"] = {
                        "min": _json_safe(mn),
                        "max": _json_safe(mx),
                        "mean": round(avg, 6) if avg is not None else None,
                    }
                except Exception:
                    pass
            column_stats[name] = stat
        return {
            "table": table,
            "row_count": row_count,
            "columns": [c["name"] for c in schema],
            "schema": schema,
            "column_stats": column_stats,
            "sample_rows": sample.to_records(),
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_READONLY_PREFIXES = ("select", "with", "describe", "show", "pragma", "explain", "table")


def _is_readonly(sql: str) -> bool:
    s = sql.strip().lstrip("(").strip().lower()
    # forbid obvious mutations even if they appear after a CTE
    forbidden = re.search(
        r"\b(insert|update|delete|drop|create|alter|attach|copy|truncate|replace|"
        r"pragma\s+enable|install|load)\b",
        s,
    )
    if s.startswith("with") or s.startswith("select"):
        # a leading CTE/SELECT is fine unless it hides a mutation
        if forbidden and forbidden.group(1) not in ("replace",):
            # allow "create" only if it's not present; CTEs don't mutate
            if re.search(r"\b(insert|update|delete|drop|alter|attach|copy|truncate|install|load)\b", s):
                return False
        return True
    return s.startswith(_READONLY_PREFIXES) and not forbidden


def _duck_type(description_row) -> str:
    # DBAPI description: (name, type_code, ...). DuckDB puts a type name string.
    try:
        tc = description_row[1]
        return str(tc) if tc is not None else "unknown"
    except Exception:
        return "unknown"


def _json_safe(value: Any) -> Any:
    import datetime
    import decimal

    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return str(value)
    return str(value)


def _semantic_type(name: str, ctype: str) -> str:
    """Heuristic semantic role used for auto chart-type selection."""
    n = name.lower()
    if any(k in ctype for k in ("date", "time", "timestamp")):
        return "time"
    if any(k in n for k in ("date", "_at", "time", "day", "month", "year", "week")):
        return "time"
    if any(k in ctype for k in ("int", "double", "float", "decimal", "hugeint")):
        # ids are numeric but categorical in role
        if n.endswith("_id") or n == "id":
            return "id"
        return "measure"
    if n.endswith("_id") or n == "id":
        return "id"
    return "category"


def _redact_connection_error(exc: Exception) -> str:
    text = str(exc)
    text = re.sub(r"([a-z][a-z0-9+.-]*://[^:/\s]+:)[^@\s]+@", r"\1***@", text, flags=re.I)
    text = re.sub(r"(?i)(password|pwd|token|api[_-]?key)\s*[=:]\s*[^\s,;]+", r"\1=***", text)
    return text[:1200]


_ENGINE: Optional["DataEngine"] = None


def get_engine() -> "DataEngine":
    """Return a process-wide engine (sources persist across tool calls)."""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = DataEngine()
    return _ENGINE


def reset_engine() -> None:
    global _ENGINE
    if _ENGINE is not None:
        try:
            _ENGINE.con.close()
        except Exception:
            pass
    _ENGINE = None
