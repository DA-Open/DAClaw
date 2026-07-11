"""Process-wide 'current analysis run' context.

Tools like ``data_query`` / ``data_visualize`` / ``data_document`` all want to
save artifacts into the *same* analysis run so the run is a coherent, replayable
record. Rather than thread a run object through every call, we keep a single
current run here (per process), created lazily on first use.

The CLI/web layer can call :func:`start_run` to begin a fresh, named run (e.g.
when a new user task starts) and :func:`end_run` to finalize it.
"""

from __future__ import annotations

from typing import Optional

from agent.data_agent.artifacts import AnalysisRun, get_store

_CURRENT: Optional[AnalysisRun] = None


def current_run() -> AnalysisRun:
    """Return the active run, creating a default one if none exists."""
    global _CURRENT
    if _CURRENT is None:
        _CURRENT = get_store().new_run(task="ad-hoc data analysis")
    return _CURRENT


def start_run(task: str, *, data_sources=None) -> AnalysisRun:
    """Begin a new named run and make it current."""
    global _CURRENT
    _CURRENT = get_store().new_run(task=task, data_sources=data_sources)
    return _CURRENT


def end_run(status: str = "completed") -> Optional[AnalysisRun]:
    """Finalize the current run and clear it."""
    global _CURRENT
    run = _CURRENT
    if run is not None:
        run.finalize(status)
    _CURRENT = None
    return run


def has_active_run() -> bool:
    return _CURRENT is not None
