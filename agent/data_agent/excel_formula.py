"""Excel formula engine — offline recalculation, error auditing, and building
computable workbooks.

Backed by two libraries:
  - ``openpyxl`` — read/write cells, formulas, styling, charts (no evaluation).
  - ``formulas`` — a pure-Python Excel calculation engine that evaluates
    formulas offline and surfaces error values (#DIV/0!, #REF!, #NUM!, ...).

This is the load-bearing piece for the "formula construction / build a
computable model" and "formula-error repair" capabilities: after writing or
editing a workbook we recalc it to VERIFY the numbers are real (not just
formula strings), and to detect residual errors.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
from typing import Any, Dict, List, Optional, Tuple

_ERROR_TOKENS = ("#REF!", "#DIV/0!", "#NUM!", "#VALUE!", "#NAME?", "#N/A", "#NULL!", "#SPILL!")


@contextlib.contextmanager
def _quiet():
    """Silence the tqdm progress bars + stray prints from the formulas lib."""
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        # formulas checks this env var to disable its progress bars
        prev = os.environ.get("DISABLE_TQDM")
        os.environ["DISABLE_TQDM"] = "1"
        try:
            yield
        finally:
            if prev is None:
                os.environ.pop("DISABLE_TQDM", None)
            else:
                os.environ["DISABLE_TQDM"] = prev


def _cell_ref(key: str) -> Tuple[str, str]:
    """Split a formulas solution key like "'[wb.xlsx]Sheet1'!B3" into (sheet, cell)."""
    m = re.search(r"\]([^'!]+)'?!([A-Z]+\d+)$", key)
    if m:
        return m.group(1), m.group(2)
    # fallback: last !CELL
    if "!" in key:
        left, cell = key.rsplit("!", 1)
        sheet = left.split("]")[-1].strip("'")
        return sheet, cell
    return "", key


def _scalar(value: Any) -> Any:
    """Unwrap the 1x1 numpy arrays formulas returns into a plain scalar."""
    try:
        # numpy arrays / ranges
        v = value
        for _ in range(3):
            if hasattr(v, "tolist"):
                v = v.tolist()
            if isinstance(v, list) and len(v) == 1:
                v = v[0]
            else:
                break
        if isinstance(v, list) and len(v) == 1:
            v = v[0]
        return v
    except Exception:
        return value


def recalc(path: str) -> Dict[str, Any]:
    """Recalculate every formula in the workbook offline.

    Returns {"cells": {"Sheet!Cell": value, ...}, "errors": [{cell, error}]}.
    """
    import formulas

    cells: Dict[str, Any] = {}
    errors: List[Dict[str, str]] = []
    with _quiet():
        xl = formulas.ExcelModel().loads(path).finish()
        sol = xl.calculate()
    for key, raw in sol.items():
        val = _scalar(getattr(raw, "value", raw))
        sheet, cell = _cell_ref(key)
        ref = f"{sheet}!{cell}" if sheet else cell
        cells[ref] = val
        s = str(val)
        if any(tok in s for tok in _ERROR_TOKENS):
            errors.append({"cell": ref, "error": s.strip()})
    return {"cells": cells, "errors": errors}


def audit(path: str) -> Dict[str, Any]:
    """Static + dynamic audit of a workbook's formulas.

    Combines:
      - dynamic recalc errors (#DIV/0! etc.)
      - duplicate-formula detection (same formula pattern repeated — a smell)
      - hardcoded-number-in-formula detection (magic numbers embedded in a
        formula instead of referencing a cell)
    """
    from openpyxl import load_workbook

    report: Dict[str, Any] = {
        "path": path,
        "errors": [],
        "hardcoded": [],
        "duplicate_groups": [],
        "formula_count": 0,
        "sheets": [],
    }

    # dynamic errors
    try:
        rc = recalc(path)
        report["errors"] = rc["errors"]
    except Exception as exc:  # pragma: no cover
        report["recalc_error"] = str(exc)

    wb = load_workbook(path, data_only=False)
    # magic numbers: a number literal inside a formula that isn't part of a cell
    # ref, not 0/1/-1/100, and not a small step like +1. Heuristic.
    num_re = re.compile(r"(?<![A-Za-z0-9_$:.])\d+(?:\.\d+)?")
    norm_map: Dict[str, List[str]] = {}
    for ws in wb.worksheets:
        sheet_formulas = 0
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if isinstance(v, str) and v.startswith("="):
                    sheet_formulas += 1
                    ref = f"{ws.title}!{cell.coordinate}"
                    # hardcoded numbers
                    body = v[1:]
                    # strip cell refs so we don't flag row/col numbers
                    stripped = re.sub(r"\$?[A-Z]{1,3}\$?\d+", "", body)
                    for m in num_re.finditer(stripped):
                        token = m.group(0)
                        if token not in {"0", "1", "100"} and float(token) not in {0.0, 1.0, 100.0}:
                            report["hardcoded"].append({"cell": ref, "formula": v, "value": token})
                            break
                    # normalized formula for duplicate grouping (refs -> R)
                    norm = re.sub(r"\$?[A-Z]{1,3}\$?\d+", "R", body)
                    norm_map.setdefault(norm, []).append(ref)
        report["sheets"].append({"name": ws.title, "formula_count": sheet_formulas})
        report["formula_count"] += sheet_formulas

    for norm, refs in norm_map.items():
        if len(refs) >= 5:  # same shape repeated a lot — often fine, but report top ones
            report["duplicate_groups"].append({"pattern": norm, "count": len(refs), "cells": refs[:8]})
    report["duplicate_groups"].sort(key=lambda g: -g["count"])
    report["duplicate_groups"] = report["duplicate_groups"][:5]
    return report


def apply_fixes(src_path: str, out_path: str, fixes: List[Dict[str, str]]) -> Dict[str, Any]:
    """Apply cell-level formula/value fixes and recalc to verify.

    Each fix: {"cell": "Sheet!B3", "formula": "=B4/B1"}  (or "value": 123).
    Returns {applied, errors_before, errors_after, out_path}.
    """
    from openpyxl import load_workbook

    before = []
    try:
        before = recalc(src_path)["errors"]
    except Exception:
        pass

    wb = load_workbook(src_path)
    applied = []
    for fx in fixes:
        ref = fx.get("cell", "")
        if "!" in ref:
            sheet, coord = ref.split("!", 1)
        else:
            sheet, coord = wb.sheetnames[0], ref
        if sheet not in wb.sheetnames:
            continue
        ws = wb[sheet]
        if "formula" in fx:
            ws[coord] = fx["formula"]
        elif "value" in fx:
            ws[coord] = fx["value"]
        applied.append(ref)
    wb.save(out_path)

    after = []
    try:
        after = recalc(out_path)["errors"]
    except Exception as exc:  # pragma: no cover
        after = [{"cell": "?", "error": f"recalc failed: {exc}"}]

    return {
        "out_path": out_path,
        "applied": applied,
        "errors_before": before,
        "errors_after": after,
        "fixed_count": max(0, len(before) - len(after)),
    }


def write_model(
    out_path: str,
    sheets: List[Dict[str, Any]],
    *,
    title: str = "Model",
) -> Dict[str, Any]:
    """Write a computable workbook from a structured spec, then recalc to verify.

    Each sheet: {"name": str, "cells": [{"cell": "B3", "formula"/"value": ...,
                 "bold"?: bool, "fmt"?: "#,##0"}], "widths"?: {"A": 22}}.
    Returns recalc results for the key cells so the caller can prove the model
    computes (numbers are real, no residual errors).
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment

    wb = Workbook()
    wb.remove(wb.active)
    for spec in sheets:
        ws = wb.create_sheet(title=(spec.get("name") or "Sheet")[:31])
        for c in spec.get("cells", []):
            coord = c["cell"]
            if "formula" in c:
                ws[coord] = c["formula"]
            elif "value" in c:
                ws[coord] = c["value"]
            cell = ws[coord]
            if c.get("bold"):
                cell.font = Font(bold=True)
            if c.get("fmt"):
                cell.number_format = c["fmt"]
            if c.get("align"):
                cell.alignment = Alignment(horizontal=c["align"])
        for col, w in (spec.get("widths") or {}).items():
            ws.column_dimensions[col].width = w
    wb.save(out_path)

    result = {"out_path": out_path, "sheets": [s.get("name") for s in sheets], "title": title}

    # Guardrail: flag sheets that look like calculation areas but contain
    # pasted numbers instead of formulas (e.g. a sensitivity grid hardcoded).
    warnings_out: List[str] = []
    _CALC_HINTS = ("sensitiv", "scenario", "敏感", "情景", "projection", "预测", "model", "模型")
    for spec in sheets:
        name = spec.get("name") or "Sheet"
        cells = spec.get("cells", [])
        n_formula = sum(1 for c in cells if "formula" in c)
        # numeric literal cells that are NOT obviously a header/label/year index
        n_numlit = 0
        for c in cells:
            if "formula" in c:
                continue
            v = c.get("value")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                n_numlit += 1
        looks_calc = any(h in name.lower() for h in _CALC_HINTS)
        # a calc sheet with lots of number literals and few formulas is suspicious
        if looks_calc and n_numlit >= 6 and n_formula <= max(1, n_numlit // 4):
            warnings_out.append(
                f"Sheet '{name}' looks like a calculation/scenario area but has "
                f"{n_formula} formula(s) vs {n_numlit} numeric literal(s) — its "
                f"computed cells appear hardcoded. Rebuild the body cells as "
                f"formulas that reference assumption cells so the model stays live."
            )
    if warnings_out:
        result["hardcoding_warnings"] = warnings_out

    try:
        rc = recalc(out_path)
        result["errors"] = rc["errors"]
        result["computed_ok"] = len(rc["errors"]) == 0
        result["cell_count"] = len(rc["cells"])
    except Exception as exc:  # pragma: no cover
        result["errors"] = [{"cell": "?", "error": str(exc)}]
        result["computed_ok"] = False
    return result
