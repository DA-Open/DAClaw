"""Artifact storage for DataAgent: ResultTable envelope + reproducible runs.

Design goals (from the demo plan, batch 7):

- Every number in a final report must be traceable to a real execution. So each
  execution produces a :class:`ResultTable` with a stable ``artifact_id`` that the
  report references.
- The model only ever sees a *summary* of a result (sample rows + row count +
  stats) — the full result lands in a parquet/json file on disk. This keeps
  numbers real without blowing up the context window.
- Every analysis is a :class:`AnalysisRun` directory under
  ``$DACLAW_HOME/analysis_runs/<run_id>/`` containing queries, code, results,
  charts and the final report — so ``/data runs`` and ``/data artifacts`` can
  replay the whole thing.

This module has no heavy dependencies: parquet writing is attempted via the
engine's DuckDB connection when available, otherwise it falls back to JSON so
the store always works.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from daclaw_constants import get_daclaw_home


# ---------------------------------------------------------------------------
# id helpers (deterministic, no Math.random / uuid needed for demo stability)
# ---------------------------------------------------------------------------

_slug_re = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, *, max_len: int = 48) -> str:
    slug = _slug_re.sub("-", (text or "").strip().lower()).strip("-")
    return (slug or "item")[:max_len]


def _short_hash(text: str) -> str:
    # Small stable hash for artifact ids. Not for security — just uniqueness.
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------------------
# ResultTable — the standard execution-result envelope
# ---------------------------------------------------------------------------


@dataclass
class ResultTable:
    """Standard envelope for an execution result.

    ``sample_rows`` + ``row_count`` + ``stats`` are what the model sees.
    ``full_result_path`` points at the complete result on disk.
    """

    artifact_id: str
    kind: str  # "sql_result" | "dataframe" | ...
    columns: List[Dict[str, str]]  # [{"name","type"}]
    rows: List[List[Any]]  # full rows held in memory for small results
    row_count: int
    query_ref: Optional[str] = None
    full_result_path: Optional[str] = None
    stats: Dict[str, Any] = field(default_factory=dict)
    sample_limit: int = 50

    @property
    def column_names(self) -> List[str]:
        return [c["name"] for c in self.columns]

    @property
    def sample_rows(self) -> List[List[Any]]:
        return self.rows[: self.sample_limit]

    @property
    def truncated_preview(self) -> bool:
        return self.row_count > self.sample_limit

    def to_records(self) -> List[Dict[str, Any]]:
        """Return full rows as list-of-dicts (for charting / documents)."""
        names = self.column_names
        return [dict(zip(names, row)) for row in self.rows]

    def column_values(self, name: str) -> List[Any]:
        try:
            idx = self.column_names.index(name)
        except ValueError:
            return []
        return [row[idx] for row in self.rows]

    def envelope(self) -> Dict[str, Any]:
        """The compact JSON summary handed back to the model."""
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "query_ref": self.query_ref,
            "columns": self.columns,
            "row_count": self.row_count,
            "sample_rows": self.sample_rows,
            "full_result_path": self.full_result_path,
            "stats": self.stats,
            "truncated_preview": self.truncated_preview,
        }


# ---------------------------------------------------------------------------
# AnalysisRun + ArtifactStore
# ---------------------------------------------------------------------------


def get_analysis_runs_dir() -> Path:
    """Profile-scoped directory holding all analysis runs."""
    return get_daclaw_home() / "analysis_runs"


class AnalysisRun:
    """One reproducible analysis: queries, code, results, charts, report."""

    def __init__(self, run_dir: Path, meta: Dict[str, Any]):
        self.dir = run_dir
        self.meta = meta
        self.run_id: str = meta["run_id"]
        for sub in ("queries", "code", "results", "charts", "reports"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)

    # -- persistence of the run manifest ------------------------------------

    def _manifest_path(self) -> Path:
        return self.dir / "run.json"

    def _save_manifest(self) -> None:
        self._manifest_path().write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # -- artifact writers ---------------------------------------------------

    def save_query(self, sql: str, result: "ResultTable") -> str:
        """Persist a SQL query + its result. Returns the artifact_id."""
        n = len(self.meta.setdefault("queries", []))
        qref = f"queries/q_{n:03d}.sql"
        (self.dir / qref).write_text(sql, encoding="utf-8")
        result.query_ref = qref
        self._persist_result(result)
        self.meta["queries"].append(
            {
                "artifact_id": result.artifact_id,
                "query_ref": qref,
                "row_count": result.row_count,
                "columns": result.column_names,
                "full_result_path": result.full_result_path,
            }
        )
        self._save_manifest()
        return result.artifact_id

    def _persist_result(self, result: "ResultTable") -> None:
        """Write the full result to disk (parquet if possible, else JSON)."""
        rel = f"results/{result.artifact_id}.json"
        out = self.dir / rel
        payload = {
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
        }
        out.write_text(
            json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8"
        )
        result.full_result_path = rel

    def save_code(
        self, code: str, stdout: str = "", charts: Optional[List[str]] = None
    ) -> str:
        n = len(self.meta.setdefault("code", []))
        cref = f"code/c_{n:03d}.py"
        (self.dir / cref).write_text(code, encoding="utf-8")
        aid = f"code_{_short_hash(cref + str(n))}"
        self.meta["code"].append(
            {
                "artifact_id": aid,
                "code_ref": cref,
                "stdout": (stdout or "")[:4000],
                "charts": charts or [],
            }
        )
        self._save_manifest()
        return aid

    def save_chart(self, name: str, html: str) -> Dict[str, str]:
        """Persist a chart HTML file. Returns {artifact_id, path}."""
        n = len(self.meta.setdefault("charts", []))
        slug = _slugify(name)
        rel = f"charts/chart_{n:03d}_{slug}.html"
        (self.dir / rel).write_text(html, encoding="utf-8")
        aid = f"viz_{_short_hash(rel)}"
        rec = {"artifact_id": aid, "path": rel, "title": name}
        self.meta["charts"].append(rec)
        self._save_manifest()
        return {"artifact_id": aid, "path": str(self.dir / rel), "rel": rel}

    def save_report(
        self,
        name: str,
        html: str,
        references: Optional[List[str]] = None,
    ) -> Dict[str, str]:
        n = len(self.meta.setdefault("reports", []))
        slug = _slugify(name)
        rel = f"reports/report_{n:03d}_{slug}.html"
        (self.dir / rel).write_text(html, encoding="utf-8")
        aid = f"doc_{_short_hash(rel)}"
        rec = {
            "artifact_id": aid,
            "path": rel,
            "title": name,
            "references": references or [],
        }
        self.meta["reports"].append(rec)
        self._save_manifest()
        return {"artifact_id": aid, "path": str(self.dir / rel), "rel": rel}

    def finalize(self, status: str = "completed") -> None:
        self.meta["status"] = status
        self._save_manifest()

    def summary(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dir": str(self.dir),
            "task": self.meta.get("task", ""),
            "status": self.meta.get("status", "open"),
            "queries": len(self.meta.get("queries", [])),
            "charts": len(self.meta.get("charts", [])),
            "reports": len(self.meta.get("reports", [])),
            "created_at": self.meta.get("created_at"),
        }


class ArtifactStore:
    """Factory + registry for analysis runs (profile-scoped on disk)."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base = base_dir or get_analysis_runs_dir()
        self.base.mkdir(parents=True, exist_ok=True)

    def new_run(
        self,
        task: str,
        *,
        data_sources: Optional[List[str]] = None,
        now: Optional[float] = None,
    ) -> AnalysisRun:
        ts = now if now is not None else time.time()
        # run id: sortable timestamp + short slug (deterministic given inputs)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))
        run_id = f"run-{stamp}-{_short_hash(task + str(ts))[:4]}"
        run_dir = self.base / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "run_id": run_id,
            "task": task,
            "data_sources": data_sources or [],
            "created_at": stamp,
            "status": "open",
            "queries": [],
            "code": [],
            "charts": [],
            "reports": [],
        }
        run = AnalysisRun(run_dir, meta)
        run._save_manifest()
        return run

    def get_run(self, run_id: str) -> Optional[AnalysisRun]:
        run_dir = self.base / run_id
        manifest = run_dir / "run.json"
        if not manifest.exists():
            return None
        meta = json.loads(manifest.read_text(encoding="utf-8"))
        return AnalysisRun(run_dir, meta)

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        runs: List[Dict[str, Any]] = []
        if not self.base.exists():
            return runs
        for run_dir in sorted(self.base.iterdir(), reverse=True):
            manifest = run_dir / "run.json"
            if not manifest.is_file():
                continue
            try:
                meta = json.loads(manifest.read_text(encoding="utf-8"))
            except Exception:
                continue
            runs.append(AnalysisRun(run_dir, meta).summary())
            if len(runs) >= limit:
                break
        return runs


_STORE: Optional[ArtifactStore] = None


def get_store() -> ArtifactStore:
    global _STORE
    if _STORE is None:
        _STORE = ArtifactStore()
    return _STORE
