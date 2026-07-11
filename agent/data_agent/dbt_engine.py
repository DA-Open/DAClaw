"""Auto-DBT engine — natural language → a dbt-style layered data pipeline.

Given the connected DuckDB tables and a plain-English goal, an LLM designs a
set of dbt-style *models* organized in layers (staging → intermediate → marts).
Each model is a single SELECT that references upstream models via ``ref('name')``
and raw tables via ``source('name')``. We:

  1. parse ref()/source() calls into a dependency DAG,
  2. topologically sort it (Kahn's algorithm, with cycle detection),
  3. compile ref()/source() → real DuckDB identifiers,
  4. materialize each model as a table/view (CREATE OR REPLACE …),
  5. capture per-model row counts, previews, and errors,
  6. emit the lineage graph (nodes coloured by layer, edges = ref deps).

This is a faithful offline analog of ``dbt run`` (no real dbt install needed),
modeled on the DAComp-DE benchmark's offline executor and the existing
``nl2sql.py`` materialization idiom (CREATE via ``engine.con.execute``, preview
via ``engine.run_query``).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from agent.data_agent.engine import DataEngine, _safe_ident
from agent.data_agent.llm import get_llm


class DbtError(RuntimeError):
    pass


LAYERS = ("staging", "intermediate", "marts")

# read-only guard on each model body (before ref/source compilation)
_READONLY_OK = re.compile(r"^\s*(with|select)\b", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|attach|copy|truncate|create|install|load)\b",
    re.IGNORECASE,
)

# ref('x') and source('x') / source('src','x')  (single or double quotes)
_REF_RE = re.compile(r"""ref\(\s*['"]([^'"]+)['"]\s*\)""", re.IGNORECASE)
_SRC_RE = re.compile(
    r"""source\(\s*['"]([^'"]+)['"]\s*(?:,\s*['"]([^'"]+)['"]\s*)?\)""", re.IGNORECASE
)


_PLAN_SYSTEM = (
    "You are a senior analytics engineer who designs dbt projects. Given the raw "
    "tables already loaded in a DuckDB warehouse and a business goal, design a "
    "dbt-style model DAG.\n\n"
    "LAYER TAGS (used for coloring, NOT a rigid column count):\n"
    "- staging  (name: stg_<entity>): light cleaning per source; reads source('raw').\n"
    "- intermediate (name: int_<concept>): joins/derivations/aggregations over "
    "staging AND other intermediate models. You may chain MANY intermediate "
    "models at different depths (int_a → int_b → int_c) — real pipelines are "
    "deep, branching trees, not a flat 3-column strip.\n"
    "- marts (name: fct_/dim_/<business>): analytics-ready outputs. There can be "
    "SEVERAL marts, and a mart may feed another mart.\n\n"
    "TOPOLOGY — build a REALISTIC, COMPLEX DAG, not a tidy 3-layer stack:\n"
    "- Depth is free: a model may depend on models several steps upstream, and "
    "edges may skip 'layers' (a mart can read a staging model directly).\n"
    "- Fan-out: one model can feed many downstream models. Fan-in: one model can "
    "join many upstreams. Reuse shared intermediates across branches.\n"
    "- There is NO fixed number of layers — use as many derivation steps as the "
    "goal naturally needs.\n\n"
    "RULES:\n"
    "- Every model is ONE read-only SELECT (DuckDB dialect). No DDL/DML — the "
    "runtime wraps it in CREATE TABLE.\n"
    "- Reference upstream MODELS only via ref('model_name'); reference RAW tables "
    "only via source('raw_table_name'). Never hardcode a raw table name in a "
    "FROM without source(); never FROM a model without ref().\n"
    "- model names are snake_case, unique, and equal the produced table name.\n"
    "- No cycles.\n"
    "- Aim for a rich pipeline (typically 6-14 models) with real branching; "
    "always end with marts model(s) that directly answer the goal.\n\n"
    "Return ONLY a JSON object:\n"
    "{\n"
    '  "project": "short_snake_name",\n'
    '  "explanation": "2-3 sentences describing the pipeline",\n'
    '  "models": [\n'
    '    {"name": "stg_orders", "layer": "staging", "materialized": "view", '
    '"description": "one line", "sql": "SELECT ... FROM source(\'orders\')"},\n'
    '    {"name": "int_order_metrics", "layer": "intermediate", "materialized": '
    '"table", "description": "...", "sql": "SELECT ... FROM ref(\'stg_orders\')"},\n'
    '    {"name": "fct_sales", "layer": "marts", "materialized": "table", '
    '"description": "...", "sql": "SELECT ... FROM ref(\'int_order_metrics\')"}\n'
    "  ]\n"
    "}"
)


def _schema_brief(engine: DataEngine) -> str:
    lines: List[str] = []
    for t in engine.list_tables():
        try:
            cols = engine.table_schema(t)
        except Exception:
            continue
        col_str = ", ".join(f"{c['name']} {c['type']}" for c in cols)
        lines.append(f"- {t}({col_str})")
    return "\n".join(lines) if lines else "(no tables connected)"


def _validate_model_sql(sql: str) -> None:
    body = sql or ""
    if not _READONLY_OK.match(body):
        raise DbtError("model SQL must start with SELECT or WITH")
    # strip ref()/source() before checking forbidden words so "create" inside
    # a function name never trips it (defensive; unlikely).
    stripped = _SRC_RE.sub(" _src_ ", _REF_RE.sub(" _ref_ ", body))
    if _FORBIDDEN.search(stripped):
        raise DbtError("model SQL must be read-only (no DDL/DML)")


def _refs_of(sql: str) -> List[str]:
    return [m.group(1) for m in _REF_RE.finditer(sql or "")]


def _sources_of(sql: str) -> List[str]:
    out = []
    for m in _SRC_RE.finditer(sql or ""):
        # source('raw') or source('src','raw') → take the table name (last group)
        out.append(m.group(2) or m.group(1))
    return out


def plan_project(engine: DataEngine, goal: str, *, config: Optional[Any] = None) -> Dict[str, Any]:
    """Ask the LLM to design the dbt project (models not yet executed)."""
    llm = get_llm(config)
    schema = _schema_brief(engine)
    user = (
        f"RAW TABLES (already loaded in DuckDB):\n{schema}\n\n"
        f"BUSINESS GOAL:\n{goal}\n\n"
        "Design the layered dbt model DAG now. Return only the JSON object."
    )
    plan = llm.chat_json(
        [{"role": "system", "content": _PLAN_SYSTEM}, {"role": "user", "content": user}],
        max_output_tokens=16000,
    )
    models = plan.get("models") or []
    if not models:
        raise DbtError("planner returned no models")
    for m in models:
        m["name"] = _safe_ident(m.get("name", "model"))
        layer = str(m.get("layer", "")).lower()
        m["layer"] = layer if layer in LAYERS else "marts"
        m["materialized"] = "view" if str(m.get("materialized", "table")).lower() == "view" else "table"
        m.setdefault("description", "")
        _validate_model_sql(m.get("sql", ""))
    plan["models"] = models
    plan.setdefault("project", "dbt_project")
    plan.setdefault("explanation", "")
    return plan


def _topo_order(models: List[Dict[str, Any]]) -> Tuple[List[str], Dict[str, List[str]]]:
    """Kahn topological sort over ref() edges. Returns (order, deps_map).

    Raises DbtError on a cycle.
    """
    names = {m["name"] for m in models}
    deps: Dict[str, List[str]] = {}
    for m in models:
        model_refs = [r for r in _refs_of(m["sql"]) if _safe_ident(r) in names or r in names]
        # normalize ref names to _safe_ident to match model names
        deps[m["name"]] = sorted({_safe_ident(r) for r in model_refs if _safe_ident(r) in names})

    indeg = {n: 0 for n in names}
    adj: Dict[str, List[str]] = {n: [] for n in names}
    for n, ds in deps.items():
        for d in ds:
            adj[d].append(n)
            indeg[n] += 1

    # deterministic: process ready nodes sorted by (layer priority, name)
    layer_rank = {m["name"]: LAYERS.index(m["layer"]) if m["layer"] in LAYERS else 99 for m in models}
    ready = sorted([n for n in names if indeg[n] == 0], key=lambda n: (layer_rank.get(n, 99), n))
    order: List[str] = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for nb in adj[n]:
            indeg[nb] -= 1
            if indeg[nb] == 0:
                ready.append(nb)
        ready.sort(key=lambda n: (layer_rank.get(n, 99), n))
    if len(order) != len(names):
        cyclic = sorted(names - set(order))
        raise DbtError(f"cyclic ref() dependency among models: {', '.join(cyclic)}")
    return order, deps


def _compile_sql(sql: str, model_names: Set[str]) -> str:
    """Rewrite ref('x') → x and source('t') / source('s','t') → t (DuckDB idents)."""
    def _ref_sub(m: "re.Match[str]") -> str:
        return _safe_ident(m.group(1))

    def _src_sub(m: "re.Match[str]") -> str:
        return _safe_ident(m.group(2) or m.group(1))

    out = _REF_RE.sub(_ref_sub, sql)
    out = _SRC_RE.sub(_src_sub, out)
    return out


def run_dbt(
    engine: DataEngine,
    goal: str,
    *,
    config: Optional[Any] = None,
    preview_rows: int = 8,
) -> Dict[str, Any]:
    """Plan + execute a dbt-style pipeline. Returns models + lineage DAG."""
    plan = plan_project(engine, goal, config=config)
    models = plan["models"]
    by_name = {m["name"]: m for m in models}
    model_names = set(by_name)

    raw_tables = set(engine.list_tables())
    order, deps = _topo_order(models)

    # ---- lineage DAG (nodes coloured by layer; edges = ref + source) --------
    nodes: List[Dict[str, Any]] = []
    seen_sources: Set[str] = set()
    edges: List[Dict[str, str]] = []
    for m in models:
        nodes.append({
            "id": m["name"], "kind": "model", "layer": m["layer"],
            "label": m["name"], "description": m.get("description", ""),
            "materialized": m["materialized"],
        })
        for r in _refs_of(m["sql"]):
            rn = _safe_ident(r)
            if rn in model_names:
                edges.append({"from": rn, "to": m["name"]})
        for s in _sources_of(m["sql"]):
            sn = _safe_ident(s)
            if sn in raw_tables:
                if sn not in seen_sources:
                    nodes.append({"id": sn, "kind": "source", "layer": "raw", "label": sn})
                    seen_sources.add(sn)
                edges.append({"from": sn, "to": m["name"]})

    # ---- execute in topological order ---------------------------------------
    executed: List[Dict[str, Any]] = []
    failed: Set[str] = set()
    for name in order:
        m = by_name[name]
        rec: Dict[str, Any] = {
            "name": name, "layer": m["layer"], "materialized": m["materialized"],
            "description": m.get("description", ""), "sql": m["sql"],
            "order": len(executed),
        }
        # skip if an upstream dependency failed
        upstream_failed = [d for d in deps.get(name, []) if d in failed]
        if upstream_failed:
            rec.update({"status": "skipped", "error": f"upstream failed: {', '.join(upstream_failed)}"})
            executed.append(rec)
            failed.add(name)
            continue
        try:
            compiled = _compile_sql(m["sql"], model_names)
            rec["compiled_sql"] = compiled
            kw = "VIEW" if m["materialized"] == "view" else "TABLE"
            engine.con.execute(f"CREATE OR REPLACE {kw} {name} AS {compiled}")
            engine._table_source[name] = "__dbt__"
            rt = engine.run_query(f"SELECT * FROM {name} LIMIT {int(preview_rows)}")
            total = engine.con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            rec.update({
                "status": "ok", "row_count": int(total),
                "columns": rt.column_names, "preview": rt.rows,
            })
        except Exception as exc:  # noqa: BLE001
            rec.update({"status": "error", "error": str(exc)})
            failed.add(name)
        executed.append(rec)

    n_ok = sum(1 for r in executed if r["status"] == "ok")
    marts = [r["name"] for r in executed if r["layer"] == "marts" and r["status"] == "ok"]
    final = marts[-1] if marts else (order[-1] if order else None)
    final_result = None
    if final and by_name.get(final) and any(r["name"] == final and r["status"] == "ok" for r in executed):
        try:
            frt = engine.run_query(f"SELECT * FROM {final} LIMIT 200")
            final_result = {
                "table": final, "columns": frt.column_names,
                "rows": frt.rows, "row_count": frt.row_count,
            }
        except Exception:
            final_result = None

    return {
        "project": plan.get("project", "dbt_project"),
        "goal": goal,
        "explanation": plan.get("explanation", ""),
        "models": executed,
        "dag": {"nodes": nodes, "edges": edges},
        "layers": list(LAYERS),
        "final": final,
        "final_result": final_result,
        "n_models": len(models),
        "n_ok": n_ok,
    }


def export_project(result: Dict[str, Any], out_dir: str) -> Dict[str, Any]:
    """Write the pipeline as a real dbt project layout (models/<layer>/*.sql +
    dbt_project.yml + a schema.yml). Returns file manifest."""
    import os

    project = _safe_ident(result.get("project", "dbt_project"))
    root = os.path.join(out_dir, project)
    written: List[str] = []
    for m in result.get("models", []):
        layer = m.get("layer", "marts")
        d = os.path.join(root, "models", layer)
        os.makedirs(d, exist_ok=True)
        # keep ref()/source() in the exported .sql (real dbt syntax)
        body = m.get("sql", "")
        mat = m.get("materialized", "table")
        header = f"{{{{ config(materialized='{mat}') }}}}\n\n"
        path = os.path.join(d, f"{m['name']}.sql")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(header + body + "\n")
        written.append(path)

    # dbt_project.yml
    os.makedirs(root, exist_ok=True)
    proj_yml = os.path.join(root, "dbt_project.yml")
    with open(proj_yml, "w", encoding="utf-8") as fh:
        fh.write(
            f"name: '{project}'\nversion: '1.0.0'\nprofile: '{project}'\n\n"
            "models:\n"
            f"  {project}:\n"
            "    staging:\n      +materialized: view\n"
            "    intermediate:\n      +materialized: table\n"
            "    marts:\n      +materialized: table\n"
        )
    written.append(proj_yml)
    return {"root": root, "files": written, "n_files": len(written)}
