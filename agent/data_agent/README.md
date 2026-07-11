# DataAgent (`agent/data_agent/`)

A dedicated data-analysis capability that rides on top of DAClaw's general
`AIAgent` loop — it does **not** fork or rewrite `run_agent.py`. All logic lives
here in a pure service layer; the CLI, web server, and gateway all call the same
code through thin tool wrappers (`tools/data_agent_tools.py`).

Implements the "upload a table → beautiful visualization → conversational
charts → one-click document" line from
`docs/plans/2026-07-06-dataagent-demo-plan.md` (batch 13 / milestone M2.5).

## What it does

- **Connect** any local CSV / TSV / Parquet / Excel `.xlsx` / SQLite / DuckDB
  file behind one DuckDB-backed SQL interface (`engine.py`). Fully offline.
- **Query** it read-only; every result is a `ResultTable` with a stable
  `artifact_id`, sample rows in-context and the full result on disk
  (`artifacts.py`).
- **Visualize** a query result as a self-contained, interactive ECharts HTML
  chart — chart type auto-selected from the data shape (`viz.py`). ECharts is
  vendored (`assets/echarts.min.js`) so charts render offline in the sandboxed
  workspace iframe *and* open standalone in any browser.
- **Auto-dashboard** a freshly connected table: KPIs + time trend + category
  charts, all from real queries, no LLM needed for the first screen
  (`overview.py`).
- **Document**: assemble narrative + charts + tables + caveats into a polished
  self-contained HTML report; optional PDF (playwright / system Chrome / weasy-
  print) and DOCX (python-docx), degrading gracefully (`document.py`).
- Every number a chart or report shows is bound to the SQL that produced it —
  the evidence chain. Each analysis is a replayable `analysis_run`.

## Tools (in the `data-agent` toolset, also in DAClaw core)

| Tool | Purpose |
| --- | --- |
| `data_connect` | Load a data source; returns its tables + schema |
| `data_query` | Run read-only SQL; returns a `ResultTable` summary |
| `data_visualize` | SQL → one beautiful chart (HTML), auto chart-type |
| `data_dashboard` | Auto-overview of a table, or explicit multi-panel dashboard |
| `data_document` | Assemble a report (HTML, optional PDF/DOCX) |

## CLI

```
/data demo-seed [dir]    Generate the reproducible demo dataset
/data runs               List past analysis runs
/data artifacts <id>     Show a run's queries/charts/reports
/data sources            List sources connected this session
```

## Try it

```bash
daclaw            # then:  /data demo-seed
# In chat: "connect ./daclaw_demo_data/ecommerce.sqlite and build an overview dashboard"
#          "show monthly GMV as a line chart"
#          "make a performance report with those charts, export as PDF"
```

## Dependencies

Install the extra: `pip install -e ".[data]"` (duckdb, pandas, openpyxl,
pyarrow). ECharts ships vendored — no frontend chart library required. PDF/DOCX
exporters are optional and detected at runtime.

## Files

```
engine.py       DuckDB unified SQL layer (CSV/Parquet/Excel/SQLite/DuckDB)
artifacts.py    ResultTable envelope + AnalysisRun / ArtifactStore
viz.py          auto chart-type selection + self-contained ECharts HTML
theme.py        one palette / type scale / ECharts base options
overview.py     auto-dashboard builder from a table's schema
document.py     HTML report assembly + optional PDF/DOCX export
seed.py         fixed-seed synthetic e-commerce dataset generator
run_context.py  process-wide "current analysis run"
assets/echarts.min.js   vendored, inlined into every chart for offline use
```
