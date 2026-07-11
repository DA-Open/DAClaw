"""Direct Excel (.xlsx) manipulation — the DV-Sheet task class.

Aligns with DV-World's **DV-Sheet** domain: native spreadsheet manipulation
including chart creation, dashboard assembly, and diagnostic repair. Unlike the
HTML/ECharts path (which renders in a browser), this produces a real ``.xlsx``
workbook with *native Excel charts* the user can open and edit in Excel.

Backed by ``openpyxl``. Data is sourced from a real DuckDB query result
(:class:`~agent.data_agent.artifacts.ResultTable`) so numbers stay grounded.

Capabilities:
- ``write_table``      — write a result table to a sheet with a styled header.
- ``add_chart``        — add a native Excel chart (bar/line/pie) over a range.
- ``build_dashboard``  — assemble a multi-panel dashboard workbook from queries.
- ``repair_workbook``  — diagnostic repair pass over an existing workbook.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.data_agent.artifacts import ResultTable
from agent.data_agent import theme

# openpyxl imported lazily inside functions so importing this module is cheap.

_HEADER_FILL = "2DD4BF"  # DAClaw teal
_HEADER_FONT = "FFFFFF"


def _col_letter(idx: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(idx)


def _style_header(ws, ncols: int, row: int = 1) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill

    fill = PatternFill(start_color=_HEADER_FILL, end_color=_HEADER_FILL, fill_type="solid")
    font = Font(color=_HEADER_FONT, bold=True)
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="center", vertical="center")


def _autosize(ws, ncols: int) -> None:
    for c in range(1, ncols + 1):
        letter = _col_letter(c)
        maxlen = 0
        for cell in ws[letter]:
            v = cell.value
            if v is not None:
                maxlen = max(maxlen, len(str(v)))
        ws.column_dimensions[letter].width = min(max(maxlen + 2, 10), 40)


def _write_result(ws, result: ResultTable, *, start_row: int = 1) -> Dict[str, int]:
    """Write a ResultTable to a worksheet. Returns the occupied range info."""
    cols = result.column_names
    for j, name in enumerate(cols, start=1):
        ws.cell(row=start_row, column=j, value=name)
    for i, row in enumerate(result.rows, start=start_row + 1):
        for j, val in enumerate(row, start=1):
            ws.cell(row=i, column=j, value=val)
    _style_header(ws, len(cols), row=start_row)
    _autosize(ws, len(cols))
    return {
        "header_row": start_row,
        "first_data_row": start_row + 1,
        "last_row": start_row + len(result.rows),
        "ncols": len(cols),
    }


def _pick_chart(chart_type: str):
    from openpyxl.chart import BarChart, LineChart, PieChart

    ct = (chart_type or "bar").lower()
    if ct in ("line", "area"):
        c = LineChart()
    elif ct == "pie":
        c = PieChart()
    else:
        c = BarChart()
        c.type = "col"
    return c


def _add_chart_to_ws(
    ws,
    span: Dict[str, int],
    *,
    chart_type: str,
    title: str,
    cat_col: int = 1,
    val_col: Optional[int] = None,
    anchor: str = "H2",
):
    """Add a native chart referencing the data already written to *ws*."""
    from openpyxl.chart import Reference

    chart = _pick_chart(chart_type)
    chart.title = title
    chart.height = 8
    chart.width = 15
    val_col = val_col or (2 if span["ncols"] >= 2 else 1)

    data = Reference(
        ws,
        min_col=val_col,
        min_row=span["header_row"],
        max_row=span["last_row"],
    )
    cats = Reference(
        ws,
        min_col=cat_col,
        min_row=span["first_data_row"],
        max_row=span["last_row"],
    )
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(cats)
    ws.add_chart(chart, anchor)
    return chart


# ---------------------------------------------------------------------------
# public operations
# ---------------------------------------------------------------------------


def write_table(result: ResultTable, out_path: str, *, sheet_name: str = "Data") -> Dict[str, Any]:
    """Write a single result table to a styled worksheet."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name[:31]
    span = _write_result(ws, result)
    wb.save(out_path)
    return {"path": out_path, "rows": result.row_count, "sheet": ws.title, "span": span}


def add_chart(
    result: ResultTable,
    out_path: str,
    *,
    chart_type: str = "bar",
    title: str = "Chart",
    sheet_name: str = "Chart",
) -> Dict[str, Any]:
    """Write data + a native Excel chart into one sheet."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name[:31]
    span = _write_result(ws, result)
    anchor = f"{_col_letter(span['ncols'] + 2)}2"
    _add_chart_to_ws(ws, span, chart_type=chart_type, title=title, anchor=anchor)
    wb.save(out_path)
    return {
        "path": out_path,
        "chart_type": chart_type,
        "rows": result.row_count,
        "sheet": ws.title,
    }


def build_dashboard(
    panels: List[Dict[str, Any]], out_path: str, *, title: str = "Dashboard"
) -> Dict[str, Any]:
    """Assemble a multi-panel Excel dashboard.

    Each panel: {"title": str, "result": ResultTable, "chart_type": str}.
    Layout: an Overview sheet with KPIs + one sheet per chart panel.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    overview = wb.active
    overview.title = "Overview"
    overview["A1"] = title
    overview["A1"].font = Font(size=16, bold=True, color=_HEADER_FILL)

    r = 3
    chart_panels = [p for p in panels if p.get("result") is not None]
    kpi_panels = [p for p in panels if p.get("kpi") is not None]

    for p in kpi_panels:
        overview.cell(row=r, column=1, value=p["title"]).font = Font(bold=True)
        overview.cell(row=r, column=2, value=p["kpi"])
        r += 1

    sheets_made = 0
    for idx, p in enumerate(chart_panels):
        result: ResultTable = p["result"]
        name = (p.get("title") or f"Panel{idx+1}")[:31]
        # openpyxl rejects some chars in sheet titles
        for ch in "[]:*?/\\":
            name = name.replace(ch, " ")
        ws = wb.create_sheet(title=name.strip()[:31] or f"Panel{idx+1}")
        span = _write_result(ws, result)
        anchor = f"{_col_letter(span['ncols'] + 2)}2"
        _add_chart_to_ws(
            ws, span, chart_type=p.get("chart_type", "bar"), title=p.get("title", name), anchor=anchor
        )
        sheets_made += 1

    wb.save(out_path)
    return {
        "path": out_path,
        "sheets": sheets_made + 1,
        "kpis": len(kpi_panels),
        "charts": sheets_made,
        "title": title,
    }


def add_sheet_to_workbook(
    src_path: str,
    out_path: str,
    result: "ResultTable",
    *,
    sheet_name: str = "Analysis",
    chart_type: Optional[str] = None,
    chart_title: str = "",
) -> Dict[str, Any]:
    """Open an existing workbook, append a new sheet from a query result
    (styled table + optional native chart), and save to out_path.

    This is the in-place-edit path: the user's uploaded workbook is preserved
    and a derived sheet is added alongside the original sheets.
    """
    from openpyxl import load_workbook

    wb = load_workbook(src_path)
    # unique sheet name
    base = (sheet_name or "Analysis")[:28]
    for ch in "[]:*?/\\":
        base = base.replace(ch, " ")
    base = base.strip() or "Analysis"
    name, i = base, 2
    while name in wb.sheetnames:
        name = f"{base}_{i}"[:31]
        i += 1
    ws = wb.create_sheet(title=name)
    span = _write_result(ws, result)
    if chart_type:
        anchor = f"{_col_letter(span['ncols'] + 2)}2"
        _add_chart_to_ws(ws, span, chart_type=chart_type, title=chart_title or name, anchor=anchor)
    wb.save(out_path)
    return {
        "path": out_path,
        "added_sheet": name,
        "rows": result.row_count,
        "chart_type": chart_type,
        "all_sheets": wb.sheetnames,
    }


def inspect_workbook(path: str) -> Dict[str, Any]:
    """Diagnostic pass over an existing workbook (for the repair scenario)."""
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=False)
    report: Dict[str, Any] = {"path": path, "sheets": [], "issues": []}
    for ws in wb.worksheets:
        info = {
            "name": ws.title,
            "dims": ws.dimensions,
            "max_row": ws.max_row,
            "max_col": ws.max_column,
            "charts": len(getattr(ws, "_charts", [])),
        }
        report["sheets"].append(info)
        # common issues: empty sheet, no header styling, formula errors
        if ws.max_row <= 1:
            report["issues"].append(f"{ws.title}: sheet has no data rows")
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("#") and cell.value.endswith("!"):
                    report["issues"].append(f"{ws.title}!{cell.coordinate}: formula error {cell.value}")
    return report


def repair_workbook(path: str, out_path: str) -> Dict[str, Any]:
    """Apply simple diagnostic repairs: restyle headers, autosize, flag errors.

    A light, deterministic repair pass (the LLM can drive richer repairs later).
    """
    from openpyxl import load_workbook

    wb = load_workbook(path)
    fixed: List[str] = []
    for ws in wb.worksheets:
        if ws.max_row >= 1 and ws.max_column >= 1:
            _style_header(ws, ws.max_column, row=1)
            _autosize(ws, ws.max_column)
            fixed.append(f"{ws.title}: restyled header + autosized {ws.max_column} cols")
    wb.save(out_path)
    return {"path": out_path, "repairs": fixed, "n_repairs": len(fixed)}
