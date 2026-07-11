"""DataAgent service layer.

A dedicated data-analysis runtime that rides on top of the general DAClaw
``AIAgent`` loop rather than replacing it (see ``future work.md`` and
``docs/plans/2026-07-06-dataagent-demo-plan.md``).

This package is a *pure service layer*: it MUST NOT import ``run_agent`` (to
avoid circular imports). It is called by ``tools/*.py`` handlers, by the CLI,
and by the web server — all three share this one implementation.

Public surface (kept small on purpose):

- :class:`~agent.data_agent.engine.DataEngine` — DuckDB-backed unified SQL layer
  that can read CSV / TSV / Parquet / Excel / SQLite / DuckDB sources offline.
- :class:`~agent.data_agent.artifacts.ResultTable` — the standard envelope for
  an execution result (sample rows in context, full result on disk).
- :class:`~agent.data_agent.artifacts.ArtifactStore` /
  :class:`~agent.data_agent.artifacts.AnalysisRun` — reproducible analysis runs.
- :mod:`agent.data_agent.viz` — turn a result table into a self-contained,
  beautiful ECharts HTML chart / dashboard.
- :mod:`agent.data_agent.document` — assemble charts + tables + narrative into a
  self-contained HTML report.
"""

from __future__ import annotations

__all__ = [
    "DataEngine",
    "ResultTable",
    "ArtifactStore",
    "AnalysisRun",
    "get_engine",
]


def __getattr__(name: str):
    # Lazy re-exports so that importing the package never eagerly imports duckdb
    # (keeps ``import agent.data_agent`` cheap and side-effect free).
    if name in {"DataEngine", "get_engine"}:
        from agent.data_agent.engine import DataEngine, get_engine

        return {"DataEngine": DataEngine, "get_engine": get_engine}[name]
    if name in {"ResultTable", "ArtifactStore", "AnalysisRun"}:
        from agent.data_agent.artifacts import ArtifactStore, AnalysisRun, ResultTable

        return {
            "ResultTable": ResultTable,
            "ArtifactStore": ArtifactStore,
            "AnalysisRun": AnalysisRun,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
