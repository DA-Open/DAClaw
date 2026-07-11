"""Data synthesis engine for the Research → Data Synthesis workbench.

Turns a connected database / a SQL query / a natural-language spec into
synthetic datasets, for two research purposes:

  1. LLM training data  — from a real schema (or a SQL result), generate
     instruction-tuning rows in a chosen style:
        - text2sql   : {question, sql, schema}         (NL → SQL pairs)
        - qa         : {question, answer}              (grounded Q&A over the data)
        - instruction: {instruction, input, output}   (generic SFT triples)
  2. Research tables    — from a plain-English spec, fabricate an arbitrary-domain
     table (columns + N believable rows) for papers / experiments.

Every pipeline tries the LLM (``llm.chat_json``) and falls back to a
deterministic generator (stdlib ``random``, seeded) so it works offline. Output
is written to the caller-provided directory in one or more formats (jsonl / csv /
xlsx / parquet) and a manifest of written files is returned.
"""

from __future__ import annotations

import csv
import json
import random
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.data_agent.engine import DataEngine, _semantic_type
from agent.data_agent.llm import get_llm, llm_available


class SynthError(RuntimeError):
    pass


TRAINING_STYLES = ("text2sql", "qa", "instruction")
FORMATS = ("jsonl", "csv", "xlsx", "parquet")


# ---------------------------------------------------------------------------
# schema brief
# ---------------------------------------------------------------------------

def _schema_brief(engine: DataEngine, tables: Optional[List[str]] = None) -> str:
    names = tables or engine.list_tables()
    lines: List[str] = []
    for t in names:
        try:
            cols = engine.table_schema(t)
        except Exception:
            continue
        col_str = ", ".join(f"{c['name']} {c['type']}" for c in cols)
        lines.append(f"- {t}({col_str})")
    return "\n".join(lines) if lines else "(no tables)"


# ---------------------------------------------------------------------------
# writers (jsonl / csv / xlsx / parquet)
# ---------------------------------------------------------------------------

def _write_jsonl(rows: List[Dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_csv(rows: List[Dict[str, Any]], path: Path) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    # union of keys, stable order from first row then extras
    keys: List[str] = list(rows[0].keys())
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: _flat(r.get(k)) for k in keys})


def _flat(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


def _write_xlsx(rows: List[Dict[str, Any]], path: Path) -> bool:
    try:
        import pandas as pd
    except Exception:
        return False
    df = pd.DataFrame([{k: _flat(v) for k, v in r.items()} for r in rows])
    df.to_excel(path, index=False)
    return True


def _write_parquet(rows: List[Dict[str, Any]], path: Path) -> bool:
    try:
        import pandas as pd
    except Exception:
        return False
    try:
        pd.DataFrame([{k: _flat(v) for k, v in r.items()} for r in rows]).to_parquet(path, index=False)
        return True
    except Exception:
        return False


def _write_all(rows: List[Dict[str, Any]], out_dir: Path, base: str, formats: List[str]) -> List[Dict[str, Any]]:
    """Write rows to each requested format; return [{format, file, n_rows}]."""
    written: List[Dict[str, Any]] = []
    for fmt in formats:
        fmt = fmt.lower()
        path = out_dir / f"{base}.{ 'jsonl' if fmt=='jsonl' else fmt }"
        ok = True
        if fmt == "jsonl":
            _write_jsonl(rows, path)
        elif fmt == "csv":
            _write_csv(rows, path)
        elif fmt == "xlsx":
            ok = _write_xlsx(rows, path)
        elif fmt == "parquet":
            ok = _write_parquet(rows, path)
        else:
            continue
        if ok and path.exists():
            written.append({"format": fmt, "file": path.name, "n_rows": len(rows)})
    return written


# ---------------------------------------------------------------------------
# pipeline 1 — LLM training data from a schema / SQL
# ---------------------------------------------------------------------------

_TRAIN_SYS = {
    "text2sql": (
        "You generate high-quality Text-to-SQL training examples. Given a database "
        "schema, produce diverse, realistic natural-language questions paired with "
        "correct DuckDB SQL that answers them over the given tables. Vary difficulty "
        "(filters, joins, aggregation, group-by, window). Return ONLY JSON: "
        '{"rows": [{"question": "...", "sql": "...", "difficulty": "easy|medium|hard"}]}'
    ),
    "qa": (
        "You generate grounded question-answer training pairs about a dataset. Given a "
        "schema and sample facts, write natural analytical questions and concise, "
        "correct answers. Return ONLY JSON: "
        '{"rows": [{"question": "...", "answer": "..."}]}'
    ),
    "instruction": (
        "You generate instruction-tuning examples for a data-analyst assistant. Given a "
        "schema, produce {instruction, input, output} triples covering analysis tasks. "
        'Return ONLY JSON: {"rows": [{"instruction": "...", "input": "...", "output": "..."}]}'
    ),
}


def _sample_facts(engine: DataEngine, tables: List[str], per_table: int = 3) -> str:
    facts: List[str] = []
    for t in tables[:5]:
        try:
            rt = engine.run_query(f'SELECT * FROM "{t}" LIMIT {per_table}')
            for rec in rt.to_records()[:per_table]:
                facts.append(f"{t}: {json.dumps(rec, ensure_ascii=False)[:300]}")
        except Exception:
            continue
    return "\n".join(facts)


def synth_training_data(
    engine: DataEngine,
    *,
    style: str = "text2sql",
    count: int = 20,
    tables: Optional[List[str]] = None,
    sql: Optional[str] = None,
    extra: str = "",
    config: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Generate `count` LLM-training rows in the given style."""
    style = style if style in TRAINING_STYLES else "text2sql"
    schema = _schema_brief(engine, tables)
    if schema == "(no tables)" and not sql:
        raise SynthError("Connect a database (or provide SQL) before synthesizing training data.")

    rows: List[Dict[str, Any]] = []
    if llm_available():
        try:
            llm = get_llm(config)
            facts = _sample_facts(engine, tables or engine.list_tables())
            sql_ctx = f"\nA reference query to build variations around:\n{sql}\n" if sql else ""
            user = (
                f"SCHEMA:\n{schema}\n\nSAMPLE FACTS:\n{facts}\n{sql_ctx}\n"
                f"Generate {count} diverse {style} training rows."
                + (f"\nExtra guidance: {extra}" if extra else "")
            )
            # batch to keep responses parseable
            remaining = count
            while remaining > 0 and len(rows) < count:
                batch = min(remaining, 15)
                data = llm.chat_json(
                    [{"role": "system", "content": _TRAIN_SYS[style]},
                     {"role": "user", "content": user + f"\n(Return exactly {batch} rows.)"}],
                    max_output_tokens=4000,
                )
                got = data.get("rows") or []
                if not got:
                    break
                rows.extend(got)
                remaining -= len(got)
        except Exception:
            rows = []

    if len(rows) < count:
        rows.extend(_fallback_training(engine, style, count - len(rows), tables))
    return rows[:count]


def _fallback_training(engine: DataEngine, style: str, count: int, tables: Optional[List[str]]) -> List[Dict[str, Any]]:
    """Deterministic training rows built from the schema (no LLM)."""
    names = tables or engine.list_tables()
    rng = random.Random(20260711)
    out: List[Dict[str, Any]] = []
    templates: List[tuple] = []
    for t in names:
        try:
            schema = engine.table_schema(t)
        except Exception:
            continue
        roles: Dict[str, list] = {"time": [], "measure": [], "category": [], "id": []}
        for c in schema:
            roles.setdefault(_semantic_type(c["name"], str(c["type"]).lower()), []).append(c["name"])
        m = roles["measure"][0] if roles["measure"] else None
        cat = roles["category"][0] if roles["category"] else None
        tm = roles["time"][0] if roles["time"] else None
        if m and cat:
            templates.append((f'What is the total {m} by {cat} in {t}?',
                              f'SELECT "{cat}", SUM("{m}") AS total_{m} FROM "{t}" GROUP BY "{cat}" ORDER BY total_{m} DESC'))
            templates.append((f'Top 5 {cat} by {m} in {t}',
                              f'SELECT "{cat}", SUM("{m}") AS total FROM "{t}" GROUP BY "{cat}" ORDER BY total DESC LIMIT 5'))
        if m and tm:
            templates.append((f'How does {m} change over {tm} in {t}?',
                              f'SELECT "{tm}", SUM("{m}") AS total FROM "{t}" GROUP BY "{tm}" ORDER BY "{tm}"'))
        if cat:
            templates.append((f'How many rows per {cat} in {t}?',
                              f'SELECT "{cat}", COUNT(*) AS n FROM "{t}" GROUP BY "{cat}" ORDER BY n DESC'))
        templates.append((f'How many rows are in {t}?', f'SELECT COUNT(*) AS n FROM "{t}"'))
    if not templates:
        templates = [("List the available tables.", "SHOW TABLES")]
    i = 0
    while len(out) < count:
        q, sql = templates[i % len(templates)]
        suffix = "" if i < len(templates) else f" (variant {i // len(templates) + 1})"
        if style == "text2sql":
            out.append({"question": q + suffix, "sql": sql, "difficulty": rng.choice(["easy", "medium", "hard"])})
        elif style == "qa":
            out.append({"question": q + suffix, "answer": f"Run: {sql}"})
        else:
            out.append({"instruction": q + suffix, "input": "", "output": f"Use SQL: {sql}"})
        i += 1
    return out[:count]


# ---------------------------------------------------------------------------
# pipeline 2 — research-style table from a natural-language spec
# ---------------------------------------------------------------------------

_TABLE_SYS = (
    "You fabricate realistic synthetic TABLES for research and experiments. Given a "
    "plain-English description of the desired table (domain, columns, style), design "
    "sensible columns and generate believable, internally-consistent rows. Values must "
    "look real (names, dates ISO, plausible numeric ranges, valid categories). Return "
    'ONLY JSON: {"columns": ["col1", ...], "rows": [{"col1": ..., ...}]}'
)


def synth_research_table(
    spec: str,
    *,
    count: int = 50,
    extra: str = "",
    config: Optional[Any] = None,
) -> Dict[str, Any]:
    """Fabricate an arbitrary-domain research table from a description."""
    spec = (spec or "").strip()
    if not spec:
        raise SynthError("Describe the table you want (domain, columns, style).")
    columns: List[str] = []
    rows: List[Dict[str, Any]] = []
    if llm_available():
        try:
            llm = get_llm(config)
            remaining = count
            while remaining > 0 and len(rows) < count:
                batch = min(remaining, 40)
                user = (
                    f"TABLE SPEC:\n{spec}\n" + (f"Extra: {extra}\n" if extra else "") +
                    f"\nGenerate exactly {batch} rows."
                    + (f"\nUse EXACTLY these columns: {columns}" if columns else "")
                )
                data = llm.chat_json(
                    [{"role": "system", "content": _TABLE_SYS}, {"role": "user", "content": user}],
                    max_output_tokens=4000,
                )
                if not columns:
                    columns = [str(c) for c in (data.get("columns") or [])]
                got = data.get("rows") or []
                if not got:
                    break
                rows.extend(got)
                remaining -= len(got)
        except Exception:
            rows = []
    if not rows:
        columns, rows = _fallback_table(spec, count)
        return {"columns": columns, "rows": rows, "source": "heuristic"}
    if not columns and rows:
        columns = list(rows[0].keys())
    return {"columns": columns, "rows": rows[:count], "source": "llm"}


def _fallback_table(spec: str, count: int) -> tuple:
    """A generic deterministic table when no LLM is available."""
    rng = random.Random(abs(hash(spec)) % (2**31))
    columns = ["id", "name", "category", "value", "date"]
    cats = ["A", "B", "C", "D"]
    first = ["Alex", "Sam", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie"]
    last = ["Lee", "Chen", "Patel", "Garcia", "Kim", "Brown", "Wang", "Diaz"]
    rows: List[Dict[str, Any]] = []
    for i in range(count):
        rows.append({
            "id": i + 1,
            "name": f"{rng.choice(first)} {rng.choice(last)}",
            "category": rng.choice(cats),
            "value": round(rng.uniform(1, 1000), 2),
            "date": f"2026-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}",
        })
    return columns, rows


# ---------------------------------------------------------------------------
# top-level orchestrator
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (text or "synth").strip().lower()).strip("_")
    return (s or "synth")[:40]


def run_synthesis(
    engine: DataEngine,
    payload: Dict[str, Any],
    out_dir: Path,
    *,
    stamp: str = "0",
) -> Dict[str, Any]:
    """Dispatch a synthesis request and write files. Returns a result manifest."""
    mode = str(payload.get("mode") or "training")
    count = max(1, min(int(payload.get("count") or 20), 2000))
    formats = [f for f in (payload.get("formats") or ["jsonl"]) if f in FORMATS] or ["jsonl"]

    if mode == "table":
        spec = str(payload.get("spec") or "")
        res = synth_research_table(spec, count=count, extra=str(payload.get("extra") or ""))
        rows = res["rows"]
        base = f"table_{_slug(spec)}_{stamp}"
        files = _write_all(rows, out_dir, base, formats)
        return {
            "ok": True, "mode": "table", "source": res.get("source"),
            "columns": res.get("columns") or (list(rows[0].keys()) if rows else []),
            "n_rows": len(rows), "preview": rows[:20], "files": files,
        }

    # default: training-data mode
    style = str(payload.get("style") or "text2sql")
    tables = payload.get("tables") or None
    sql = payload.get("sql") or None
    rows = synth_training_data(
        engine, style=style, count=count, tables=tables, sql=sql,
        extra=str(payload.get("extra") or ""),
    )
    base = f"train_{style}_{stamp}"
    files = _write_all(rows, out_dir, base, formats)
    return {
        "ok": True, "mode": "training", "style": style,
        "columns": list(rows[0].keys()) if rows else [],
        "n_rows": len(rows), "preview": rows[:20], "files": files,
    }
