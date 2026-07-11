"""Assemble a self-contained HTML report from analysis sections.

This is the "one-click document" step (batch 13). A report is a list of
*sections*, each of which can be:

  - a markdown-ish narrative block (headings, paragraphs, bullet lists),
  - a chart (driven by a real :class:`ResultTable`, rendered inline with ECharts),
  - a data table,
  - a KPI row,
  - a "caveats / limitations" callout.

The output is ONE self-contained HTML file (ECharts inlined) that:
  - renders in the DAClaw web workspace iframe,
  - opens standalone in any browser,
  - can be stored as an ``analysis_run`` report artifact,
  - can optionally be exported to PDF (if a converter is installed).

L1 (HTML) is always available. L2 (PDF) / L3 (DOCX) degrade gracefully.
"""

from __future__ import annotations

import html as _html
import json
import re
from typing import Any, Dict, List, Optional

from agent.data_agent import theme, viz
from agent.data_agent.artifacts import ResultTable


def _esc(v: Any) -> str:
    return _html.escape("" if v is None else str(v))


def _md_inline(text: str) -> str:
    """Very small inline markdown: **bold**, `code`, and escape the rest."""
    out = _esc(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`(.+?)`", r"<code>\1</code>", out)
    return out


def _render_narrative(text: str) -> str:
    """Render a small subset of markdown: #/## headings, - bullets, paragraphs."""
    lines = text.split("\n")
    html_parts: List[str] = []
    bullets: List[str] = []

    def flush_bullets():
        if bullets:
            items = "".join(f"<li>{_md_inline(b)}</li>" for b in bullets)
            html_parts.append(f"<ul>{items}</ul>")
            bullets.clear()

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush_bullets()
            continue
        if line.startswith("### "):
            flush_bullets()
            html_parts.append(f"<h3>{_md_inline(line[4:])}</h3>")
        elif line.startswith("## "):
            flush_bullets()
            html_parts.append(f"<h2>{_md_inline(line[3:])}</h2>")
        elif line.startswith("# "):
            flush_bullets()
            html_parts.append(f"<h2>{_md_inline(line[2:])}</h2>")
        elif line.lstrip().startswith(("- ", "* ")):
            bullets.append(line.lstrip()[2:])
        else:
            html_parts.append(f"<p>{_md_inline(line)}</p>")
    flush_bullets()
    return "\n".join(html_parts)


_REPORT_CSS = f"""
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: {theme.PAGE_BG}; color: {theme.INK};
    font-family: {theme.FONT_STACK}; line-height: 1.6; }}
  .doc {{ max-width: 880px; margin: 0 auto; padding: 48px 32px 80px;
    background: {theme.PANEL_BG}; min-height: 100vh;
    box-shadow: 0 0 40px rgba(15,23,42,0.05); }}
  .doc-header {{ border-bottom: 3px solid {theme.BRAND}; padding-bottom: 16px; margin-bottom: 8px; }}
  .doc-header h1 {{ margin: 0 0 6px; font-size: 30px; letter-spacing: -0.02em; }}
  .doc-meta {{ color: {theme.MUTED}; font-size: 13px; }}
  h2 {{ font-size: 22px; margin: 36px 0 10px; }}
  h3 {{ font-size: 17px; margin: 24px 0 8px; color: {theme.MUTED}; }}
  p {{ margin: 10px 0; }}
  ul {{ margin: 10px 0; padding-left: 22px; }}
  li {{ margin: 4px 0; }}
  code {{ background: {theme.PAGE_BG}; padding: 1px 6px; border-radius: 5px;
    font-size: 0.9em; color: #0d9488; }}
  .chart {{ width: 100%; height: 400px; margin: 8px 0; }}
  .kpis {{ display: grid; gap: 14px; margin: 18px 0; }}
  .kpi {{ background: {theme.PAGE_BG}; border: 1px solid {theme.GRID};
    border-radius: 12px; padding: 14px 16px; }}
  .kpi .label {{ color: {theme.MUTED}; font-size: 12px; margin-bottom: 4px; }}
  .kpi .value {{ font-size: 26px; font-weight: 700; }}
  .callout {{ background: #fffbeb; border-left: 4px solid {theme.PALETTE[2]};
    border-radius: 8px; padding: 12px 16px; margin: 18px 0; }}
  .callout h3 {{ margin: 0 0 6px; color: #92400e; }}
  .data-table {{ border-collapse: collapse; width: 100%; font-size: 13px; margin: 12px 0; }}
  .data-table th {{ text-align: left; padding: 8px 10px; border-bottom: 2px solid {theme.GRID};
    color: {theme.MUTED}; font-weight: 600; }}
  .data-table td {{ padding: 7px 10px; border-bottom: 1px solid {theme.GRID}; }}
  .prov {{ color: {theme.MUTED}; font-size: 12px; margin-top: 4px; }}
  .figure-cap {{ color: {theme.MUTED}; font-size: 13px; text-align: center; margin-top: 4px; }}
  /* SQL provenance code block (matches viz.py) */
  .sql-block {{ margin: 8px 0 4px; }}
  .sql-block > summary {{ cursor: pointer; color: {theme.MUTED}; font-size: 12px;
    font-weight: 600; letter-spacing: .02em; text-transform: uppercase; user-select: none;
    list-style: none; padding: 2px 0; }}
  .sql-block > summary::-webkit-details-marker {{ display: none; }}
  .sql-block > summary::before {{ content: "▸ "; }}
  .sql-block[open] > summary::before {{ content: "▾ "; }}
  .sql-code {{ margin: 6px 0 0; padding: 12px 14px; border-radius: 10px;
    background: #0f172a; overflow-x: auto;
    font-family: {getattr(theme, 'MONO_STACK', 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace')};
    font-size: 12.5px; line-height: 1.55; white-space: pre-wrap; word-break: break-word; }}
  .sql-code code {{ background: none; padding: 0; color: #e2e8f0; font-size: inherit; }}
  .sql-code .s-kw {{ color: #7dd3fc; font-weight: 600; }}
  .sql-code .s-str {{ color: #fca5a5; }}
  .sql-code .s-num {{ color: #fcd34d; }}
  @media print {{ body {{ background: #fff; }} .doc {{ box-shadow: none; max-width: none; }} }}
"""


def _table_html(columns: List[str], rows: List[List[Any]], limit: int = 100) -> str:
    head = "".join(f"<th>{_esc(c)}</th>" for c in columns)
    body = []
    for row in rows[:limit]:
        body.append("<tr>" + "".join(f"<td>{_esc(v)}</td>" for v in row) + "</tr>")
    more = f'<p class="prov">… {len(rows)-limit} more rows</p>' if len(rows) > limit else ""
    return f'<table class="data-table"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>{more}'


def build_report_html(
    title: str,
    sections: List[Dict[str, Any]],
    *,
    subtitle: str = "",
    generated_at: str = "",
    inline_echarts: bool = True,
) -> str:
    """Build a self-contained HTML report.

    ``sections`` is a list of dicts, each with a ``type`` key:
      - {"type": "narrative", "text": "## heading\\n- bullet ..."}
      - {"type": "chart", "title": str, "result": ResultTable,
         "chart_type": Optional[str], "provenance": Optional[str]}
      - {"type": "table", "title": str, "result": ResultTable}
      - {"type": "kpis", "items": [{"label": str, "value": str}, ...]}
      - {"type": "caveats", "items": [str, ...]}
    """
    body_parts: List[str] = []
    scripts: List[str] = []
    chart_i = 0

    for sec in sections:
        stype = sec.get("type")
        if stype == "narrative":
            body_parts.append(_render_narrative(sec.get("text", "")))
        elif stype == "kpis":
            cards = "".join(
                f'<div class="kpi"><div class="label">{_esc(it.get("label",""))}</div>'
                f'<div class="value">{_esc(it.get("value",""))}</div></div>'
                for it in sec.get("items", [])
            )
            n = min(len(sec.get("items", [])) or 1, 4)
            body_parts.append(
                f'<div class="kpis" style="grid-template-columns:repeat({n},1fr)">{cards}</div>'
            )
        elif stype == "chart":
            result: ResultTable = sec["result"]
            if sec.get("title"):
                body_parts.append(f'<h3>{_esc(sec["title"])}</h3>')
            option = viz.build_chart_option(result, chart_type=sec.get("chart_type"))
            if option.get("_table"):
                body_parts.append(_table_html(option["columns"], option["rows"]))
            else:
                eid = f"c{chart_i}"
                chart_i += 1
                body_parts.append(f'<div id="{eid}" class="chart"></div>')
                if sec.get("caption"):
                    body_parts.append(f'<div class="figure-cap">{_esc(sec["caption"])}</div>')
                opt_json = json.dumps(option, ensure_ascii=False)
                scripts.append(
                    f"(function(){{var ch=echarts.init(document.getElementById('{eid}'));"
                    f"ch.setOption({opt_json});charts.push(ch);}})();"
                )
            if sec.get("provenance"):
                body_parts.append(viz.render_sql_block(sec["provenance"]))
        elif stype == "table":
            result = sec["result"]
            if sec.get("title"):
                body_parts.append(f'<h3>{_esc(sec["title"])}</h3>')
            body_parts.append(_table_html(result.column_names, result.rows))
            if sec.get("provenance"):
                body_parts.append(viz.render_sql_block(sec["provenance"]))
        elif stype == "caveats":
            items = "".join(f"<li>{_md_inline(it)}</li>" for it in sec.get("items", []))
            title_txt = sec.get("title", "Limitations & caveats")
            body_parts.append(
                f'<div class="callout"><h3>{_esc(title_txt)}</h3><ul>{items}</ul></div>'
            )

    script_block = ""
    if scripts:
        script_block = (
            "<script>var charts=[];"
            + "".join(scripts)
            + "window.addEventListener('resize',function(){charts.forEach(function(c){c.resize();});});"
            + "</script>"
        )

    meta_line = " · ".join(x for x in [subtitle, generated_at] if x)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
{viz._echarts_script_tag(inline_echarts)}
<style>{_REPORT_CSS}</style></head>
<body><div class="doc">
  <div class="doc-header">
    <h1>{_esc(title)}</h1>
    {f'<div class="doc-meta">{_esc(meta_line)}</div>' if meta_line else ''}
  </div>
  {"".join(body_parts)}
</div>{script_block}</body></html>"""


# ---------------------------------------------------------------------------
# optional exporters (graceful degradation)
# ---------------------------------------------------------------------------


def _find_system_chrome() -> Optional[str]:
    """Locate a system Chrome/Chromium/Edge binary for headless PDF export."""
    import shutil

    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
    ]
    for c in candidates:
        if "/" in c:
            from pathlib import Path as _P

            if _P(c).exists():
                return c
        else:
            found = shutil.which(c)
            if found:
                return found
    return None


def export_pdf(html: str, out_path: str) -> Dict[str, Any]:
    """Best-effort HTML->PDF. Returns {"ok", "path"|"reason"}.

    Tries, in order: playwright (bundled chromium), a system Chrome/Edge in
    headless mode, then weasyprint. If none work this returns ok=False with a
    clear reason rather than raising — the HTML report is always the primary
    deliverable.
    """
    errors: Dict[str, str] = {}

    # 1. playwright (best fidelity for ECharts / JS)
    try:
        from playwright.sync_api import sync_playwright  # type: ignore

        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as fh:
            fh.write(html)
            tmp = fh.name
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"file://{tmp}")
            page.wait_for_timeout(600)  # let ECharts draw
            page.pdf(path=out_path, print_background=True, format="A4")
            browser.close()
        return {"ok": True, "path": out_path, "engine": "playwright"}
    except Exception as exc:
        errors["playwright"] = str(exc)

    # 2. system Chrome/Edge headless --print-to-pdf (works with plain Chrome)
    chrome = _find_system_chrome()
    if chrome:
        try:
            import subprocess
            import tempfile

            with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as fh:
                fh.write(html)
                tmp = fh.name
            subprocess.run(
                [
                    chrome,
                    "--headless",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--virtual-time-budget=2000",  # let JS/ECharts settle
                    f"--print-to-pdf={out_path}",
                    "--no-pdf-header-footer",
                    f"file://{tmp}",
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
            from pathlib import Path as _P

            if _P(out_path).exists():
                return {"ok": True, "path": out_path, "engine": "system-chrome"}
            errors["system-chrome"] = "chrome ran but produced no file"
        except Exception as exc:
            errors["system-chrome"] = str(exc)

    # 3. weasyprint (no JS; charts must be static — limited)
    try:
        from weasyprint import HTML  # type: ignore

        HTML(string=html).write_pdf(out_path)
        return {"ok": True, "path": out_path, "engine": "weasyprint",
                "note": "weasyprint does not run JS; interactive charts may not render"}
    except Exception as exc:
        errors["weasyprint"] = str(exc)

    return {
        "ok": False,
        "reason": (
            "PDF export needs playwright (chromium), a system Chrome/Edge, or "
            "weasyprint. Install one, e.g. `pip install playwright && playwright "
            "install chromium`. The HTML report is available regardless."
        ),
        "errors": errors,
    }


def export_docx(sections: List[Dict[str, Any]], title: str, out_path: str) -> Dict[str, Any]:
    """Best-effort DOCX export (text + tables; charts become notes)."""
    try:
        from docx import Document  # type: ignore
    except Exception:
        return {
            "ok": False,
            "reason": "DOCX export needs 'python-docx' (pip install python-docx). "
            "HTML report is available regardless.",
        }
    doc = Document()
    doc.add_heading(title, level=0)
    for sec in sections:
        stype = sec.get("type")
        if stype == "narrative":
            for line in sec.get("text", "").split("\n"):
                s = line.strip()
                if not s:
                    continue
                if s.startswith("## "):
                    doc.add_heading(s[3:], level=1)
                elif s.startswith("### "):
                    doc.add_heading(s[4:], level=2)
                elif s.startswith(("- ", "* ")):
                    doc.add_paragraph(s[2:], style="List Bullet")
                else:
                    doc.add_paragraph(s)
        elif stype in ("table", "chart") and sec.get("result") is not None:
            result: ResultTable = sec["result"]
            if sec.get("title"):
                doc.add_heading(sec["title"], level=2)
            if stype == "chart":
                doc.add_paragraph(f"[chart: {sec.get('chart_type') or 'auto'}]")
            cols = result.column_names
            table = doc.add_table(rows=1, cols=len(cols))
            for i, c in enumerate(cols):
                table.rows[0].cells[i].text = str(c)
            for row in result.rows[:100]:
                cells = table.add_row().cells
                for i, v in enumerate(row):
                    cells[i].text = "" if v is None else str(v)
        elif stype == "kpis":
            for it in sec.get("items", []):
                doc.add_paragraph(f"{it.get('label','')}: {it.get('value','')}")
        elif stype == "caveats":
            doc.add_heading(sec.get("title", "Limitations & caveats"), level=2)
            for it in sec.get("items", []):
                doc.add_paragraph(it, style="List Bullet")
    doc.save(out_path)
    return {"ok": True, "path": out_path, "engine": "python-docx"}
