#!/usr/bin/env python3
"""DataAgent tools: connect sources, query, visualize, dashboard, document.

These are thin wrappers over the ``agent/data_agent/`` service layer, following
the exact registration pattern of ``tools/data_analysis_tool.py``. All real
logic lives in the service layer so the CLI, web server, and gateway share one
implementation.

Key rule (evidence chain): every number a chart or report shows comes from a
real ``data_query`` result — the model never hand-writes data values here.

Generated HTML (charts / dashboards / reports) is written into the active web
workspace root so it shows up in the DAClaw workspace UI immediately, and also
saved into the current ``analysis_run`` for reproducibility.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _engine():
    from agent.data_agent.engine import get_engine

    return get_engine()


def _active_run():
    """Return (creating if needed) the current analysis run."""
    from agent.data_agent import run_context

    return run_context.current_run()


def _output_dir() -> Path:
    """Where to drop generated HTML so the web workspace can preview it.

    Prefers the web workspace root (so files appear in the UI); falls back to
    the current working directory.
    """
    try:
        from daclaw_cli.web_server import _workspace_upload_root  # type: ignore

        root = _workspace_upload_root()
        out = Path(root) / "data_agent"
        out.mkdir(parents=True, exist_ok=True)
        return out
    except Exception:
        out = Path.cwd() / "data_agent_output"
        out.mkdir(parents=True, exist_ok=True)
        return out


# Channels that can show the HTML chart itself (web UI preview, a local
# browser). Anything else — Feishu, WeChat, Telegram, cron delivery — only
# receives files, so charts are also exported as PNG for them.
_HTML_CAPABLE_PLATFORMS = {"", "cli", "local", "web"}
# Chat channels whose adapters cannot upload files: a PNG would only reach the
# user as a server path, so don't render one unless export_png asks for it.
_NO_FILE_PLATFORMS = {"dingtalk", "sms", "homeassistant"}


def _maybe_export_png(args: Dict[str, Any], html_path: Path) -> Dict[str, Any]:
    """Export a PNG next to ``html_path`` when the current channel needs one.

    ``export_png`` in ``args`` forces it on or off; otherwise it is decided by
    the session platform the gateway sets. Failure never breaks the tool —
    the HTML stays the primary artifact and the reason is reported.
    """
    want = args.get("export_png")
    if isinstance(want, str):
        want = want.strip().lower() in {"1", "true", "yes"}
    if want is None:
        try:
            from gateway.session_context import get_session_env

            platform = get_session_env("DACLAW_SESSION_PLATFORM", "")
        except Exception:
            platform = ""
        platform = (platform or "").strip().lower()
        want = platform not in _HTML_CAPABLE_PLATFORMS and platform not in _NO_FILE_PLATFORMS
    if not want:
        return {}

    from agent.data_agent import viz

    res = viz.export_chart_png(str(html_path), str(html_path.with_suffix(".png")))
    if not res.get("ok"):
        return {"png_error": res.get("reason", "png export failed")}
    out: Dict[str, Any] = {
        "png_path": res["path"],
        "deliver_with": f"MEDIA:{res['path']}",
    }
    if res.get("warning"):
        out["png_warning"] = res["warning"]
    return out


_PNG_DELIVERY_NOTE = (
    "This chat cannot open HTML files. To show the chart, put the "
    "'deliver_with' line on its own line in your reply; do not link the "
    "local .html path."
)


def _check_engine() -> bool:
    try:
        from agent.data_agent.engine import engine_available

        return engine_available()
    except Exception:
        return False


def _allowed_source_path(value: str) -> str:
    """Keep agent-invoked source reads inside known local data roots."""
    from daclaw_constants import get_daclaw_home

    target = Path(value).expanduser().resolve()
    roots = [Path.cwd().resolve(), (get_daclaw_home() / "demo_data").resolve()]
    try:
        from daclaw_cli.web_server import _workspace_upload_root

        roots.append(Path(_workspace_upload_root()).resolve())
    except Exception:
        pass
    if not any(target.is_relative_to(root) for root in roots):
        raise ValueError(
            "Data sources must be inside the current project, the DAClaw upload "
            "workspace, or $DACLAW_HOME/demo_data. Upload/copy the file first."
        )
    return str(target)


# ---------------------------------------------------------------------------
# data_workflow_plan
# ---------------------------------------------------------------------------

DATA_WORKFLOW_PLAN_SCHEMA: Dict[str, Any] = {
    "name": "data_workflow_plan",
    "description": (
        "Compile a transparent typed workflow before a complex analysis. Returns "
        "a validated DAG, independent execution layers, parallelism metadata, and "
        "the tools expected at each node. Use when the user asks for a multi-step "
        "analysis, dashboard, report, story, spreadsheet, or data pipeline."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {"type": "string"},
            "tables": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["goal"],
    },
}


def _handle_data_workflow_plan(args, **_kwargs):
    from agent.data_agent import knowledge, workflow

    try:
        tables = args.get("tables") or _engine().list_tables()
        plan = workflow.draft_workflow(
            args["goal"],
            tables=tables,
            knowledge_sources=[item["source_id"] for item in knowledge.list_sources()],
        )
        return tool_result(plan)
    except Exception as exc:  # pragma: no cover
        logger.exception("data_workflow_plan failed")
        return tool_error(f"data_workflow_plan failed: {exc}")


# ---------------------------------------------------------------------------
# data_knowledge_connect / data_knowledge_search
# ---------------------------------------------------------------------------

DATA_KNOWLEDGE_CONNECT_SCHEMA: Dict[str, Any] = {
    "name": "data_knowledge_connect",
    "description": (
        "Connect a local PDF, DOCX, Markdown, text, JSON, or HTML document to the "
        "Data Agent knowledge base. The document is extracted and chunked locally "
        "for joint analysis with structured tables."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Path to the uploaded document."},
            "name": {"type": "string"},
        },
        "required": ["source"],
    },
}


def _handle_data_knowledge_connect(args, **_kwargs):
    from agent.data_agent import knowledge

    try:
        return tool_result(knowledge.connect_document(_allowed_source_path(args["source"]), name=args.get("name")))
    except Exception as exc:
        return tool_error(str(exc))


DATA_KNOWLEDGE_SEARCH_SCHEMA: Dict[str, Any] = {
    "name": "data_knowledge_search",
    "description": (
        "Search connected local documents and return the most relevant grounded "
        "passages with source ids. Use this to combine policies, market reports, "
        "research notes, or metric definitions with structured-data analysis."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "source_ids": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer", "default": 8},
        },
        "required": ["query"],
    },
}


def _handle_data_knowledge_search(args, **_kwargs):
    from agent.data_agent import knowledge

    try:
        return tool_result(
            knowledge.search(
                args["query"],
                source_ids=args.get("source_ids"),
                limit=int(args.get("limit", 8)),
            )
        )
    except Exception as exc:
        return tool_error(str(exc))


# ---------------------------------------------------------------------------
# data_connect
# ---------------------------------------------------------------------------

DATA_CONNECT_SCHEMA: Dict[str, Any] = {
    "name": "data_connect",
    "description": (
        "Connect a local data source (CSV, TSV, Parquet, Excel .xlsx, SQLite, "
        "or DuckDB file) so it can be queried and visualized. Returns the list "
        "of tables it exposes. Call this first, before data_query / data_visualize."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Path to the data file."},
            "name": {"type": "string", "description": "Optional short name for the source."},
        },
        "required": ["source"],
    },
}


def _handle_data_connect(args, **_kwargs):
    from agent.data_agent.engine import DataEngineError

    try:
        eng = _engine()
        desc = eng.connect_source(_allowed_source_path(args["source"]), name=args.get("name"))
        # attach a quick schema for each table so the model can plan queries
        tables = []
        for t in desc["tables"]:
            tables.append({"table": t, "schema": eng.table_schema(t)})
        return tool_result(
            {
                "connected": desc["slug"],
                "kind": desc["kind"],
                "path": desc["path"],
                "tables": tables,
                "all_tables_in_session": eng.list_tables(),
            }
        )
    except DataEngineError as exc:
        return tool_error(str(exc))
    except Exception as exc:  # pragma: no cover
        logger.exception("data_connect failed")
        return tool_error(f"data_connect failed: {exc}")


# ---------------------------------------------------------------------------
# data_query
# ---------------------------------------------------------------------------

DATA_QUERY_SCHEMA: Dict[str, Any] = {
    "name": "data_query",
    "description": (
        "Execute a read-only SQL query (DuckDB dialect) against connected data "
        "sources and return a compact result summary with an artifact_id. EVERY "
        "number in your final answer or report MUST come from a data_query (or "
        "data_execute_code) result — never estimate or hand-compute. The full "
        "result is saved to disk; you get sample rows + row count + stats back."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "Read-only SELECT/WITH query (DuckDB SQL)."},
            "max_rows": {
                "type": "integer",
                "description": "Max rows to fetch (default 100000).",
                "default": 100000,
            },
        },
        "required": ["sql"],
    },
}


def _handle_data_query(args, **_kwargs):
    from agent.data_agent.engine import DataEngineError

    try:
        eng = _engine()
        rt = eng.run_query(args["sql"], max_rows=int(args.get("max_rows", 100000)))
        run = _active_run()
        run.save_query(args["sql"], rt)
        return tool_result(rt.envelope())
    except DataEngineError as exc:
        return tool_error(str(exc))
    except Exception as exc:  # pragma: no cover
        logger.exception("data_query failed")
        return tool_error(f"data_query failed: {exc}")


# ---------------------------------------------------------------------------
# data_parallel_query — run INDEPENDENT queries concurrently (real parallelism)
# ---------------------------------------------------------------------------

DATA_PARALLEL_QUERY_SCHEMA: Dict[str, Any] = {
    "name": "data_parallel_query",
    "description": (
        "Run 2–8 INDEPENDENT read-only SQL queries CONCURRENTLY (each on its own "
        "DuckDB cursor) and get all results back at once. Use this when a step has "
        "several queries that don't depend on each other (e.g. revenue by region "
        "AND by month AND top products) — it executes the independent branches of "
        "your plan in parallel instead of one-by-one. Do NOT use for dependent "
        "queries; use data_query for those."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Short name for this branch."},
                        "sql": {"type": "string", "description": "Read-only SELECT/WITH query (DuckDB SQL)."},
                    },
                    "required": ["sql"],
                },
                "minItems": 2,
                "maxItems": 8,
                "description": "The independent queries to run in parallel.",
            },
            "max_rows": {"type": "integer", "description": "Max rows per query (default 100000).", "default": 100000},
        },
        "required": ["queries"],
    },
}


def _handle_data_parallel_query(args, **_kwargs):
    from agent.data_agent.engine import DataEngineError

    try:
        items = args.get("queries") or []
        sqls = [str(it.get("sql", "")) for it in items]
        labels = [str(it.get("label") or f"branch_{i+1}") for i, it in enumerate(items)]
        eng = _engine()
        # concurrent execution (per-thread cursors)
        results = eng.run_queries_parallel(sqls, max_rows=int(args.get("max_rows", 100000)))
        # persist serially AFTER the parallel run (run manifest is not thread-safe)
        run = _active_run()
        branches = []
        total_ms = 0.0
        for label, sql, rt in zip(labels, sqls, results):
            run.save_query(sql, rt)
            env = rt.envelope()
            total_ms += float(rt.stats.get("elapsed_ms", 0) or 0)
            branches.append({"label": label, **env})
        return tool_result({
            "kind": "parallel_result",
            "n_branches": len(branches),
            "ran_in_parallel": True,
            "sum_query_ms": round(total_ms, 1),
            "branches": branches,
        })
    except DataEngineError as exc:
        return tool_error(str(exc))
    except Exception as exc:  # pragma: no cover
        logger.exception("data_parallel_query failed")
        return tool_error(f"data_parallel_query failed: {exc}")


# ---------------------------------------------------------------------------
# data_visualize
# ---------------------------------------------------------------------------

DATA_VISUALIZE_SCHEMA: Dict[str, Any] = {
    "name": "data_visualize",
    "description": (
        "Run a SQL query and render its result as a beautiful, self-contained "
        "interactive chart (HTML with inlined ECharts). Chart type is auto-"
        "selected from the data shape unless you specify one. The HTML file is "
        "saved to the workspace so it previews in the UI. Use this to answer a "
        "question WITH a visualization in one step."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "SQL producing the chart data (read-only)."},
            "title": {"type": "string", "description": "Chart title."},
            "chart_type": {
                "type": "string",
                "description": "Optional: line, bar, bar_horizontal, pie, scatter, heatmap, area, table. Omit to auto-select.",
            },
            "subtitle": {"type": "string", "description": "Optional subtitle."},
            "export_png": {
                "type": "boolean",
                "description": "Also save a PNG of the chart. Omit to decide automatically: on by default in chat apps that cannot open HTML.",
            },
        },
        "required": ["sql", "title"],
    },
}


def _handle_data_visualize(args, **_kwargs):
    from agent.data_agent import viz
    from agent.data_agent.engine import DataEngineError

    try:
        eng = _engine()
        rt = eng.run_query(args["sql"])
        run = _active_run()
        run.save_query(args["sql"], rt)
        html = viz.render_chart_html(
            rt,
            chart_type=args.get("chart_type"),
            title=args.get("title", "Chart"),
            subtitle=args.get("subtitle", ""),
            provenance=args["sql"],
        )
        check = viz.self_check_html(html)
        chart_rec = run.save_chart(args.get("title", "chart"), html)
        # also drop into the workspace so it appears in the UI
        out = _output_dir() / Path(chart_rec["rel"]).name
        out.write_text(html, encoding="utf-8")
        payload = {
            "artifact_id": chart_rec["artifact_id"],
            "chart_type": args.get("chart_type") or viz.select_chart_type(rt),
            "row_count": rt.row_count,
            "html_path": str(out),
            "workspace_file": out.name,
            "render_check": check,
            "note": "Chart HTML saved. It previews in the workspace UI and opens standalone.",
        }
        png = _maybe_export_png(args, out)
        payload.update(png)
        if png.get("png_path"):
            payload["note"] = _PNG_DELIVERY_NOTE
        return tool_result(payload)
    except DataEngineError as exc:
        return tool_error(str(exc))
    except Exception as exc:  # pragma: no cover
        logger.exception("data_visualize failed")
        return tool_error(f"data_visualize failed: {exc}")


# ---------------------------------------------------------------------------
# data_dashboard  (auto-overview or explicit panels)
# ---------------------------------------------------------------------------

DATA_DASHBOARD_SCHEMA: Dict[str, Any] = {
    "name": "data_dashboard",
    "description": (
        "Build a multi-chart dashboard as one self-contained HTML file. Two "
        "modes: (1) pass 'table' to auto-generate an overview dashboard "
        "(KPIs + trend + category charts) from that table's schema — great for "
        "the moment right after connecting/uploading data; or (2) pass explicit "
        "'panels', each a {title, sql, chart_type?} spec."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Dashboard title."},
            "table": {
                "type": "string",
                "description": "Auto-overview mode: table name to summarize.",
            },
            "panels": {
                "type": "array",
                "description": "Explicit mode: list of {title, sql, chart_type?}.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "sql": {"type": "string"},
                        "chart_type": {"type": "string"},
                    },
                    "required": ["title", "sql"],
                },
            },
            "export_png": {
                "type": "boolean",
                "description": "Also save a PNG of the dashboard. Omit to decide automatically: on by default in chat apps that cannot open HTML.",
            },
        },
        "required": ["title"],
    },
}


def _handle_data_dashboard(args, **_kwargs):
    from agent.data_agent import overview, viz
    from agent.data_agent.engine import DataEngineError

    try:
        eng = _engine()
        run = _active_run()
        title = args.get("title", "Dashboard")

        if args.get("table"):
            ov = overview.build_overview(eng, args["table"])
            panels = ov["panels"]
            for q in ov.get("queries", []):
                pass  # queries already executed inside build_overview
            subtitle = f"auto-overview of {args['table']} · {ov['profile']['row_count']} rows"
        elif args.get("panels"):
            panels = []
            for p in args["panels"]:
                rt = eng.run_query(p["sql"])
                run.save_query(p["sql"], rt)
                panels.append(
                    {
                        "title": p["title"],
                        "result": rt,
                        "chart_type": p.get("chart_type"),
                        "provenance": p["sql"],
                    }
                )
            subtitle = ""
        else:
            return tool_error("Provide either 'table' (auto mode) or 'panels'.")

        html = viz.render_dashboard_html(panels, title=title, subtitle=subtitle)
        check = viz.self_check_html(html)
        rec = run.save_chart(title, html)
        out = _output_dir() / f"dashboard_{Path(rec['rel']).name}"
        out.write_text(html, encoding="utf-8")
        chart_panels = [p for p in panels if not p.get("kpi")]
        payload = {
            "artifact_id": rec["artifact_id"],
            "panels": len(panels),
            "charts": len(chart_panels),
            "html_path": str(out),
            "workspace_file": out.name,
            "render_check": check,
        }
        png = _maybe_export_png(args, out)
        payload.update(png)
        if png.get("png_path"):
            payload["note"] = _PNG_DELIVERY_NOTE
        return tool_result(payload)
    except DataEngineError as exc:
        return tool_error(str(exc))
    except Exception as exc:  # pragma: no cover
        logger.exception("data_dashboard failed")
        return tool_error(f"data_dashboard failed: {exc}")


# ---------------------------------------------------------------------------
# data_document  (assemble a report)
# ---------------------------------------------------------------------------

DATA_DOCUMENT_SCHEMA: Dict[str, Any] = {
    "name": "data_document",
    "description": (
        "Assemble an analysis report as a self-contained HTML document (charts "
        "inlined, opens anywhere). Provide ordered 'sections'. Chart/table "
        "sections take a 'sql' that is executed for real data. Optionally export "
        "to PDF or DOCX (format='pdf'|'docx'); these degrade gracefully if the "
        "converter isn't installed — HTML is always produced."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Report title."},
            "subtitle": {"type": "string"},
            "sections": {
                "type": "array",
                "description": (
                    "Ordered sections. Each is one of: "
                    "{type:'narrative', text:'markdown-ish'} | "
                    "{type:'kpis', items:[{label,value}]} | "
                    "{type:'chart', title, sql, chart_type?, provenance?} | "
                    "{type:'table', title, sql} | "
                    "{type:'caveats', items:[str]}"
                ),
                "items": {"type": "object"},
            },
            "format": {
                "type": "string",
                "description": "Optional extra export: 'pdf' or 'docx'. HTML is always generated.",
            },
        },
        "required": ["title", "sections"],
    },
}


def _handle_data_document(args, **_kwargs):
    from agent.data_agent import document
    from agent.data_agent.engine import DataEngineError

    try:
        eng = _engine()
        run = _active_run()
        resolved: List[Dict[str, Any]] = []
        # resolve any sql-backed sections into ResultTables
        for sec in args.get("sections", []):
            stype = sec.get("type")
            if stype in ("chart", "table") and sec.get("sql"):
                rt = eng.run_query(sec["sql"])
                run.save_query(sec["sql"], rt)
                resolved.append(
                    {
                        **sec,
                        "result": rt,
                        "provenance": sec.get("provenance", sec["sql"]),
                    }
                )
            else:
                resolved.append(sec)

        import time as _t

        html = document.build_report_html(
            args.get("title", "Report"),
            resolved,
            subtitle=args.get("subtitle", ""),
            generated_at=_t.strftime("%Y-%m-%d %H:%M"),
        )
        rec = run.save_report(args.get("title", "report"), html)
        out = _output_dir() / Path(rec["rel"]).name
        out.write_text(html, encoding="utf-8")

        result: Dict[str, Any] = {
            "artifact_id": rec["artifact_id"],
            "html_path": str(out),
            "workspace_file": out.name,
            "sections": len(resolved),
        }

        fmt = (args.get("format") or "").lower()
        if fmt == "pdf":
            pdf_out = out.with_suffix(".pdf")
            result["pdf"] = document.export_pdf(html, str(pdf_out))
        elif fmt == "docx":
            docx_out = out.with_suffix(".docx")
            result["docx"] = document.export_docx(resolved, args.get("title", "Report"), str(docx_out))

        return tool_result(result)
    except DataEngineError as exc:
        return tool_error(str(exc))
    except Exception as exc:  # pragma: no cover
        logger.exception("data_document failed")
        return tool_error(f"data_document failed: {exc}")


# ---------------------------------------------------------------------------
# data_excel  (export query results to a native .xlsx with real Excel charts)
# ---------------------------------------------------------------------------

DATA_EXCEL_SCHEMA: Dict[str, Any] = {
    "name": "data_excel",
    "description": (
        "Export query results to a native Excel .xlsx file (openpyxl) with "
        "styled tables and REAL, editable Excel charts (not images). Two modes: "
        "(1) single sheet — pass 'sql' (+ optional 'chart_type' to embed a chart); "
        "(2) multi-sheet dashboard — pass 'panels', each {title, sql, chart_type?}, "
        "producing an Overview sheet + one sheet per panel. Use this whenever the "
        "user wants an Excel/spreadsheet deliverable they can open in Excel. "
        "chart_type is one of: bar, line, pie, bar_horizontal, scatter."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Workbook / sheet title."},
            "sql": {
                "type": "string",
                "description": "Single-sheet mode: read-only SQL whose result fills the sheet.",
            },
            "chart_type": {
                "type": "string",
                "description": "Single-sheet mode: embed this chart type (bar/line/pie/bar_horizontal/scatter).",
            },
            "panels": {
                "type": "array",
                "description": "Dashboard mode: list of {title, sql, chart_type?} — one sheet each.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "sql": {"type": "string"},
                        "chart_type": {"type": "string"},
                    },
                    "required": ["title", "sql"],
                },
            },
        },
        "required": ["title"],
    },
}


def _handle_data_excel(args, **_kwargs):
    from agent.data_agent import excel_ops
    from agent.data_agent.artifacts import _slugify
    from agent.data_agent.engine import DataEngineError

    try:
        eng = _engine()
        title = args.get("title", "export")
        out = _output_dir() / f"excel_{_slugify(title)}.xlsx"

        if args.get("panels"):
            panels = []
            for p in args["panels"]:
                rt = eng.run_query(p["sql"])
                panels.append(
                    {"title": p.get("title", ""), "result": rt, "chart_type": p.get("chart_type", "bar")}
                )
            rec = excel_ops.build_dashboard(panels, str(out), title=title)
            sheets = len(panels)
        elif args.get("sql"):
            rt = eng.run_query(args["sql"])
            if args.get("chart_type"):
                rec = excel_ops.add_chart(
                    rt, str(out), chart_type=args["chart_type"], title=title
                )
            else:
                rec = excel_ops.write_table(rt, str(out), sheet_name=title[:31])
            sheets = 1
        else:
            return tool_error("data_excel needs either 'sql' (single sheet) or 'panels' (dashboard).")

        return tool_result(
            {
                "excel_path": str(out),
                "workspace_file": out.name,
                "sheets": sheets,
                "rows": rec.get("rows"),
                "chart_type": rec.get("chart_type"),
                "note": "Native .xlsx saved with editable Excel charts. Offer it as a download.",
            }
        )
    except DataEngineError as exc:
        return tool_error(str(exc))
    except Exception as exc:  # pragma: no cover
        logger.exception("data_excel failed")
        return tool_error(f"data_excel failed: {exc}")


# ---------------------------------------------------------------------------
# data_excel_edit  (edit an ALREADY-UPLOADED .xlsx in place)
# ---------------------------------------------------------------------------

DATA_EXCEL_EDIT_SCHEMA: Dict[str, Any] = {
    "name": "data_excel_edit",
    "description": (
        "Edit an Excel workbook the user already uploaded, preserving its "
        "original sheets. Operations: (1) 'add_sheet' — run 'sql' and append the "
        "result as a NEW styled sheet (optional 'chart_type' embeds a native "
        "chart) — e.g. add a summary/analysis sheet next to the raw data; "
        "(2) 'repair' — restyle headers and autosize columns across all sheets. "
        "If multiple Excel files are connected, pass 'table' (a table from the "
        "target workbook) or 'source_path' to disambiguate; otherwise the single "
        "connected workbook is used. Returns a new .xlsx to download."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["add_sheet", "repair"], "description": "Edit operation."},
            "sql": {"type": "string", "description": "add_sheet: SQL whose result becomes the new sheet."},
            "sheet_name": {"type": "string", "description": "add_sheet: name for the new sheet."},
            "chart_type": {"type": "string", "description": "add_sheet: optional chart (bar/line/pie/bar_horizontal/scatter)."},
            "table": {"type": "string", "description": "A table from the target workbook (to locate the file)."},
            "source_path": {"type": "string", "description": "Explicit path to the workbook to edit."},
        },
        "required": ["op"],
    },
}


def _resolve_excel_source(eng, args) -> str:
    """Find the uploaded .xlsx to edit, from source_path / table / the sole one."""
    if args.get("source_path"):
        return args["source_path"]
    if args.get("table"):
        p = eng.source_path_for_table(args["table"])
        if p:
            return p
    xl = eng.excel_sources()
    if len(xl) == 1:
        return xl[0]["path"]
    if not xl:
        raise ValueError("No Excel workbook is connected. Upload a .xlsx first.")
    raise ValueError(
        "Multiple Excel files connected — pass 'table' or 'source_path' to pick one: "
        + ", ".join(s["path"] for s in xl)
    )


def _handle_data_excel_edit(args, **_kwargs):
    from agent.data_agent import excel_ops
    from agent.data_agent.artifacts import _slugify
    from agent.data_agent.engine import DataEngineError

    try:
        eng = _engine()
        op = args.get("op")
        src = _resolve_excel_source(eng, args)
        stem = Path(src).stem

        if op == "add_sheet":
            if not args.get("sql"):
                return tool_error("data_excel_edit add_sheet needs 'sql'.")
            rt = eng.run_query(args["sql"])
            sheet_name = args.get("sheet_name", "Analysis")
            out = _output_dir() / f"edited_{_slugify(stem)}_{_slugify(sheet_name)}.xlsx"
            rec = excel_ops.add_sheet_to_workbook(
                src, str(out), rt,
                sheet_name=sheet_name,
                chart_type=args.get("chart_type"),
                chart_title=sheet_name,
            )
            note = f"Added sheet '{rec['added_sheet']}' to the workbook."
        elif op == "repair":
            out = _output_dir() / f"repaired_{_slugify(stem)}.xlsx"
            rec = excel_ops.repair_workbook(src, str(out))
            note = f"Applied {rec.get('n_repairs', 0)} repair(s)."
        else:
            return tool_error(f"unknown op: {op}")

        return tool_result(
            {
                "excel_path": str(out),
                "workspace_file": out.name,
                "source_file": Path(src).name,
                "op": op,
                "detail": rec,
                "note": note + " Offer it as a download.",
            }
        )
    except DataEngineError as exc:
        return tool_error(str(exc))
    except ValueError as exc:
        return tool_error(str(exc))
    except Exception as exc:  # pragma: no cover
        logger.exception("data_excel_edit failed")
        return tool_error(f"data_excel_edit failed: {exc}")


# ---------------------------------------------------------------------------
# data_excel_formula  (build a COMPUTABLE workbook / financial model)
# ---------------------------------------------------------------------------

DATA_EXCEL_FORMULA_SCHEMA: Dict[str, Any] = {
    "name": "data_excel_formula",
    "description": (
        "Build a COMPUTABLE Excel workbook from explicit cells and formulas "
        "(financial models: DCF, LBO, M&A accretion/dilution, sensitivity "
        "tables, etc.). Unlike data_excel (which dumps a query result), this "
        "writes real Excel formulas (=B2/(1+$B$3)^B1, =SUM(...), =NPV(...), "
        "=IRR(...), =VLOOKUP(...)) and then RECALCULATES the workbook offline to "
        "verify every number computes with NO errors. Use this whenever the user "
        "wants a formula-driven, updatable model. Provide 'sheets', each with a "
        "list of cells. Each cell: {cell:'B4', formula:'=...' OR value:123, "
        "bold?, fmt?:'#,##0.00', align?}. Cross-sheet refs use 'Sheet2'!A1.\n"
        "HARD RULES — the model must be fully live/updatable:\n"
        "1. Only INPUT ASSUMPTIONS (revenue, growth rate, WACC, tax rate, etc.) "
        "may be literal values. EVERY derived/calculated cell MUST be a formula. "
        "Never write a computed result as a number.\n"
        "2. Sensitivity / scenario tables MUST be formulas too — each body cell "
        "references the assumption cells (e.g. rebuild the model expression with "
        "the row/column input, or reference a driver cell). Do NOT pre-compute "
        "the grid and paste numbers.\n"
        "3. Time-series / multi-period cash flows MUST reference assumption cells "
        "and the prior period (e.g. =B10*(1+$B$4)), never hardcoded per year. "
        "An IRR/NPV's period-0 outflow must reference the equity/investment cell "
        "(e.g. =-B8), not a typed number.\n"
        "4. Do NOT embed magic numbers inside formulas — put the constant in a "
        "labeled assumption cell and reference it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Model title."},
            "sheets": {
                "type": "array",
                "description": "Worksheets to create, in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "cells": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "cell": {"type": "string", "description": "A1-style coordinate."},
                                    "formula": {"type": "string", "description": "Excel formula starting with =."},
                                    "value": {"description": "Literal value (number/string) if not a formula."},
                                    "bold": {"type": "boolean"},
                                    "fmt": {"type": "string", "description": "Excel number format, e.g. '#,##0.00' or '0.0%'."},
                                    "align": {"type": "string"},
                                },
                                "required": ["cell"],
                            },
                        },
                        "widths": {"type": "object", "description": "Column widths, e.g. {\"A\": 22}."},
                    },
                    "required": ["name", "cells"],
                },
            },
        },
        "required": ["title", "sheets"],
    },
}


def _handle_data_excel_formula(args, **_kwargs):
    from agent.data_agent import excel_formula
    from agent.data_agent.artifacts import _slugify

    try:
        title = args.get("title", "model")
        out = _output_dir() / f"model_{_slugify(title)}.xlsx"
        rec = excel_formula.write_model(str(out), args["sheets"], title=title)
        # a compact preview of a few computed values so the agent can cite them
        computed_preview = {}
        try:
            rc = excel_formula.recalc(str(out))
            computed_preview = {
                k: v for k, v in list(rc["cells"].items())
                if not str(v).startswith("#")
            }
            # keep it small
            computed_preview = dict(list(computed_preview.items())[:40])
        except Exception:
            pass
        return tool_result(
            {
                "excel_path": str(out),
                "workspace_file": out.name,
                "title": title,
                "sheets": rec.get("sheets"),
                "computed_ok": rec.get("computed_ok"),
                "errors": rec.get("errors"),
                "hardcoding_warnings": rec.get("hardcoding_warnings"),
                "computed_preview": computed_preview,
                "note": (
                    "Computable workbook built and recalculated offline. "
                    + ("All formulas evaluate with no errors." if rec.get("computed_ok")
                       else "WARNING: residual formula errors remain — see 'errors'.")
                    + (
                        " IMPORTANT: some calculated cells look hardcoded — see "
                        "'hardcoding_warnings'. Rebuild those as formulas that "
                        "reference assumption cells, then call this tool again."
                        if rec.get("hardcoding_warnings") else ""
                    )
                ),
            }
        )
    except Exception as exc:  # pragma: no cover
        logger.exception("data_excel_formula failed")
        return tool_error(f"data_excel_formula failed: {exc}")


# ---------------------------------------------------------------------------
# data_excel_fix  (locate & repair formula errors in an uploaded workbook)
# ---------------------------------------------------------------------------

DATA_EXCEL_FIX_SCHEMA: Dict[str, Any] = {
    "name": "data_excel_fix",
    "description": (
        "Diagnose and repair formula problems in an uploaded Excel workbook "
        "(NOT build from scratch). Two phases: (1) if 'fixes' is omitted, AUDIT "
        "the workbook — recalc offline and return every error cell (#REF!, "
        "#DIV/0!, #NUM!, #VALUE!, #NAME?, #N/A, circular refs surface as errors), "
        "plus hardcoded magic numbers inside formulas and duplicated-formula "
        "groups; call again with 'fixes'. (2) if 'fixes' is provided, APPLY them "
        "and recalc to VERIFY the errors are gone. Each fix: {cell:'Sheet!B3', "
        "formula:'=B4/B1'} (or value:123). Locate the workbook via 'table' or "
        "'source_path' (defaults to the single connected .xlsx)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "table": {"type": "string", "description": "A table from the target workbook (to find the file)."},
            "source_path": {"type": "string", "description": "Explicit path to the workbook."},
            "fixes": {
                "type": "array",
                "description": "Apply mode: cell fixes to write, then verify by recalc.",
                "items": {
                    "type": "object",
                    "properties": {
                        "cell": {"type": "string", "description": "Sheet!Cell or Cell."},
                        "formula": {"type": "string"},
                        "value": {},
                    },
                    "required": ["cell"],
                },
            },
        },
    },
}


def _handle_data_excel_fix(args, **_kwargs):
    from agent.data_agent import excel_formula
    from agent.data_agent.artifacts import _slugify

    try:
        eng = _engine()
        src = _resolve_excel_source(eng, args)
        stem = Path(src).stem

        if not args.get("fixes"):
            # audit phase
            report = excel_formula.audit(src)
            return tool_result(
                {
                    "phase": "audit",
                    "source_file": Path(src).name,
                    "errors": report.get("errors"),
                    "hardcoded": report.get("hardcoded"),
                    "duplicate_groups": report.get("duplicate_groups"),
                    "formula_count": report.get("formula_count"),
                    "note": (
                        f"Found {len(report.get('errors', []))} error cell(s), "
                        f"{len(report.get('hardcoded', []))} hardcoded-number formula(s). "
                        "Call data_excel_fix again with 'fixes' to repair."
                    ),
                }
            )

        # apply phase
        out = _output_dir() / f"fixed_{_slugify(stem)}.xlsx"
        rec = excel_formula.apply_fixes(src, str(out), args["fixes"])
        return tool_result(
            {
                "phase": "apply",
                "excel_path": str(out),
                "workspace_file": out.name,
                "source_file": Path(src).name,
                "applied": rec.get("applied"),
                "errors_before": rec.get("errors_before"),
                "errors_after": rec.get("errors_after"),
                "fixed_count": rec.get("fixed_count"),
                "note": (
                    f"Applied {len(rec.get('applied', []))} fix(es). "
                    f"Errors: {len(rec.get('errors_before', []))} → {len(rec.get('errors_after', []))}. "
                    + ("All clear ✓" if not rec.get("errors_after") else "Some errors remain.")
                    + " Offer the repaired file as a download."
                ),
            }
        )
    except ValueError as exc:
        return tool_error(str(exc))
    except Exception as exc:  # pragma: no cover
        logger.exception("data_excel_fix failed")
        return tool_error(f"data_excel_fix failed: {exc}")


# ---------------------------------------------------------------------------
# data_story  (animated executive narrative)
# ---------------------------------------------------------------------------

DATA_STORY_SCHEMA: Dict[str, Any] = {
    "name": "data_story",
    "description": (
        "Create a cinematic, self-contained interactive Data Story / data video. "
        "Each slide can be narrative, KPI cards, a real SQL-backed chart, or a real "
        "SQL-backed table. Provide a short spoken 'narration' per slide (1-2 "
        "sentences) and set narrate=true to synthesize voice-over so it plays as a "
        "narrated data VIDEO that auto-advances when each narration ends. The result "
        "has autoplay + keyboard navigation, ideal for executive presentations."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "subtitle": {"type": "string"},
            "narrate": {
                "type": "boolean",
                "description": "Synthesize voice-over narration (TTS) so it plays as a narrated data video. Default false.",
                "default": False,
            },
            "intro_narration": {"type": "string", "description": "Spoken narration for the opening title scene."},
            "slides": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["narrative", "kpi", "chart", "table"]},
                        "title": {"type": "string"},
                        "kicker": {"type": "string"},
                        "summary": {"type": "string"},
                        "narration": {"type": "string", "description": "Spoken voice-over for this slide (1-2 sentences); used when narrate=true."},
                        "text": {"type": "string"},
                        "items": {"type": "array", "items": {"type": "object"}},
                        "sql": {"type": "string"},
                        "chart_type": {"type": "string"},
                    },
                    "required": ["type", "title"],
                },
            },
        },
        "required": ["title", "slides"],
    },
}


def _synthesize_narration(text: str, out_path: Path) -> bool:
    """Best-effort TTS: text → audio file at out_path. Returns True on success.

    Reuses the shared TTS tool (config `tts.provider`; `neutts` is offline).
    Degrades gracefully — if TTS is unavailable the story just plays silently.
    """
    from agent.data_agent.story import _normalize_newlines

    # LLM narration may contain literal "\n"; turn escapes into spaces so the
    # voice doesn't read them out and reads as one flowing sentence.
    text = " ".join(_normalize_newlines(text).split()).strip()
    if not text:
        return False
    try:
        from tools.tts_tool import text_to_speech_tool
        import json as _json

        raw = text_to_speech_tool(text, output_path=str(out_path))
        data = _json.loads(raw) if isinstance(raw, str) else (raw or {})
        return bool(data.get("success")) and out_path.exists()
    except Exception:
        logger.info("data_story narration TTS unavailable; continuing silently")
        return False


def _handle_data_story(args, **_kwargs):
    from agent.data_agent import story
    from agent.data_agent.artifacts import _slugify

    try:
        eng = _engine()
        run = _active_run()
        resolved = []
        for slide in args.get("slides") or []:
            item = dict(slide)
            if item.get("type") in {"chart", "table"} and item.get("sql"):
                result = eng.run_query(item["sql"])
                run.save_query(item["sql"], result)
                item["result"] = result
            resolved.append(item)

        # Optional voice-over → a real narrated "data video". audio_srcs[0] is the
        # intro scene; audio_srcs[i+1] aligns with resolved slide i.
        narrate = bool(args.get("narrate"))
        audio_srcs: list[str] = []
        narrated = 0
        if narrate:
            slug = _slugify(args["title"])
            out_dir = _output_dir()
            intro_text = args.get("intro_narration") or args.get("subtitle") or args["title"]
            intro_file = out_dir / f"story_{slug}_scene_intro.mp3"
            audio_srcs.append(intro_file.name if _synthesize_narration(intro_text, intro_file) else "")
            for i, item in enumerate(resolved):
                ntext = item.get("narration") or item.get("summary") or ""
                af = out_dir / f"story_{slug}_scene_{i}.mp3"
                if _synthesize_narration(ntext, af):
                    audio_srcs.append(af.name)
                    narrated += 1
                else:
                    audio_srcs.append("")

        html = story.render_story_html(
            args["title"],
            resolved,
            subtitle=args.get("subtitle", ""),
            audio_srcs=audio_srcs or None,
        )
        rec = run.save_report(args["title"] + " story", html)
        out = _output_dir() / f"story_{_slugify(args['title'])}.html"
        out.write_text(html, encoding="utf-8")
        return tool_result(
            {
                "artifact_id": rec["artifact_id"],
                "html_path": str(out),
                "workspace_file": out.name,
                "title": args["title"],
                "slides": len(resolved),
                "narrated": narrated,
                "is_video": narrated > 0,
            }
        )
    except Exception as exc:  # pragma: no cover
        logger.exception("data_story failed")
        return tool_error(f"data_story failed: {exc}")


# ---------------------------------------------------------------------------
# data_dbt  (auto-DBT: NL goal -> layered dbt-style pipeline, executed)
# ---------------------------------------------------------------------------

DATA_DBT_SCHEMA: Dict[str, Any] = {
    "name": "data_dbt",
    "description": (
        "Auto-build a dbt-style data-engineering pipeline from a natural-language "
        "goal over the connected database. Designs layered models "
        "(staging → intermediate → marts) that reference each other via "
        "ref()/source(), builds a lineage DAG, topologically executes each model "
        "as a real table/view in DuckDB, and reports per-model row counts. Use "
        "this for 'build an ETL/dbt pipeline', 'model this data', 'create a "
        "staging/marts layer', or repository-level data-engineering requests. "
        "The result includes the model DAG + final marts table."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "The business/data-engineering goal in plain language.",
            },
        },
        "required": ["goal"],
    },
}


def _handle_data_dbt(args, **_kwargs):
    from agent.data_agent import dbt_engine

    try:
        eng = _engine()
        res = dbt_engine.run_dbt(eng, args["goal"])
        # export a real dbt project layout for download
        try:
            manifest = dbt_engine.export_project(res, str(_output_dir()))
            res["export"] = {"root": manifest["root"], "n_files": manifest["n_files"]}
        except Exception:
            pass
        # compact the per-model preview so the result stays small
        for m in res.get("models", []):
            if m.get("preview") and len(m["preview"]) > 3:
                m["preview"] = m["preview"][:3]
        return tool_result(res)
    except Exception as exc:  # pragma: no cover
        logger.exception("data_dbt failed")
        return tool_error(f"data_dbt failed: {exc}")


# ---------------------------------------------------------------------------
# registration
#
# NOTE: tool discovery statically scans each module for a *top-level*
# ``registry.register(...)`` call (see tools/registry._module_registers_tools).
# So we must call registry.register directly at module scope — a for-loop over
# a table would be invisible to the scanner and the tools would never load.
# ---------------------------------------------------------------------------

registry.register(
    name="data_workflow_plan",
    toolset="data-agent",
    schema=DATA_WORKFLOW_PLAN_SCHEMA,
    handler=_handle_data_workflow_plan,
    check_fn=_check_engine,
    emoji="FLOW",
    max_result_size_chars=200_000,
)
registry.register(
    name="data_knowledge_connect",
    toolset="data-agent",
    schema=DATA_KNOWLEDGE_CONNECT_SCHEMA,
    handler=_handle_data_knowledge_connect,
    emoji="KNOW",
    max_result_size_chars=200_000,
)
registry.register(
    name="data_knowledge_search",
    toolset="data-agent",
    schema=DATA_KNOWLEDGE_SEARCH_SCHEMA,
    handler=_handle_data_knowledge_search,
    emoji="KNOW",
    max_result_size_chars=200_000,
)
registry.register(
    name="data_connect",
    toolset="data-agent",
    schema=DATA_CONNECT_SCHEMA,
    handler=_handle_data_connect,
    check_fn=_check_engine,
    emoji="DATA",
    max_result_size_chars=200_000,
)
registry.register(
    name="data_query",
    toolset="data-agent",
    schema=DATA_QUERY_SCHEMA,
    handler=_handle_data_query,
    check_fn=_check_engine,
    emoji="DATA",
    max_result_size_chars=200_000,
)
registry.register(
    name="data_parallel_query",
    toolset="data-agent",
    schema=DATA_PARALLEL_QUERY_SCHEMA,
    handler=_handle_data_parallel_query,
    check_fn=_check_engine,
    emoji="DATA",
    max_result_size_chars=200_000,
)
registry.register(
    name="data_visualize",
    toolset="data-agent",
    schema=DATA_VISUALIZE_SCHEMA,
    handler=_handle_data_visualize,
    check_fn=_check_engine,
    emoji="DATA",
    max_result_size_chars=200_000,
)
registry.register(
    name="data_dashboard",
    toolset="data-agent",
    schema=DATA_DASHBOARD_SCHEMA,
    handler=_handle_data_dashboard,
    check_fn=_check_engine,
    emoji="DATA",
    max_result_size_chars=200_000,
)
registry.register(
    name="data_document",
    toolset="data-agent",
    schema=DATA_DOCUMENT_SCHEMA,
    handler=_handle_data_document,
    check_fn=_check_engine,
    emoji="DATA",
    max_result_size_chars=200_000,
)
registry.register(
    name="data_excel",
    toolset="data-agent",
    schema=DATA_EXCEL_SCHEMA,
    handler=_handle_data_excel,
    check_fn=_check_engine,
    emoji="DATA",
    max_result_size_chars=200_000,
)
registry.register(
    name="data_excel_edit",
    toolset="data-agent",
    schema=DATA_EXCEL_EDIT_SCHEMA,
    handler=_handle_data_excel_edit,
    check_fn=_check_engine,
    emoji="DATA",
    max_result_size_chars=200_000,
)
registry.register(
    name="data_excel_formula",
    toolset="data-agent",
    schema=DATA_EXCEL_FORMULA_SCHEMA,
    handler=_handle_data_excel_formula,
    check_fn=_check_engine,
    emoji="DATA",
    max_result_size_chars=200_000,
)
registry.register(
    name="data_excel_fix",
    toolset="data-agent",
    schema=DATA_EXCEL_FIX_SCHEMA,
    handler=_handle_data_excel_fix,
    check_fn=_check_engine,
    emoji="DATA",
    max_result_size_chars=200_000,
)
registry.register(
    name="data_story",
    toolset="data-agent",
    schema=DATA_STORY_SCHEMA,
    handler=_handle_data_story,
    check_fn=_check_engine,
    emoji="STORY",
    max_result_size_chars=200_000,
)
registry.register(
    name="data_dbt",
    toolset="data-agent",
    schema=DATA_DBT_SCHEMA,
    handler=_handle_data_dbt,
    check_fn=_check_engine,
    emoji="DATA",
    max_result_size_chars=200_000,
)
