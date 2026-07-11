"""FastAPI routes for the DataAgent demo (Part 1 workbench + Part 2 leaderboard).

Kept in its own module and mounted via ``app.include_router`` so the large
``web_server.py`` stays readable. All heavy work delegates to the
``agent/data_agent/`` service layer, so CLI / web / gateway share one impl.

Part 1 endpoints (interactive Data Agent):
  POST /api/da/connect        connect an uploaded data file
  GET  /api/da/tables         list connected tables + schema
  POST /api/da/query          run read-only SQL, return a ResultTable envelope
  POST /api/da/nl2sql         NL -> multi-stage SQL pipeline (executed) + DAG
  POST /api/da/visualize      SQL -> self-contained ECharts chart HTML
  POST /api/da/dashboard      auto-overview dashboard for a table
  POST /api/da/excel          Excel op (chart/dashboard/repair) -> .xlsx path
  POST /api/da/chat           chatbot turn (LLM, tool-aware planning)

Part 2 endpoints (leaderboard):
  GET  /api/lb/overview       harness ranking + benchmark matrix + best combos
  GET  /api/lb/raw            raw entries + taxonomy
  POST /api/lb/evaluate       (framework) queue a Model x Harness eval → seed row
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from fastapi import APIRouter, Body, HTTPException
except Exception:  # pragma: no cover
    APIRouter = None  # type: ignore
    HTTPException = Exception  # type: ignore

logger = logging.getLogger(__name__)

router = APIRouter() if APIRouter else None


def _engine():
    from agent.data_agent.engine import get_engine

    return get_engine()


def _da_output_dir() -> Path:
    """Where generated artifacts (html/xlsx) are written so the UI can serve them."""
    try:
        from daclaw_cli.web_server import _workspace_upload_root

        out = Path(_workspace_upload_root()) / "data_agent"
    except Exception:
        out = Path.cwd() / "data_agent_output"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _uploaded_path(value: str, *, suffixes: set[str]) -> Path:
    """Resolve a browser-uploaded file and keep it inside the workspace sandbox."""
    from fastapi import HTTPException
    from daclaw_cli.web_server import _workspace_upload_root

    root = Path(_workspace_upload_root()).resolve()
    target = Path(value or "").expanduser().resolve()
    if not target.is_relative_to(root):
        raise HTTPException(status_code=400, detail="Uploaded file must be inside the DAClaw workspace.")
    if target.suffix.lower() not in suffixes:
        raise HTTPException(status_code=400, detail=f"Unexpected file type: {target.suffix or '(none)'}")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Uploaded file not found.")
    return target


if router is not None:

    # ---------------- Part 1: interactive Data Agent ----------------------

    @router.post("/api/da/connect")
    async def da_connect(payload: Dict[str, Any] = Body(...)):
        from agent.data_agent.engine import DataEngineError

        source = payload.get("source", "")
        try:
            desc = _engine().connect_source(source, name=payload.get("name"))
            eng = _engine()
            tables = [{"table": t, "schema": eng.table_schema(t)} for t in desc["tables"]]
            return {"ok": True, "connected": desc["slug"], "kind": desc["kind"],
                    "tables": tables, "all_tables": eng.list_tables()}
        except DataEngineError as exc:
            return {"ok": False, "error": str(exc)}
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("da_connect failed")
            return {"ok": False, "error": str(exc)}

    @router.get("/api/da/db-types")
    async def da_db_types():
        """Supported live-database types for the connect dialog."""
        return {
            "types": [
                {"id": "postgres", "label": "PostgreSQL", "fields": ["host", "port", "user", "password", "database"], "default_port": 5432},
                {"id": "mysql", "label": "MySQL / MariaDB", "fields": ["host", "port", "user", "password", "database"], "default_port": 3306},
                {"id": "snowflake", "label": "Snowflake", "fields": ["host", "user", "password", "database"], "hint": "host = account id"},
                {"id": "mssql", "label": "SQL Server", "fields": ["host", "port", "user", "password", "database"], "default_port": 1433},
                {"id": "sqlite", "label": "SQLite (path)", "fields": ["database"]},
                {"id": "duckdb", "label": "DuckDB (path)", "fields": ["database"]},
            ],
        }

    @router.post("/api/da/connect-db")
    async def da_connect_db(payload: Dict[str, Any] = Body(...)):
        """Connect a LIVE external database via DSN or structured params."""
        from agent.data_agent.engine import DataEngineError

        dbtype = (payload.get("dbtype") or payload.get("type") or "").strip().lower()
        try:
            eng = _engine()
            # sqlite/duckdb file paths still go through the file connector
            if dbtype in ("sqlite", "duckdb") and payload.get("database") and not payload.get("dsn"):
                allowed = {".sqlite", ".sqlite3", ".db", ".db3"} if dbtype == "sqlite" else {".duckdb", ".ddb"}
                target = _uploaded_path(payload["database"], suffixes=allowed)
                desc = eng.connect_source(str(target), name=payload.get("name"))
            else:
                desc = eng.connect_database(
                    dbtype=dbtype,
                    dsn=payload.get("dsn"),
                    host=payload.get("host"),
                    port=payload.get("port"),
                    user=payload.get("user"),
                    password=payload.get("password"),
                    database=payload.get("database"),
                    schema=payload.get("schema"),
                    name=payload.get("name"),
                )
            tables = [{"table": t, "schema": eng.table_schema(t)} for t in desc["tables"]]
            return {"ok": True, "connected": desc["slug"], "kind": desc["kind"],
                    "dbtype": desc.get("dbtype"), "via": desc.get("via"),
                    "tables": tables, "all_tables": eng.list_tables()}
        except DataEngineError as exc:
            return {"ok": False, "error": str(exc)}
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("da_connect_db failed")
            return {"ok": False, "error": str(exc)}

    @router.get("/api/da/tables")
    async def da_tables():
        eng = _engine()
        out = []
        for t in eng.list_tables():
            try:
                schema = eng.table_schema(t)
            except Exception:
                schema = []
            row_count = None
            try:
                rt = eng.run_query(f'SELECT COUNT(*) AS n FROM "{t}"')
                if rt.rows:
                    row_count = rt.rows[0][0]
            except Exception:
                row_count = None
            out.append({"table": t, "schema": schema, "row_count": row_count})
        return {"tables": out}

    @router.get("/api/da/knowledge")
    async def da_knowledge_sources():
        from agent.data_agent import knowledge

        return {"sources": knowledge.list_sources()}

    @router.post("/api/da/knowledge/connect")
    async def da_knowledge_connect(payload: Dict[str, Any] = Body(...)):
        from agent.data_agent import knowledge

        try:
            target = _uploaded_path(
                payload["source"],
                suffixes={".txt", ".md", ".markdown", ".json", ".html", ".htm", ".pdf", ".docx"},
            )
            return {"ok": True, **knowledge.connect_document(str(target), name=payload.get("name"))}
        except HTTPException:
            raise
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.delete("/api/da/knowledge/{source_id}")
    async def da_knowledge_delete(source_id: str):
        from agent.data_agent import knowledge

        return {"ok": knowledge.remove_source(source_id)}

    @router.post("/api/da/knowledge/search")
    async def da_knowledge_search(payload: Dict[str, Any] = Body(...)):
        from agent.data_agent import knowledge

        return {"ok": True, **knowledge.search(payload["query"], source_ids=payload.get("source_ids"), limit=int(payload.get("limit", 8)))}

    @router.post("/api/da/workflow/plan")
    async def da_workflow_plan(payload: Dict[str, Any] = Body(...)):
        from agent.data_agent import knowledge, workflow

        tables = payload.get("tables") or _engine().list_tables()
        plan = workflow.draft_workflow(
                payload["goal"],
                tables=tables,
                knowledge_sources=[item["source_id"] for item in knowledge.list_sources()],
            )
        workflow.save_workflow(plan)
        return {"ok": True, **plan}

    @router.get("/api/da/workflows")
    async def da_workflows():
        from agent.data_agent import workflow

        return {"workflows": workflow.list_workflows()}

    @router.post("/api/da/workflow/compile")
    async def da_workflow_compile(payload: Dict[str, Any] = Body(...)):
        from agent.data_agent import workflow

        return {"ok": True, **workflow.compile_workflow(payload)}

    @router.get("/api/da/table/{table}/sample")
    async def da_table_sample(table: str, limit: int = 25):
        """Sample rows + per-column stats for the left DB browser."""
        from agent.data_agent.engine import DataEngineError

        try:
            prof = _engine().profile_table(table, sample_rows=int(limit))
            return {
                "ok": True,
                "table": table,
                "row_count": prof.get("row_count"),
                "columns": [c["name"] for c in (prof.get("schema") or [])] or None,
                "sample_rows": prof.get("sample_rows"),
                "column_stats": prof.get("column_stats"),
            }
        except DataEngineError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            logger.exception("da_table_sample failed")
            return {"ok": False, "error": str(exc)}

    @router.get("/api/da/suggest")
    async def da_suggest(limit: int = 4):
        """Recommend a few starter questions inferred from the connected tables.

        Tries the LLM for natural-sounding questions; always has a deterministic
        schema-heuristic fallback (time→trend, measure→top-N, category→breakdown)
        so it works even when no LLM is configured or it's slow.
        """
        try:
            eng = _engine()
            tables = eng.list_tables()
        except Exception:
            tables = []
        if not tables:
            return {"ok": True, "suggestions": []}

        # ---- deterministic heuristic (always available) --------------------
        def _heuristic() -> list[str]:
            out: list[str] = []
            for t in tables[:4]:
                try:
                    schema = eng.table_schema(t)
                except Exception:
                    continue
                from agent.data_agent.engine import _semantic_type
                roles: Dict[str, list] = {"time": [], "measure": [], "category": []}
                for col in schema:
                    r = _semantic_type(col["name"], str(col.get("type", "")).lower())
                    if r in roles:
                        roles[r].append(col["name"])
                if roles["measure"] and roles["category"]:
                    out.append(f"{roles['category'][0]} 维度下 {roles['measure'][0]} 最高的前 5 项(表 {t})")
                if roles["measure"] and roles["time"]:
                    out.append(f"按 {roles['time'][0]} 看 {roles['measure'][0]} 的变化趋势(表 {t})")
                if roles["category"] and not roles["measure"]:
                    out.append(f"{t} 按 {roles['category'][0]} 的记录数分布")
                if len(out) >= limit:
                    break
            if not out and tables:
                out.append(f"给我 {tables[0]} 的概览")
            return out[:limit]

        heuristic = _heuristic()

        # ---- LLM refinement (best-effort) ----------------------------------
        try:
            from agent.data_agent.llm import get_llm, llm_available

            if llm_available():
                brief = []
                for t in tables[:6]:
                    cols = ", ".join(c["name"] for c in eng.table_schema(t))
                    brief.append(f"- {t}({cols})")
                schema_text = "\n".join(brief)
                sys_msg = (
                    "You suggest short, concrete starter analytics questions a user "
                    "could ask about their connected database. Return ONLY JSON: "
                    '{"questions": ["...", "..."]}. Each question must be answerable '
                    "by SQL over the given tables, specific (name real columns/tables), "
                    "and one line. Match the language of the schema/user (Chinese if "
                    "the columns look Chinese, else English)."
                )
                user = f"TABLES:\n{schema_text}\n\nGive {limit} starter questions."
                data = get_llm().chat_json(
                    [{"role": "system", "content": sys_msg}, {"role": "user", "content": user}],
                    max_output_tokens=500,
                )
                qs = [str(q).strip() for q in (data.get("questions") or []) if str(q).strip()]
                if qs:
                    return {"ok": True, "suggestions": qs[:limit], "source": "llm"}
        except Exception:
            logger.info("da_suggest LLM refinement unavailable; using heuristic")

        return {"ok": True, "suggestions": heuristic, "source": "heuristic"}

    @router.post("/api/da/query")
    async def da_query(payload: Dict[str, Any] = Body(...)):
        from agent.data_agent.engine import DataEngineError

        try:
            rt = _engine().run_query(payload["sql"], max_rows=int(payload.get("max_rows", 100000)))
            return {"ok": True, **rt.envelope()}
        except DataEngineError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/api/da/nl2sql")
    async def da_nl2sql(payload: Dict[str, Any] = Body(...)):
        from agent.data_agent import nl2sql

        try:
            res = nl2sql.run_nl2sql(_engine(), payload["request"])
            return {"ok": True, **res}
        except Exception as exc:
            logger.exception("nl2sql failed")
            return {"ok": False, "error": str(exc)}

    # ---------------- ETL / auto-dbt --------------------------------------
    @router.post("/api/da/synthesize")
    async def da_synthesize(payload: Dict[str, Any] = Body(...)):
        """Synthesize LLM training data (from schema/SQL) or a research table
        (from a prompt). Writes files to the workspace and returns download URLs."""
        import time as _time
        from agent.data_agent import synth

        try:
            out_dir = _da_output_dir()
            stamp = str(int(_time.time()))[-6:]
            res = synth.run_synthesis(_engine(), payload, out_dir, stamp=stamp)
            for f in res.get("files", []):
                f["download_url"] = f"/api/da/download/{f['file']}"
            return res
        except synth.SynthError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            logger.exception("synthesize failed")
            return {"ok": False, "error": str(exc)}

    @router.post("/api/da/dbt/build")
    async def da_dbt_build(payload: Dict[str, Any] = Body(...)):
        """NL goal -> layered dbt-style pipeline, executed against the engine."""
        from agent.data_agent import dbt_engine

        try:
            res = dbt_engine.run_dbt(_engine(), payload["goal"])
            # export a real dbt project layout so the UI can offer a download
            try:
                out_dir = _da_output_dir()
                manifest = dbt_engine.export_project(res, str(out_dir))
                res["export"] = {"root": manifest["root"], "n_files": manifest["n_files"]}
            except Exception:
                logger.exception("dbt export failed")
            return {"ok": True, **res}
        except Exception as exc:
            logger.exception("dbt build failed")
            return {"ok": False, "error": str(exc)}

    @router.get("/api/da/dbt/model/{name}")
    async def da_dbt_model(name: str, limit: int = 50):
        """Fetch a materialized model's sample rows for the ETL node inspector."""
        from agent.data_agent.engine import DataEngineError

        try:
            eng = _engine()
            schema = [c["name"] for c in eng.table_schema(name)]
            rt = eng.run_query(f'SELECT * FROM "{name}" LIMIT {int(limit)}')
            return {
                "ok": True, "table": name, "columns": schema or rt.column_names,
                "rows": rt.rows, "row_count": rt.row_count,
            }
        except DataEngineError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            logger.exception("dbt model fetch failed")
            return {"ok": False, "error": str(exc)}

    @router.post("/api/da/visualize")
    async def da_visualize(payload: Dict[str, Any] = Body(...)):
        from agent.data_agent import viz

        try:
            rt = _engine().run_query(payload["sql"])
            html = viz.render_chart_html(
                rt,
                chart_type=payload.get("chart_type"),
                title=payload.get("title", "Chart"),
                subtitle=payload.get("subtitle", ""),
                provenance=payload["sql"],
            )
            return {"ok": True, "html": html, "chart_type": payload.get("chart_type") or viz.select_chart_type(rt),
                    "row_count": rt.row_count, "check": viz.self_check_html(html)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/api/da/dashboard")
    async def da_dashboard(payload: Dict[str, Any] = Body(...)):
        from agent.data_agent import overview, viz

        try:
            ov = overview.build_overview(_engine(), payload["table"])
            html = viz.render_dashboard_html(
                ov["panels"],
                title=payload.get("title", f"{payload['table']} — Overview"),
                subtitle=f"{ov['profile']['row_count']} rows",
            )
            return {"ok": True, "html": html, "charts": ov["chart_count"], "check": viz.self_check_html(html)}
        except Exception as exc:
            logger.exception("dashboard failed")
            return {"ok": False, "error": str(exc)}

    @router.post("/api/da/excel")
    async def da_excel(payload: Dict[str, Any] = Body(...)):
        from agent.data_agent import excel_ops

        op = payload.get("op", "dashboard")
        out_dir = _da_output_dir()
        try:
            eng = _engine()
            if op == "chart":
                rt = eng.run_query(payload["sql"])
                out = out_dir / (payload.get("filename") or "chart.xlsx")
                r = excel_ops.add_chart(rt, str(out), chart_type=payload.get("chart_type", "bar"),
                                        title=payload.get("title", "Chart"))
            elif op == "dashboard":
                panels = []
                for p in payload.get("panels", []):
                    rt = eng.run_query(p["sql"])
                    panels.append({"title": p.get("title", ""), "result": rt,
                                   "chart_type": p.get("chart_type", "bar")})
                out = out_dir / (payload.get("filename") or "dashboard.xlsx")
                r = excel_ops.build_dashboard(panels, str(out), title=payload.get("title", "Dashboard"))
            else:
                return {"ok": False, "error": f"unknown excel op: {op}"}
            r["ok"] = True
            r["file"] = Path(r["path"]).name
            r["download_url"] = f"/api/da/download/{Path(r['path']).name}"
            return r
        except Exception as exc:
            logger.exception("excel op failed")
            return {"ok": False, "error": str(exc)}

    @router.get("/api/da/download/{filename}")
    async def da_download(filename: str):
        from fastapi.responses import FileResponse
        from fastapi import HTTPException

        # only serve from the data_agent output dir; block path traversal
        safe = Path(filename).name
        target = _da_output_dir() / safe
        if not target.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        return FileResponse(str(target), filename=safe)

    @router.get("/api/da/view/{filename}")
    async def da_view(filename: str):
        """Serve a data_agent artifact INLINE (not as an attachment) so it can be
        rendered in an <iframe src> and its relative sub-resources (e.g. per-slide
        narration audio in a data-story) resolve against this same path."""
        from fastapi.responses import FileResponse
        from fastapi import HTTPException

        safe = Path(filename).name
        target = _da_output_dir() / safe
        if not target.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        return FileResponse(str(target), content_disposition_type="inline")

    @router.post("/api/da/chat")
    async def da_chat(payload: Dict[str, Any] = Body(...)):
        """A lightweight chatbot turn grounded in the connected data.

        Not the full AIAgent loop — a focused data-analyst assistant that knows
        the connected schema and can suggest / explain. Tool actions (nl2sql,
        visualize) are surfaced as structured suggestions the UI can run.
        """
        from agent.data_agent.llm import get_llm

        messages: List[Dict[str, str]] = payload.get("messages", [])
        eng = _engine()
        schema_lines = []
        for t in eng.list_tables():
            try:
                cols = ", ".join(c["name"] for c in eng.table_schema(t))
                schema_lines.append(f"- {t}({cols})")
            except Exception:
                pass
        schema = "\n".join(schema_lines) or "(no data connected yet)"
        system = (
            "You are DAClaw's data analyst assistant. You help with data "
            "engineering, data analysis, and Excel tasks. Connected tables:\n"
            f"{schema}\n\n"
            "Be concise and concrete. When the user asks for analysis, propose the "
            "exact SQL. When they ask for a pipeline, describe the stages. Never "
            "invent numbers — say they should run the query."
        )
        try:
            llm = get_llm()
            reply = llm.chat(
                [{"role": "system", "content": system}, *messages],
                max_output_tokens=1200,
            )
            return {"ok": True, "reply": reply, "schema_tables": eng.list_tables()}
        except Exception as exc:
            logger.exception("chat failed")
            return {"ok": False, "error": str(exc)}

    # ---------------- Part 2: leaderboard ---------------------------------

    @router.get("/api/lb/overview")
    async def lb_overview():
        from agent.data_agent import leaderboard as lb

        return {
            "matrix": lb.harness_by_benchmark_matrix(),
            "best_combos": lb.best_combos(10),
            "model_ranking": lb.model_ranking(),
        }

    @router.get("/api/lb/raw")
    async def lb_raw():
        from agent.data_agent import leaderboard as lb

        return lb.load_leaderboard()

    @router.post("/api/lb/evaluate")
    async def lb_evaluate(payload: Dict[str, Any] = Body(...)):
        """Framework endpoint for 'one-click evaluate'. For the demo this returns
        a seed-calibrated score row; real runs replace this later."""
        from agent.data_agent import leaderboard as lb

        model = payload.get("model")
        harness = payload.get("harness")
        benchmark = payload.get("benchmark")
        split = payload.get("split", "")
        bm = next((b for b in lb.BENCHMARKS if b["id"] == benchmark), None)
        if not (model and harness and bm):
            return {"ok": False, "error": "need model, harness, benchmark"}
        score = lb._seed_score(model, harness, bm, split or bm["splits"][0])
        entry = {"model": model, "harness": harness, "benchmark": benchmark,
                 "split": split or bm["splits"][0], "score": score,
                 "metric": bm["metric"], "date": "2026-07-10", "source": "evaluated"}
        lb.add_entry(entry)
        return {"ok": True, "entry": entry, "note": "demo: seed-calibrated score (framework)"}


def register(app) -> None:
    """Attach the DataAgent demo routes to a FastAPI app."""
    if router is not None:
        app.include_router(router)
