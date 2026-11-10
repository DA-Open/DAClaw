"""Turn a result table into a beautiful, self-contained ECharts HTML chart.

Design (batch 13 of the demo plan):

- The output is ONE self-contained HTML file with ECharts inlined, so it renders
  inside the DAClaw web workspace's sandboxed iframe with no network and no
  frontend chart library — and it also opens standalone in any browser.
- Chart type can be auto-selected from the data's column *semantics* (time /
  category / measure / id), so "upload a table → see the right chart" needs no
  user input.
- Charts are driven by real query results (a :class:`ResultTable`), never by
  numbers the model made up — this keeps the evidence chain intact.

Public API:
    select_chart_type(result)          -> str
    build_chart_option(result, ...)    -> dict   (ECharts option)
    render_chart_html(result, ...)     -> str    (full HTML doc)
    render_dashboard_html(panels, ...) -> str    (multi-chart HTML doc)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.data_agent import theme
from agent.data_agent.artifacts import ResultTable

_ASSETS = Path(__file__).parent / "assets"
_ECHARTS_PATH = _ASSETS / "echarts.min.js"
_ECHARTS_CDN = "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"

CHART_TYPES = {"line", "bar", "bar_horizontal", "pie", "scatter", "heatmap", "area", "table"}


# ---------------------------------------------------------------------------
# column-role inference
# ---------------------------------------------------------------------------

_NUMERIC_HINTS = ("int", "double", "float", "decimal", "hugeint", "numeric", "real", "bigint")
_TIME_HINTS = ("date", "time", "timestamp")


def _role_of(col: Dict[str, str], values: List[Any]) -> str:
    """Infer a charting role for a column: time | measure | category | id."""
    name = col["name"].lower()
    ctype = str(col.get("type", "")).lower()
    if any(h in ctype for h in _TIME_HINTS):
        return "time"
    if name.endswith("_id") or name == "id":
        return "id"
    if any(h in ctype for h in _NUMERIC_HINTS):
        return "measure"
    # datelike string values are a strong temporal signal on their own
    # (e.g. "2025-01", "2025-03-14"), even when the column isn't named nicely.
    if _looks_datelike(values):
        return "time"
    # value-based fallback: all parseable numbers -> measure
    if values and all(_is_number(v) for v in values if v is not None):
        return "measure"
    return "category"


def _is_number(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    try:
        float(str(v).replace(",", ""))
        return True
    except (TypeError, ValueError):
        return False


def _looks_datelike(values: List[Any]) -> bool:
    sample = [v for v in values if v is not None][:20]
    if not sample:
        return False
    hits = 0
    for v in sample:
        s = str(v)
        if len(s) >= 7 and s[:4].isdigit() and ("-" in s or "/" in s or ":" in s):
            hits += 1
    return hits >= max(1, len(sample) // 2)


def _roles(result: ResultTable) -> Dict[str, str]:
    roles: Dict[str, str] = {}
    for col in result.columns:
        roles[col["name"]] = _role_of(col, result.column_values(col["name"]))
    return roles


# ---------------------------------------------------------------------------
# chart-type selection
# ---------------------------------------------------------------------------


def select_chart_type(result: ResultTable) -> str:
    """Pick a sensible chart type from the shape of the result."""
    roles = _roles(result)
    role_list = list(roles.values())
    measures = [n for n, r in roles.items() if r == "measure"]
    times = [n for n, r in roles.items() if r == "time"]
    cats = [n for n, r in roles.items() if r == "category"]

    n = result.row_count
    if not measures:
        return "table"
    # time series -> line
    if times and measures:
        return "line"
    # one category + one measure
    if cats and measures:
        # few slices summing to a whole -> pie; otherwise bar
        if len(cats) == 1 and len(measures) == 1 and 2 <= n <= 8:
            return "pie"
        if n > 12:
            return "bar_horizontal"
        return "bar"
    # two measures, no category -> scatter
    if len(measures) >= 2 and not cats and not times:
        return "scatter"
    # single measure column, many rows -> bar
    if len(measures) == 1:
        return "bar"
    return "table"


# ---------------------------------------------------------------------------
# ECharts option builders
# ---------------------------------------------------------------------------


def _merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    out.update(extra)
    return out


def build_chart_option(
    result: ResultTable,
    *,
    chart_type: Optional[str] = None,
    title: str = "",
    subtitle: str = "",
) -> Dict[str, Any]:
    """Build an ECharts option dict for *result*."""
    ct = chart_type or select_chart_type(result)
    roles = _roles(result)
    measures = [n for n, r in roles.items() if r == "measure"]
    times = [n for n, r in roles.items() if r == "time"]
    cats = [n for n, r in roles.items() if r == "category"]

    base = theme.echarts_base_options()
    title_block = {
        "title": {
            "text": title,
            "subtext": subtitle,
            "left": "center",
            "textStyle": {"color": theme.INK, "fontSize": 18, "fontWeight": 600},
            "subtextStyle": {"color": theme.MUTED},
        }
    }

    if ct == "table":
        return {"_table": True, "columns": result.column_names, "rows": result.rows}

    # choose the dimension column
    dim = (times or cats or [result.column_names[0]])[0]
    dim_idx = result.column_names.index(dim)
    dim_values = [row[dim_idx] for row in result.rows]

    if ct in ("line", "area"):
        return _line_option(result, dim, dim_values, measures, base, title_block, area=(ct == "area"))
    if ct in ("bar", "bar_horizontal"):
        return _bar_option(result, dim, dim_values, measures, base, title_block, horizontal=(ct == "bar_horizontal"))
    if ct == "pie":
        return _pie_option(result, dim, dim_values, measures[0], base, title_block)
    if ct == "scatter":
        return _scatter_option(result, measures, base, title_block)
    if ct == "heatmap":
        return _heatmap_option(result, cats, measures, base, title_block)
    # fallback
    return _bar_option(result, dim, dim_values, measures, base, title_block, horizontal=False)


def _series_data(result: ResultTable, col: str) -> List[Any]:
    idx = result.column_names.index(col)
    out = []
    for row in result.rows:
        v = row[idx]
        out.append(_num(v))
    return out


def _num(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _line_option(result, dim, dim_values, measures, base, title_block, area=False):
    axis = theme.axis_style()
    series = []
    for m in measures:
        s = {
            "name": m,
            "type": "line",
            "smooth": True,
            "showSymbol": len(dim_values) <= 40,
            "data": _series_data(result, m),
            "lineStyle": {"width": 3},
        }
        if area:
            s["areaStyle"] = {"opacity": 0.15}
        series.append(s)
    return _merge(
        base,
        {
            **title_block,
            "tooltip": {"trigger": "axis"},
            "legend": {"top": 8, "textStyle": {"color": theme.MUTED}} if len(measures) > 1 else {"show": False},
            "xAxis": _merge({"type": "category", "data": [str(v) for v in dim_values], "boundaryGap": False}, axis),
            "yAxis": _merge({"type": "value"}, axis),
            "series": series,
        },
    )


def _bar_option(result, dim, dim_values, measures, base, title_block, horizontal=False):
    axis = theme.axis_style()
    cat_axis = _merge({"type": "category", "data": [str(v) for v in dim_values]}, axis)
    val_axis = _merge({"type": "value"}, axis)
    series = [
        {
            "name": m,
            "type": "bar",
            "data": _series_data(result, m),
            "itemStyle": {"borderRadius": [4, 4, 0, 0] if not horizontal else [0, 4, 4, 0]},
            "barMaxWidth": 40,
        }
        for m in measures
    ]
    if horizontal:
        # horizontal bars read top-to-bottom; reverse so largest is on top
        cat_axis["data"] = list(reversed(cat_axis["data"]))
        for s in series:
            s["data"] = list(reversed(s["data"]))
            s["itemStyle"] = {"borderRadius": [0, 4, 4, 0]}
    return _merge(
        base,
        {
            **title_block,
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
            "legend": {"top": 8, "textStyle": {"color": theme.MUTED}} if len(measures) > 1 else {"show": False},
            "xAxis": val_axis if horizontal else cat_axis,
            "yAxis": cat_axis if horizontal else val_axis,
            "series": series,
        },
    )


def _pie_option(result, dim, dim_values, measure, base, title_block):
    mdata = _series_data(result, measure)
    data = [
        {"name": str(dim_values[i]), "value": mdata[i]}
        for i in range(len(dim_values))
        if mdata[i] is not None
    ]
    return _merge(
        base,
        {
            **title_block,
            "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
            "legend": {"orient": "vertical", "left": "left", "top": "middle", "textStyle": {"color": theme.MUTED}},
            "series": [
                {
                    "name": measure,
                    "type": "pie",
                    "radius": ["42%", "70%"],
                    "center": ["58%", "54%"],
                    "avoidLabelOverlap": True,
                    "itemStyle": {"borderColor": "#fff", "borderWidth": 2, "borderRadius": 6},
                    "label": {"show": True, "formatter": "{b}\n{d}%", "color": theme.MUTED},
                    "data": data,
                }
            ],
        },
    )


def _scatter_option(result, measures, base, title_block):
    axis = theme.axis_style()
    xcol, ycol = measures[0], measures[1]
    xs = _series_data(result, xcol)
    ys = _series_data(result, ycol)
    pts = [[xs[i], ys[i]] for i in range(len(xs)) if xs[i] is not None and ys[i] is not None]
    return _merge(
        base,
        {
            **title_block,
            "tooltip": {"trigger": "item"},
            "legend": {"show": False},
            "xAxis": _merge({"type": "value", "name": xcol, "scale": True}, axis),
            "yAxis": _merge({"type": "value", "name": ycol, "scale": True}, axis),
            "series": [{"type": "scatter", "symbolSize": 12, "data": pts, "itemStyle": {"opacity": 0.7}}],
        },
    )


def _heatmap_option(result, cats, measures, base, title_block):
    # expects: cat0 (x), cat1 (y), measure (value)
    axis = theme.axis_style()
    if len(cats) < 2 or not measures:
        return _bar_option(result, cats[0] if cats else result.column_names[0], result.column_values(result.column_names[0]), measures, base, title_block, False)
    xcol, ycol, mcol = cats[0], cats[1], measures[0]
    xi, yi, mi = (result.column_names.index(c) for c in (xcol, ycol, mcol))
    xvals = sorted({str(r[xi]) for r in result.rows})
    yvals = sorted({str(r[yi]) for r in result.rows})
    xindex = {v: i for i, v in enumerate(xvals)}
    yindex = {v: i for i, v in enumerate(yvals)}
    data = []
    mmax = 0.0
    for r in result.rows:
        val = _num(r[mi]) or 0
        mmax = max(mmax, val)
        data.append([xindex[str(r[xi])], yindex[str(r[yi])], val])
    return _merge(
        base,
        {
            **title_block,
            "tooltip": {"position": "top"},
            "grid": {"left": 60, "right": 24, "top": 60, "bottom": 60, "containLabel": True},
            "xAxis": _merge({"type": "category", "data": xvals, "splitArea": {"show": True}}, axis),
            "yAxis": _merge({"type": "category", "data": yvals, "splitArea": {"show": True}}, axis),
            "visualMap": {
                "min": 0,
                "max": mmax or 1,
                "calculable": True,
                "orient": "horizontal",
                "left": "center",
                "bottom": 10,
                "inRange": {"color": theme.HEAT_RAMP},
            },
            "series": [{"name": mcol, "type": "heatmap", "data": data, "label": {"show": False}}],
        },
    )


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def _echarts_script_tag(inline: bool) -> str:
    if inline and _ECHARTS_PATH.exists():
        js = _ECHARTS_PATH.read_text(encoding="utf-8")
        return f"<script>{js}</script>"
    return f'<script src="{_ECHARTS_CDN}"></script>'


def _table_html(columns: List[str], rows: List[List[Any]], limit: int = 200) -> str:
    head = "".join(f"<th>{_esc(c)}</th>" for c in columns)
    body_rows = []
    for row in rows[:limit]:
        cells = "".join(f"<td>{_esc(v)}</td>" for v in row)
        body_rows.append(f"<tr>{cells}</tr>")
    more = ""
    if len(rows) > limit:
        more = f'<p class="muted">… {len(rows) - limit} more rows</p>'
    return (
        f'<table class="data-table"><thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody></table>{more}'
    )


def _esc(v: Any) -> str:
    import html

    return html.escape("" if v is None else str(v))


# --- SQL pretty-printing + syntax highlighting for provenance blocks --------
import re as _re

_SQL_KEYWORDS = (
    "WITH", "SELECT", "DISTINCT", "FROM", "WHERE", "GROUP BY", "ORDER BY",
    "HAVING", "LIMIT", "OFFSET", "JOIN", "LEFT JOIN", "RIGHT JOIN",
    "INNER JOIN", "FULL JOIN", "CROSS JOIN", "ON", "USING", "AND", "OR",
    "NOT", "AS", "CASE", "WHEN", "THEN", "ELSE", "END", "UNION ALL",
    "UNION", "OVER", "PARTITION BY", "VALUES", "IN", "IS", "NULL", "ASC",
    "DESC", "BETWEEN", "LIKE",
)
# keywords that should start a new line
_SQL_NEWLINE_KW = (
    "WITH", "SELECT", "FROM", "WHERE", "GROUP BY", "ORDER BY", "HAVING",
    "LIMIT", "LEFT JOIN", "RIGHT JOIN", "INNER JOIN", "FULL JOIN",
    "CROSS JOIN", "JOIN", "UNION ALL", "UNION",
)


def format_sql(sql: str) -> str:
    """Light pretty-printer: collapse whitespace, break before major clauses."""
    s = _re.sub(r"\s+", " ", (sql or "").strip())
    if not s:
        return ""
    # newline before major clause keywords (word-boundary, case-insensitive)
    for kw in sorted(_SQL_NEWLINE_KW, key=len, reverse=True):
        s = _re.sub(rf"(?<![\w]) ?\b{kw}\b", "\n" + kw, s, flags=_re.IGNORECASE)
    # keep leading WITH on first line, drop a leading blank line
    lines = [ln.strip() for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def highlight_sql(sql: str) -> str:
    """Return HTML with keywords/strings/numbers wrapped in styled spans.

    Input is pretty-printed then HTML-escaped, so it is safe to embed.
    """
    pretty = format_sql(sql)
    esc = _esc(pretty)
    # strings first (so keywords inside strings aren't highlighted)
    esc = _re.sub(r"(&#x27;[^&]*?&#x27;|&#39;[^&]*?&#39;|&#x27;.*?&#x27;)", r'<span class="s-str">\1</span>', esc)
    # numbers
    esc = _re.sub(r"\b(\d+\.?\d*)\b", r'<span class="s-num">\1</span>', esc)
    # keywords
    for kw in sorted(_SQL_KEYWORDS, key=len, reverse=True):
        esc = _re.sub(rf"(?<![\w])({_re.escape(kw)})(?![\w])", r'<span class="s-kw">\1</span>', esc, flags=_re.IGNORECASE)
    return esc


def render_sql_block(sql: str, *, label: str = "Source query") -> str:
    """A styled, highlighted, wrapped <pre> code block for a provenance SQL."""
    if not sql:
        return ""
    return (
        f'<details class="sql-block" open><summary>{_esc(label)}</summary>'
        f'<pre class="sql-code"><code>{highlight_sql(sql)}</code></pre></details>'
    )


_PAGE_CSS = f"""
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: {theme.PAGE_BG}; color: {theme.INK};
    font-family: {theme.FONT_STACK}; }}
  .wrap {{ max-width: 1080px; margin: 0 auto; padding: 24px; }}
  .card {{ background: {theme.PANEL_BG}; border: 1px solid {theme.GRID};
    border-radius: 14px; padding: 16px 18px; box-shadow: 0 1px 3px rgba(15,23,42,0.06);
    margin-bottom: 20px; }}
  .chart {{ width: 100%; height: 420px; }}
  .muted {{ color: {theme.MUTED}; font-size: 13px; }}
  h1 {{ font-size: 22px; margin: 4px 0 2px; }}
  .data-table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  .data-table th {{ text-align: left; padding: 8px 10px; border-bottom: 2px solid {theme.GRID};
    color: {theme.MUTED}; font-weight: 600; }}
  .data-table td {{ padding: 7px 10px; border-bottom: 1px solid {theme.GRID}; }}
  .data-table tbody tr:hover {{ background: {theme.PAGE_BG}; }}
  .grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 20px; }}
  @media (max-width: 760px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  /* SQL provenance code block */
  .sql-block {{ margin-top: 12px; }}
  .sql-block > summary {{ cursor: pointer; color: {theme.MUTED}; font-size: 12px;
    font-weight: 600; letter-spacing: .02em; text-transform: uppercase; user-select: none;
    list-style: none; padding: 2px 0; }}
  .sql-block > summary::-webkit-details-marker {{ display: none; }}
  .sql-block > summary::before {{ content: "▸ "; }}
  .sql-block[open] > summary::before {{ content: "▾ "; }}
  .sql-code {{ margin: 6px 0 0; padding: 12px 14px; border-radius: 10px;
    background: #0f172a; color: #e2e8f0; overflow-x: auto;
    font-family: {getattr(theme, 'MONO_STACK', 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace')};
    font-size: 12.5px; line-height: 1.55; white-space: pre-wrap; word-break: break-word;
    tab-size: 2; }}
  .sql-code .s-kw {{ color: #7dd3fc; font-weight: 600; }}
  .sql-code .s-str {{ color: #fca5a5; }}
  .sql-code .s-num {{ color: #fcd34d; }}
"""


def render_chart_html(
    result: ResultTable,
    *,
    chart_type: Optional[str] = None,
    title: str = "Chart",
    subtitle: str = "",
    inline_echarts: bool = True,
    provenance: Optional[str] = None,
) -> str:
    """Render one chart as a full self-contained HTML document."""
    option = build_chart_option(result, chart_type=chart_type, title="", subtitle="")

    prov_html = ""
    if provenance:
        prov_html = render_sql_block(provenance)

    if option.get("_table"):
        inner = _table_html(option["columns"], option["rows"])
        chart_block = f'<div class="card">{inner}{prov_html}</div>'
        script = ""
    else:
        opt_json = json.dumps(option, ensure_ascii=False)
        chart_block = f'<div class="card"><div id="c0" class="chart"></div>{prov_html}</div>'
        script = f"""<script>
    var ch = echarts.init(document.getElementById('c0'));
    ch.setOption({opt_json});
    window.addEventListener('resize', function(){{ ch.resize(); }});
  </script>"""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
{_echarts_script_tag(inline_echarts)}
<style>{_PAGE_CSS}</style></head>
<body><div class="wrap">
  <h1>{_esc(title)}</h1>
  {f'<p class="muted">{_esc(subtitle)}</p>' if subtitle else ''}
  {chart_block}
</div>{script}</body></html>"""


def self_check_html(html: str) -> Dict[str, Any]:
    """Cheap pre-render sanity check to avoid shipping a white-screen chart.

    Returns {"ok": bool, "issues": [...]}. This is deterministic and fast — it
    doesn't run a browser, just verifies the structural invariants that cause
    blank charts in practice.

    Note: we can't count ``setOption(`` globally because the *inlined* ECharts
    library contains that string many times. Instead we check that every chart
    container id we emit has a matching ``getElementById('<id>')`` init call.
    """
    import re

    issues: List[str] = []
    if "<!DOCTYPE html>" not in html:
        issues.append("missing doctype")
    if "echarts" not in html.lower():
        issues.append("echarts not referenced")

    # container ids we emit look like id="c0", id="c1", ...
    container_ids = set(re.findall(r'id="(c\d+)"\s+class="chart"', html))
    inited_ids = set(re.findall(r"getElementById\('(c\d+)'\)", html))
    has_table = 'class="data-table"' in html

    if not container_ids and not has_table:
        issues.append("no chart container and no table fallback")
    missing = container_ids - inited_ids
    if missing:
        issues.append(f"chart containers without init: {sorted(missing)}")
    orphan = inited_ids - container_ids
    if orphan:
        issues.append(f"init calls without a container: {sorted(orphan)}")
    if 'class="chart"' in html and "height" not in _PAGE_CSS:
        issues.append("chart container has no height")
    return {"ok": not issues, "issues": issues}


def render_dashboard_html(
    panels: List[Dict[str, Any]],
    *,
    title: str = "Overview",
    subtitle: str = "",
    inline_echarts: bool = True,
) -> str:
    """Render a multi-panel dashboard from a list of panels.

    Each panel: {"title": str, "result": ResultTable, "chart_type": Optional[str],
                 "provenance": Optional[str], "kpi": Optional[str]}
    """
    kpis = [p for p in panels if p.get("kpi")]
    charts = [p for p in panels if not p.get("kpi")]

    kpi_html = ""
    if kpis:
        cards = []
        for p in kpis:
            cards.append(
                f'<div class="card" style="margin:0"><p class="muted" style="margin:0 0 6px">{_esc(p["title"])}</p>'
                f'<div style="font-size:28px;font-weight:700;color:{theme.INK}">{_esc(p["kpi"])}</div></div>'
            )
        kpi_html = f'<div class="grid" style="grid-template-columns:repeat({min(len(cards),4)},1fr);margin-bottom:20px">{"".join(cards)}</div>'

    blocks = []
    scripts = []
    for i, p in enumerate(charts):
        result: ResultTable = p["result"]
        option = build_chart_option(result, chart_type=p.get("chart_type"), title="", subtitle="")
        prov = ""
        if p.get("provenance"):
            prov = render_sql_block(p["provenance"], label="Source")
        head = f'<p class="muted" style="margin:0 0 8px;font-weight:600;color:{theme.INK}">{_esc(p.get("title",""))}</p>'
        if option.get("_table"):
            blocks.append(f'<div class="card">{head}{_table_html(option["columns"], option["rows"], limit=50)}{prov}</div>')
        else:
            eid = f"c{i}"
            blocks.append(f'<div class="card">{head}<div id="{eid}" class="chart" style="height:360px"></div>{prov}</div>')
            opt_json = json.dumps(option, ensure_ascii=False)
            scripts.append(
                f"(function(){{var ch=echarts.init(document.getElementById('{eid}'));"
                f"ch.setOption({opt_json});charts.push(ch);}})();"
            )

    grid = f'<div class="grid">{"".join(blocks)}</div>' if len(blocks) != 1 else "".join(blocks)
    script_block = ""
    if scripts:
        script_block = (
            "<script>var charts=[];"
            + "".join(scripts)
            + "window.addEventListener('resize',function(){charts.forEach(function(c){c.resize();});});"
            + "</script>"
        )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
{_echarts_script_tag(inline_echarts)}
<style>{_PAGE_CSS}</style></head>
<body><div class="wrap">
  <h1>{_esc(title)}</h1>
  {f'<p class="muted">{_esc(subtitle)}</p>' if subtitle else ''}
  {kpi_html}
  {grid}
</div>{script_block}</body></html>"""


# ---------------------------------------------------------------------------
# PNG export — for channels that cannot open HTML (Feishu, WeChat, ...)
# ---------------------------------------------------------------------------

# Wait until every ECharts instance on the page has finished drawing, with
# animation off so the screenshot never catches a half-drawn frame.
_WAIT_CHARTS_JS = """async () => {
  if (!window.echarts) return {instances: 0, timedOut: 0};
  const doms = [...document.querySelectorAll('[_echarts_instance_]')];
  const insts = doms.map(d => echarts.getInstanceByDom(d)).filter(Boolean);
  let timedOut = 0;
  await Promise.all(insts.map(i => new Promise(res => {
    const t = setTimeout(() => { timedOut += 1; res(); }, 5000);
    i.on('finished', () => { clearTimeout(t); res(); });
    i.setOption({animation: false});
    i.resize();
  })));
  return {instances: insts.length, timedOut};
}"""


def export_chart_png(html_path: str, png_path: str) -> Dict[str, Any]:
    """Screenshot a chart/dashboard HTML file to PNG. Never raises.

    Returns ``{"ok": True, "path": ...}`` or ``{"ok": False, "reason": ...}``.
    The provenance SQL block is hidden: it is useful in the web UI but is
    noise in an image sent to a chat.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as exc:
        return {"ok": False, "reason": f"playwright not installed: {exc}"}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(
                    viewport={"width": 1100, "height": 700}, device_scale_factor=2
                )
                page.goto(Path(html_path).resolve().as_uri(), wait_until="load", timeout=30000)
                page.add_style_tag(content=".sql-block{display:none !important}")
                state = page.evaluate(_WAIT_CHARTS_JS)
                target = page.query_selector(".wrap")
                if target:
                    target.screenshot(path=png_path)
                else:
                    page.screenshot(path=png_path, full_page=True)
            finally:
                browser.close()
    except Exception as exc:
        return {"ok": False, "reason": f"png export failed: {exc}"}

    out = Path(png_path)
    if not out.is_file() or out.stat().st_size < 2000:
        return {"ok": False, "reason": "png export produced no usable image"}
    result: Dict[str, Any] = {"ok": True, "path": str(out)}
    if state.get("timedOut"):
        result["warning"] = f"{state['timedOut']} chart(s) did not report finished rendering"
    return result
