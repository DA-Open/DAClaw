"""NL-to-SQL data-engineering pipeline plugin.

The DE centerpiece of the demo (aligns with DAComp's DE-Impl / DE-Evol idea of
repository-level, multi-stage SQL pipelines):

    natural language  ->  a *plan* of named SQL stages (a DAG)  ->  each stage
    executed for real in DuckDB  ->  previewable intermediate tables  ->  a
    final result, with the whole pipeline visualizable as a dependency graph.

Unlike a single "text2sql" shot, this builds a **pipeline**: each stage
materializes a named table that later stages depend on. That is what real data
engineering looks like, and it gives the UI a DAG to render.

Everything runs on the existing DuckDB :class:`~agent.data_agent.engine.DataEngine`
so intermediate tables are queryable and every number stays grounded.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from agent.data_agent.engine import DataEngine, DataEngineError, _safe_ident
from agent.data_agent.llm import get_llm


class NL2SQLError(RuntimeError):
    pass


_PLAN_SYSTEM = """You are a senior data engineer. Given a database schema and a \
natural-language request, design a MULTI-STAGE SQL pipeline in DuckDB dialect.

Rules:
- Break the work into ordered stages. Each stage creates ONE named intermediate \
table via a single SELECT (no DDL/DML other than the SELECT we will wrap).
- Later stages may read the tables produced by earlier stages.
- Each stage name must be a valid lowercase snake_case identifier.
- Keep SQL read-only (SELECT / WITH only). No INSERT/UPDATE/DELETE/DROP/CREATE.
- Prefer clear, correct SQL over clever SQL. Add a short "purpose" for each stage.
- The LAST stage is the final answer table.

Return ONLY a JSON object:
{
  "stages": [
    {"name": "stage_name", "purpose": "one line", "sql": "SELECT ...",
     "depends_on": ["earlier_stage_or_source_table", ...]}
  ],
  "final": "name_of_last_stage",
  "explanation": "2-3 sentence plain-English summary of the pipeline"
}"""


def _schema_brief(engine: DataEngine) -> str:
    """Compact schema description for the planner prompt."""
    lines: List[str] = []
    for t in engine.list_tables():
        try:
            cols = engine.table_schema(t)
        except Exception:
            continue
        col_str = ", ".join(f"{c['name']} {c['type']}" for c in cols)
        lines.append(f"- {t}({col_str})")
    return "\n".join(lines) if lines else "(no tables connected)"


_READONLY_OK = re.compile(r"^\s*(with|select)\b", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|attach|copy|truncate|create|install|load)\b",
    re.IGNORECASE,
)


def _validate_stage_sql(sql: str) -> None:
    if not _READONLY_OK.match(sql or ""):
        raise NL2SQLError("stage SQL must start with SELECT or WITH")
    if _FORBIDDEN.search(sql or ""):
        raise NL2SQLError("stage SQL must be read-only (no DDL/DML)")


def plan_pipeline(
    engine: DataEngine, request: str, *, config: Optional[Any] = None
) -> Dict[str, Any]:
    """Ask the LLM to produce a multi-stage SQL pipeline plan (not executed yet)."""
    llm = get_llm(config)
    schema = _schema_brief(engine)
    user = (
        f"SCHEMA (DuckDB tables already loaded):\n{schema}\n\n"
        f"REQUEST:\n{request}\n\n"
        "Design the multi-stage SQL pipeline now. Return only the JSON object."
    )
    plan = llm.chat_json(
        [{"role": "system", "content": _PLAN_SYSTEM}, {"role": "user", "content": user}],
        max_output_tokens=4000,
    )
    stages = plan.get("stages") or []
    if not stages:
        raise NL2SQLError("planner returned no stages")
    # normalize + validate
    for s in stages:
        s["name"] = _safe_ident(s.get("name", "stage"))
        _validate_stage_sql(s.get("sql", ""))
        s.setdefault("depends_on", [])
        s.setdefault("purpose", "")
    plan["stages"] = stages
    plan.setdefault("final", stages[-1]["name"])
    return plan


def execute_pipeline(
    engine: DataEngine, plan: Dict[str, Any], *, preview_rows: int = 8
) -> Dict[str, Any]:
    """Execute each stage in order, materializing intermediate tables.

    Returns a structure with per-stage status + preview + a DAG (nodes/edges)
    for the frontend to render.
    """
    stages = plan.get("stages", [])
    known_tables = set(engine.list_tables())
    executed: List[Dict[str, Any]] = []
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, str]] = []

    # source tables become DAG nodes too
    for t in known_tables:
        nodes.append({"id": t, "kind": "source", "label": t})

    for i, stage in enumerate(stages):
        name = stage["name"]
        sql = stage["sql"]
        rec: Dict[str, Any] = {
            "name": name,
            "purpose": stage.get("purpose", ""),
            "sql": sql,
            "order": i,
        }
        try:
            _validate_stage_sql(sql)
            # materialize the stage as a view so later stages can reference it
            engine.con.execute(f"CREATE OR REPLACE VIEW {name} AS {sql}")
            engine._table_source[name] = "__pipeline__"
            # preview + row count
            rt = engine.run_query(f"SELECT * FROM {name} LIMIT {int(preview_rows)}")
            total = engine.con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            rec.update(
                {
                    "status": "ok",
                    "row_count": total,
                    "columns": rt.column_names,
                    "preview": rt.rows,
                }
            )
        except Exception as exc:
            rec.update({"status": "error", "error": str(exc)})
            executed.append(rec)
            # stop pipeline on first failure (later stages likely depend on this)
            break

        # DAG node + edges
        nodes.append({"id": name, "kind": "stage", "label": name, "purpose": rec["purpose"]})
        for dep in stage.get("depends_on", []):
            dep_id = _safe_ident(dep)
            edges.append({"from": dep_id, "to": name})
        executed.append(rec)

    final = plan.get("final")
    final_result = None
    if executed and executed[-1].get("status") == "ok":
        try:
            frt = engine.run_query(f"SELECT * FROM {_safe_ident(final)} LIMIT 200")
            final_result = {
                "table": final,
                "columns": frt.column_names,
                "rows": frt.rows,
                "row_count": frt.row_count,
            }
        except Exception:
            pass

    return {
        "stages": executed,
        "dag": {"nodes": nodes, "edges": edges},
        "final": final,
        "final_result": final_result,
        "explanation": plan.get("explanation", ""),
        "n_stages": len(stages),
        "n_ok": sum(1 for s in executed if s.get("status") == "ok"),
    }


def run_nl2sql(
    engine: DataEngine, request: str, *, config: Optional[Any] = None, preview_rows: int = 8
) -> Dict[str, Any]:
    """Full flow: plan the pipeline, then execute it. Returns plan + results."""
    plan = plan_pipeline(engine, request, config=config)
    result = execute_pipeline(engine, plan, preview_rows=preview_rows)
    result["request"] = request
    return result
