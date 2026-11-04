"""Auto-analysis: turn a freshly-connected table into an overview dashboard.

This powers the demo's "upload a table → instantly see a beautiful dashboard"
moment. Given a :class:`DataEngine` and a table name, it:

  1. profiles the table (row count, column roles),
  2. derives a handful of sensible aggregate queries (KPIs + charts) from the
     column roles — a time trend, top categories, a distribution, etc.,
  3. runs them for real (so every number is grounded), and
  4. returns dashboard panels ready for :func:`agent.data_agent.viz.render_dashboard_html`.

All queries are generated deterministically from the schema — no LLM needed for
the first-screen overview. The agent can go deeper afterwards via conversation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from agent.data_agent.artifacts import ResultTable
from agent.data_agent.engine import DataEngine, _safe_ident


def _fmt_number(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        if abs(v) >= 1000:
            return f"{v:,.0f}"
        return f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _classify_columns(profile: Dict[str, Any]) -> Dict[str, List[str]]:
    """Group columns by semantic role from a profile."""
    roles: Dict[str, List[str]] = {"time": [], "measure": [], "category": [], "id": []}
    stats = profile.get("column_stats", {})
    for name, st in stats.items():
        sem = st.get("semantic", "category")
        roles.setdefault(sem, []).append(name)
    return roles


def _good_categories(profile: Dict[str, Any], candidates: List[str]) -> List[str]:
    """Prefer categorical columns with a small, chart-friendly cardinality."""
    stats = profile.get("column_stats", {})
    scored: List[Tuple[int, str]] = []
    for c in candidates:
        distinct = stats.get(c, {}).get("distinct_count") or 999
        if 2 <= distinct <= 30:
            scored.append((distinct, c))
    scored.sort()
    return [c for _, c in scored]


def build_overview(
    engine: DataEngine,
    table: str,
    *,
    max_charts: int = 4,
) -> Dict[str, Any]:
    """Build an overview: profile + KPI panels + chart panels (all grounded)."""
    profile = engine.profile_table(table, sample_rows=8)
    roles = _classify_columns(profile)
    t = _safe_ident(table)
    row_count = profile["row_count"]

    panels: List[Dict[str, Any]] = []
    queries: List[Dict[str, str]] = []  # for provenance / catalog

    # --- KPI cards --------------------------------------------------------
    panels.append({"title": f"Rows in {table}", "kpi": _fmt_number(row_count)})
    panels.append({"title": "Columns", "kpi": str(len(profile["columns"]))})

    measures = roles.get("measure", [])
    categories = _good_categories(profile, roles.get("category", []))
    times = roles.get("time", [])

    # a headline sum/avg over the first measure
    if measures:
        m = measures[0]
        qm = f'SELECT ROUND(SUM("{m}"),2) AS total, ROUND(AVG("{m}"),2) AS average FROM {t}'
        try:
            rt = engine.run_query(qm)
            rec = rt.to_records()[0]
            panels.append({"title": f"Total {m}", "kpi": _fmt_number(rec["total"])})
            panels.append({"title": f"Avg {m}", "kpi": _fmt_number(rec["average"])})
        except Exception:
            pass

    # --- charts -----------------------------------------------------------
    charts_added = 0

    # 1. time trend of the first measure (or count) by the first time column
    if times and charts_added < max_charts:
        tcol = times[0]
        agg = f'SUM("{measures[0]}")' if measures else "COUNT(*)"
        label = f"Sum of {measures[0]}" if measures else "Row count"
        sql = (
            f'SELECT CAST("{tcol}" AS VARCHAR)[1:7] AS period, '
            f"ROUND({agg}, 2) AS value FROM {t} "
            f'WHERE "{tcol}" IS NOT NULL GROUP BY period ORDER BY period'
        )
        try:
            rt = engine.run_query(sql)
            if rt.row_count >= 2:
                panels.append(
                    {
                        "title": f"{label} over time",
                        "result": rt,
                        "chart_type": "line",
                        "provenance": sql,
                    }
                )
                queries.append({"purpose": "time_trend", "sql": sql})
                charts_added += 1
        except Exception:
            pass

    # 2. top categories by measure (or count)
    for cat in categories:
        if charts_added >= max_charts:
            break
        agg = f'ROUND(SUM("{measures[0]}"),2)' if measures else "COUNT(*)"
        label = f"Sum of {measures[0]}" if measures else "Count"
        sql = (
            f'SELECT "{cat}" AS "{cat}", {agg} AS value FROM {t} '
            f'WHERE "{cat}" IS NOT NULL GROUP BY "{cat}" ORDER BY value DESC LIMIT 12'
        )
        try:
            rt = engine.run_query(sql)
            if rt.row_count >= 2:
                # pie for few slices, bar otherwise (viz auto-picks too)
                ct = "pie" if rt.row_count <= 6 else "bar"
                panels.append(
                    {
                        "title": f"{label} by {cat}",
                        "result": rt,
                        "chart_type": ct,
                        "provenance": sql,
                    }
                )
                queries.append({"purpose": f"by_{cat}", "sql": sql})
                charts_added += 1
        except Exception:
            pass

    # 3. distribution of a second measure if we still have room
    if measures and len(measures) >= 1 and charts_added < max_charts and not times:
        m = measures[0]
        sql = (
            f'SELECT CAST(FLOOR("{m}" / (NULLIF((SELECT MAX("{m}")-MIN("{m}") FROM {t}),0)/10)) AS INT) AS bucket, '
            f'COUNT(*) AS n FROM {t} WHERE "{m}" IS NOT NULL GROUP BY bucket ORDER BY bucket'
        )
        try:
            rt = engine.run_query(sql)
            if rt.row_count >= 2:
                panels.append(
                    {"title": f"Distribution of {m}", "result": rt, "chart_type": "bar", "provenance": sql}
                )
                charts_added += 1
        except Exception:
            pass

    return {
        "table": table,
        "profile": profile,
        "roles": roles,
        "panels": panels,
        "queries": queries,
        "chart_count": charts_added,
    }
