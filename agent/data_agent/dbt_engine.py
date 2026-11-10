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
    "- COLUMN VALUES: under each raw table the brief lists the real values of its "
    "low-cardinality text columns (with row counts) and sample values of the "
    "others. Whenever SQL compares or maps literal values (WHERE x = '...', "
    "CASE WHEN x IN (...)), use exactly those values with exact spelling/case. "
    "Never invent values that are not listed. If a column is an ordinal scale "
    "(e.g. 'Very high' ... 'Low'), keep or explicitly map its real levels instead "
    "of forcing it into yes/no.\n"
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


# Value profiling for the planner: without real values the LLM guesses the
# literals it filters/maps on (e.g. 'yes'/'no' for a 4-level 'Very high'..
# 'Relatively low' scale) and the pipeline "succeeds" with empty results.
_PROFILE_MAX_DISTINCT = 12   # list every value of a text column up to this many
_PROFILE_SAMPLES = 3         # otherwise show this many example values
_PROFILE_VALUE_CHARS = 40
_BRIEF_MAX_CHARS = 16000


def _quote_col(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _is_text_type(ctype: str) -> bool:
    t = (ctype or "").upper()
    return any(k in t for k in ("VARCHAR", "CHAR", "TEXT", "STRING"))


def _fmt_value(v: Any) -> str:
    s = str(v)
    if len(s) > _PROFILE_VALUE_CHARS:
        s = s[: _PROFILE_VALUE_CHARS - 3] + "..."
    return "'" + s.replace("'", "''") + "'"


def _column_profile(engine: DataEngine, table: str, col: str) -> Optional[str]:
    """One line describing the real values of a text column, or None."""
    q = _quote_col(col)
    t = _safe_ident(table)
    try:
        n_distinct = engine.run_query(f"SELECT COUNT(DISTINCT {q}) FROM {t}").rows[0][0]
        if not n_distinct:
            return f"{col}: always NULL"
        if n_distinct <= _PROFILE_MAX_DISTINCT:
            rows = engine.run_query(
                f"SELECT {q}, COUNT(*) FROM {t} WHERE {q} IS NOT NULL "
                f"GROUP BY 1 ORDER BY 2 DESC, 1"
            ).rows
            vals = ", ".join(f"{_fmt_value(v)} ({n})" for v, n in rows)
            return f"{col}: {vals}"
        rows = engine.run_query(
            f"SELECT DISTINCT {q} FROM {t} WHERE {q} IS NOT NULL LIMIT {_PROFILE_SAMPLES}"
        ).rows
        samples = ", ".join(_fmt_value(r[0]) for r in rows)
        return f"{col}: {n_distinct} distinct, e.g. {samples}"
    except Exception:
        return None


def _schema_brief(engine: DataEngine, *, with_values: bool = True) -> str:
    lines: List[str] = []
    for t in engine.list_tables():
        try:
            cols = engine.table_schema(t)
        except Exception:
            continue
        col_str = ", ".join(f"{c['name']} {c['type']}" for c in cols)
        lines.append(f"- {t}({col_str})")
        # profile raw tables only; models from an earlier dbt run are derived
        if not with_values or engine._table_source.get(t) == "__dbt__":
            continue
        for c in cols:
            if _is_text_type(c["type"]):
                prof = _column_profile(engine, t, c["name"])
                if prof:
                    lines.append(f"    {prof}")
    brief = "\n".join(lines) if lines else "(no tables connected)"
    if len(brief) > _BRIEF_MAX_CHARS:
        brief = brief[:_BRIEF_MAX_CHARS] + "\n    ... (value listing truncated)"
    return brief


# ---- post-run quality checks ------------------------------------------------
# A model can execute fine and still be useless (every metric NULL, every
# label 'unknown'). These checks surface that instead of reporting "ok".
_PLACEHOLDER_VALUES = {"unknown", "other", "others", "n/a", "na", "none", "null", "", "未知", "其他"}
_NUMERIC_HINTS = ("INT", "DOUBLE", "FLOAT", "DECIMAL", "NUMERIC", "REAL", "HUGEINT")


def _check_model(engine: DataEngine, name: str, row_count: int) -> List[Tuple[str, str]]:
    """Return [(severity, message)]. 'error' = the column carries no
    information (all NULL / all a placeholder label) and triggers a repair;
    'warning' = all zero, which is often legitimate (e.g. a missing-date count)
    so it is only reported."""
    if row_count < 2:
        return []
    try:
        cols = engine.table_schema(name)[:60]
    except Exception:
        return []
    if not cols:
        return []
    parts: List[str] = []
    for c in cols:
        q = _quote_col(c["name"])
        parts += [f"COUNT({q})", f"COUNT(DISTINCT {q})", f"MIN({q})", f"MAX({q})"]
    try:
        row = engine.run_query(f"SELECT {', '.join(parts)} FROM {_safe_ident(name)}").rows[0]
    except Exception:
        return []
    issues: List[Tuple[str, str]] = []
    for i, c in enumerate(cols):
        non_null, n_distinct, lo, hi = row[4 * i: 4 * i + 4]
        col, ctype = c["name"], c["type"].upper()
        if non_null == 0:
            issues.append(("error", f"column '{col}' is NULL in all {row_count} rows"))
        elif n_distinct == 1 and str(lo).strip().lower() in _PLACEHOLDER_VALUES:
            issues.append(("error", f"column '{col}' is {_fmt_value(lo)} in all {row_count} rows"))
        elif any(h in ctype for h in _NUMERIC_HINTS) and lo == 0 and hi == 0:
            issues.append(("warning", f"column '{col}' is 0 in all {row_count} rows"))
    return issues


def _quality_issues(engine: DataEngine, executed: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Check intermediate and marts models (staging only passes raw columns
    through, so an all-NULL raw column is not the pipeline's fault)."""
    found: List[Dict[str, str]] = []
    for rec in executed:
        rec.pop("warnings", None)
        if rec.get("status") != "ok" or rec.get("layer") == "staging":
            continue
        issues = _check_model(engine, rec["name"], int(rec.get("row_count") or 0))
        if issues:
            rec["warnings"] = [msg for _, msg in issues]
            found += [{"model": rec["name"], "severity": sev, "issue": msg} for sev, msg in issues]
    return found


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


def plan_project(
    engine: DataEngine,
    goal: str,
    *,
    config: Optional[Any] = None,
    schema_brief: Optional[str] = None,
) -> Dict[str, Any]:
    """Ask the LLM to design the dbt project (models not yet executed)."""
    llm = get_llm(config)
    schema = schema_brief if schema_brief is not None else _schema_brief(engine)
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
    repair: bool = True,
) -> Dict[str, Any]:
    """Plan + execute a dbt-style pipeline. Returns models + lineage DAG.

    After executing, every intermediate/marts model is checked for degenerate
    output. If any check fails with severity 'error', the LLM gets the issues
    plus the real column values and may rewrite models once (``repair``);
    the pipeline is then re-executed and re-checked.
    """
    raw_tables = set(engine.list_tables())
    brief = _schema_brief(engine)
    plan = plan_project(engine, goal, config=config, schema_brief=brief)
    models = plan["models"]

    executed, order, nodes, edges = _execute(engine, models, raw_tables, preview_rows)
    issues = _quality_issues(engine, executed)

    repair_info: Dict[str, Any] = {"attempted": False}
    if repair and any(i["severity"] == "error" for i in issues):
        repair_info = {"attempted": True, "issues_before": issues}
        try:
            changed, why = _repair_models(engine, goal, models, issues, brief, config=config)
            repair_info.update({"changed": sorted(changed), "explanation": why})
            if changed:
                by_name = {m["name"]: m for m in models}
                for m in changed.values():
                    by_name[m["name"]].update({"sql": m["sql"], "repaired": True})
                executed, order, nodes, edges = _execute(engine, models, raw_tables, preview_rows)
                issues = _quality_issues(engine, executed)
        except Exception as exc:  # noqa: BLE001 — repair is best effort
            repair_info["error"] = str(exc)
        repair_info["issues_after"] = issues

    by_name = {m["name"]: m for m in models}
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
        "quality_issues": issues,
        "repair": repair_info,
    }


_REPAIR_SYSTEM = (
    "You are a senior analytics engineer reviewing a dbt pipeline that ran "
    "without SQL errors but produced degenerate output (columns that are NULL "
    "or a placeholder like 'unknown' in every row). The usual cause is SQL that "
    "filters or maps on literal values that do not exist in the data. You get "
    "the raw tables with their REAL column values, the business goal, every "
    "model's SQL, and the detected issues.\n\n"
    "Fix the root cause, which may be in an upstream model rather than the one "
    "where the symptom shows. Use only the real values listed. Keep model names, "
    "ref()/source() conventions and output columns the downstream models rely "
    "on; each model stays ONE read-only SELECT (DuckDB). Change only models that "
    "need it. If an issue is legitimate (the data really is empty there), leave "
    "it and say so.\n\n"
    "Return ONLY a JSON object:\n"
    '{"explanation": "what was wrong and what you changed", '
    '"models": [{"name": "existing_model_name", "sql": "SELECT ..."}]}'
)


def _repair_models(
    engine: DataEngine,
    goal: str,
    models: List[Dict[str, Any]],
    issues: List[Dict[str, str]],
    brief: str,
    *,
    config: Optional[Any] = None,
) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """One LLM pass that rewrites models behind 'error' issues.

    Returns ({name: {name, sql}} for valid changed models, explanation).
    Unknown names and SQL that fails validation are dropped.
    """
    llm = get_llm(config)
    model_txt = "\n\n".join(
        f"-- {m['name']} ({m['layer']})\n{m['sql']}" for m in models
    )
    issue_txt = "\n".join(f"- [{i['severity']}] {i['model']}: {i['issue']}" for i in issues)
    user = (
        f"RAW TABLES (with real values):\n{brief}\n\n"
        f"BUSINESS GOAL:\n{goal}\n\n"
        f"CURRENT MODELS:\n{model_txt}\n\n"
        f"DETECTED ISSUES:\n{issue_txt}\n\n"
        "Return only the JSON object."
    )
    out = llm.chat_json(
        [{"role": "system", "content": _REPAIR_SYSTEM}, {"role": "user", "content": user}],
        max_output_tokens=16000,
    )
    names = {m["name"] for m in models}
    changed: Dict[str, Dict[str, Any]] = {}
    for m in out.get("models") or []:
        name = _safe_ident(str(m.get("name", "")))
        sql = m.get("sql") or ""
        if name not in names or not sql:
            continue
        try:
            _validate_model_sql(sql)
        except DbtError:
            continue
        changed[name] = {"name": name, "sql": sql}
    return changed, str(out.get("explanation", ""))


def _execute(
    engine: DataEngine,
    models: List[Dict[str, Any]],
    raw_tables: Set[str],
    preview_rows: int,
) -> Tuple[List[Dict[str, Any]], List[str], List[Dict[str, Any]], List[Dict[str, str]]]:
    """Build the lineage DAG and materialize every model in topological order.

    Returns (executed records, order, dag nodes, dag edges).
    """
    by_name = {m["name"]: m for m in models}
    model_names = set(by_name)
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
        if m.get("repaired"):
            rec["repaired"] = True
        executed.append(rec)

    return executed, order, nodes, edges


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
