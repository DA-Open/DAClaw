"""
GDPval rubric grader.

GDPval ships a per-task rubric (rubric_json) -- a list of items like::

    {"score": 2,
     "criterion": "The workbook contains a worksheet named exactly 'Sample Size Calculation'.",
     "required": null,
     "rubric_item_id": "4c79e9e6-...",
     "tags": ["true"]}

Items carry 1/2/5 points each (mean ~47 items per task, up to 137). Scoring is
item-wise: an item is either satisfied (award its points) or not. The official
protocol has a grader read the deliverable against each criterion, so this
module does two things:

1. rule_check() -- resolve the mechanically decidable items (basename,
   extension, worksheet name) without an LLM. Cheap, deterministic, no drift.
2. judge_items() -- send the remaining criteria plus a text rendering of the
   deliverable to an OpenAI-compatible endpoint and get back a JSON verdict.

Anything a rule cannot decide unambiguously is handed to the judge, so the
rule layer can only *skip* work -- never guess.
"""

from __future__ import annotations

import asyncio
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# ---------------------------------------------------------------------------
# Rubric model
# ---------------------------------------------------------------------------

@dataclass
class RubricItem:
    item_id: str
    criterion: str
    points: float
    required: bool = False
    tags: List[str] = field(default_factory=list)


def load_rubric(rubric_json: Any) -> List[RubricItem]:
    """Normalise the parquet field (JSON string or already-parsed list)."""
    if isinstance(rubric_json, str):
        data = json.loads(rubric_json)
    elif rubric_json is None:
        return []
    else:
        data = list(rubric_json)

    items: List[RubricItem] = []
    for i, raw in enumerate(data):
        if not isinstance(raw, dict):
            continue
        try:
            points = float(raw.get("score") or 0)
        except (TypeError, ValueError):
            points = 0.0
        if points <= 0:
            continue
        items.append(
            RubricItem(
                item_id=str(raw.get("rubric_item_id") or "item-%d" % i),
                criterion=str(raw.get("criterion") or "").strip(),
                points=points,
                required=bool(raw.get("required")),
                tags=[str(t) for t in (raw.get("tags") or [])],
            )
        )
    return items


# ---------------------------------------------------------------------------
# Deliverable collection + text extraction
# ---------------------------------------------------------------------------

TEXT_EXTS = {".txt", ".md", ".csv", ".tsv", ".json", ".py", ".sql", ".ipynb", ".yaml", ".yml"}
OPAQUE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov", ".wav", ".mp3", ".zip", ".step"}

MAX_FILE_CHARS = 20000
MAX_PDF_PAGES = 40


def collect_deliverables(workspace: Path, known: Iterable[str]) -> List[Path]:
    """Files the agent added to the workspace, excluding the seeded references."""
    known_set = set(known)
    skip_dirs = {"__pycache__", ".git", ".ipynb_checkpoints", "node_modules", ".venv", "venv"}
    out: List[Path] = []
    for p in sorted(workspace.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(workspace).as_posix()
        if rel in known_set or p.name.startswith("."):
            continue
        if any(part in skip_dirs for part in p.relative_to(workspace).parts[:-1]):
            continue
        out.append(p)
    return out


def _xlsx_text(path: Path, limit: int) -> str:
    try:
        from openpyxl import load_workbook
    except Exception:  # noqa: BLE001
        return ""
    parts: List[str] = []
    try:
        wb = load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        return "[unreadable workbook: %s]" % exc
    try:
        parts.append("worksheets: " + ", ".join(wb.sheetnames))
        for ws in wb.worksheets:
            parts.append("\n## sheet '%s'" % ws.title)
            for row in ws.iter_rows(max_row=80, max_col=25):
                cells = ["" if c.value is None else str(c.value) for c in row]
                if any(cells):
                    parts.append(" | ".join(cells))
            if len("".join(parts)) > limit:
                parts.append("... [truncated]")
                break
    finally:
        try:
            wb.close()
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(parts)[:limit]


def _zip_xml_text(path: Path, member_pattern: str, limit: int) -> str:
    """Pull text out of docx/pptx without python-docx / python-pptx."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = sorted(n for n in zf.namelist() if re.match(member_pattern, n))
            chunks: List[str] = []
            for name in names:
                xml = zf.read(name).decode("utf-8", "ignore")
                xml = re.sub(r"</w:p>|</a:p>|</w:tr>", "\n", xml)
                text = re.sub(r"<[^>]+>", " ", xml)
                text = re.sub(r"[ \t]+", " ", text)
                text = re.sub(r"\n\s*\n+", "\n", text).strip()
                if text:
                    chunks.append("## %s\n%s" % (name, text))
                if sum(len(c) for c in chunks) > limit:
                    break
            return "\n\n".join(chunks)[:limit]
    except Exception as exc:  # noqa: BLE001
        return "[unreadable archive: %s]" % exc


def _pdf_text(path: Path, limit: int) -> str:
    try:
        from pypdf import PdfReader
    except Exception:  # noqa: BLE001
        return ""
    try:
        reader = PdfReader(str(path))
        out = []
        for page in reader.pages[:MAX_PDF_PAGES]:
            out.append(page.extract_text() or "")
            if sum(len(x) for x in out) > limit:
                break
        return "\n".join(out)[:limit]
    except Exception as exc:  # noqa: BLE001
        return "[unreadable pdf: %s]" % exc


def extract_file_text(path: Path, limit: int = MAX_FILE_CHARS) -> str:
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xlsm", ".xls"):
        return _xlsx_text(path, limit)
    if ext == ".docx":
        return _zip_xml_text(path, r"word/(document|header\d*|footer\d*)\.xml", limit)
    if ext == ".pptx":
        return _zip_xml_text(path, r"ppt/(slides/slide|notesSlides/notesSlide)\d*\.xml", limit)
    if ext == ".pdf":
        return _pdf_text(path, limit)
    if ext in TEXT_EXTS:
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:limit]
        except Exception:  # noqa: BLE001
            return ""
    if ext in OPAQUE_EXTS:
        return "[binary %s file, %d bytes -- contents not inlined]" % (ext, path.stat().st_size)
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except Exception:  # noqa: BLE001
        return "[unreadable %s file, %d bytes]" % (ext, path.stat().st_size)


def render_bundle(files: List[Path], max_total_chars: int) -> str:
    """Flatten deliverables into one judge-readable document."""
    if not files:
        return "(NO DELIVERABLE FILES WERE PRODUCED)"
    per_file = max(2000, max_total_chars // max(1, len(files)))
    chunks: List[str] = []
    for p in files:
        body = extract_file_text(p, min(MAX_FILE_CHARS, per_file))
        chunks.append("=== FILE: %s (%d bytes) ===\n%s" % (p.name, p.stat().st_size, body))
        if sum(len(c) for c in chunks) > max_total_chars:
            chunks.append("... [remaining files omitted]")
            break
    return "\n\n".join(chunks)[:max_total_chars]


# ---------------------------------------------------------------------------
# Deterministic rule layer
# ---------------------------------------------------------------------------

_QUOTED = r"['\u2018\u2019\u201c\u201d\"]([^'\u2018\u2019\u201c\u201d\"]{1,120})['\u2018\u2019\u201c\u201d\"]"
_BASENAME_RE = re.compile(r"basename\s+(?:is|of|must be)\s+" + _QUOTED, re.I)
_NAMED_FILE_RE = re.compile(
    r"(?:file|workbook|document|spreadsheet|deliverable)\s+(?:is\s+)?named\s+(?:exactly\s+)?" + _QUOTED, re.I
)
_SHEET_RE = re.compile(r"(?:worksheet|sheet|tab)\s+(?:is\s+)?named\s+(?:exactly\s+)?" + _QUOTED, re.I)
_EXT_RE = re.compile(r"\.(xlsx|xlsm|xls|docx|pdf|pptx|csv|md|ipynb)\b", re.I)


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().strip("'\"")).lower()


def rule_check(item: RubricItem, files: List[Path]) -> Optional[bool]:
    """Resolve an item without an LLM, or return None when not decidable.

    Deliberately narrow: it only fires when the criterion names an exact
    filename / worksheet and the bundle has exactly one candidate type, so it
    can never silently invent a verdict on an ambiguous bundle.
    """
    crit = item.criterion
    if not files:
        return False
    names = {_norm(p.name) for p in files}
    stems = {_norm(p.stem) for p in files}

    m = _BASENAME_RE.search(crit) or _NAMED_FILE_RE.search(crit)
    if m:
        want = _norm(m.group(1))
        if want.endswith((".xlsx", ".xls", ".xlsm", ".docx", ".pdf", ".pptx", ".csv")):
            if want in names:
                return True
            return None  # a differently-named file may still qualify: ask the judge
        if want in names or want in stems:
            return True
        # "Sample" should accept "Sample v2" / "Sample_final", but a heuristic
        # can also over-accept, so only confirm -- never deny -- on a prefix hit.
        if any(s == want or s.startswith(want + " ") or s.startswith(want + "_") for s in stems):
            return True
        return None

    m = _SHEET_RE.search(crit)
    if m:
        want = _norm(m.group(1))
        xlsx = [p for p in files if p.suffix.lower() in (".xlsx", ".xlsm")]
        if len(xlsx) != 1:
            return None  # ambiguous: let the judge look at the workbook
        try:
            from openpyxl import load_workbook

            wb = load_workbook(xlsx[0], read_only=True)
            try:
                return want in {_norm(s) for s in wb.sheetnames}
            finally:
                wb.close()
        except Exception:  # noqa: BLE001
            return None

    m = _EXT_RE.search(crit)
    if m and re.search(r"\b(extension|format|file type)\b", crit, re.I):
        ext = "." + m.group(1).lower()
        return any(p.suffix.lower() == ext for p in files)

    return None


# ---------------------------------------------------------------------------
# LLM judge
# ---------------------------------------------------------------------------

JUDGE_SYSTEM = (
    "You are a meticulous, conservative grader for the GDPval benchmark. "
    "You are given a task's rubric items and a rendering of the deliverable "
    "the agent produced. For EACH rubric item decide only whether it is "
    "satisfied. Be strict: if the deliverable does not clearly satisfy a "
    "criterion, mark it unsatisfied. Never infer content that is not present. "
    "Reply with JSON only."
)

JUDGE_TEMPLATE = """TASK PROMPT (what the agent was asked to do):
{prompt}

DELIVERABLE FILES AND CONTENTS:
{bundle}

RUBRIC ITEMS:
{items}

Return a JSON object exactly in this shape:
{{"verdicts": [{{"id": "<rubric_item_id>", "satisfied": true, "reason": "<max 20 words>"}}]}}

Rules:
- Include one verdict per rubric item, using the exact id given.
- "satisfied" is strictly true or false.
- Judge only against the stated criterion; do not reward unrelated quality.
"""


def _chunks(seq: List[RubricItem], size: int) -> List[List[RubricItem]]:
    return [seq[i:i + size] for i in range(0, len(seq), size)]


FENCE = chr(96) * 3


def _parse_verdicts(raw: str) -> Dict[str, Dict[str, Any]]:
    text = raw.strip()
    if text.startswith(FENCE):
        text = text[len(FENCE):].lstrip()
        if text[:4].lower() == "json":
            text = text[4:]
        text = text.strip()
        if text.endswith(FENCE):
            text = text[:-len(FENCE)].strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in judge reply")
    payload = json.loads(text[start:end + 1])
    out: Dict[str, Dict[str, Any]] = {}
    for v in payload.get("verdicts") or []:
        if isinstance(v, dict) and v.get("id"):
            out[str(v["id"])] = {
                "satisfied": bool(v.get("satisfied")),
                "reason": str(v.get("reason") or "")[:200],
            }
    return out


class JudgeClient:
    """Thin sync wrapper; calls are pushed to a thread from async code."""

    def __init__(self, base_url: str, api_key: str, model: str, temperature: float = 0.0):
        from openai import OpenAI

        self.model = model
        self.temperature = temperature
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=180.0, max_retries=2)

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> str:
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=self.temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""


async def judge_items(
    judge: JudgeClient,
    prompt: str,
    bundle_text: str,
    items: List[RubricItem],
    *,
    batch_size: int = 25,
) -> Dict[str, Dict[str, Any]]:
    """Ask the judge about *items* in batches; failures degrade to unsatisfied."""

    async def _one(batch: List[RubricItem]) -> Dict[str, Dict[str, Any]]:
        listing = "\n".join(
            "- id=%s | worth %g pts | %s" % (it.item_id, it.points, it.criterion) for it in batch
        )
        user = JUDGE_TEMPLATE.format(prompt=prompt[:6000], bundle=bundle_text, items=listing)
        last_err = ""
        for attempt in range(2):
            try:
                raw = await asyncio.to_thread(judge.complete, JUDGE_SYSTEM, user)
                return _parse_verdicts(raw)
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                await asyncio.sleep(1.5 * (attempt + 1))
        return {
            it.item_id: {"satisfied": False, "reason": "judge error: " + last_err[:120]}
            for it in batch
        }

    merged: Dict[str, Dict[str, Any]] = {}
    for batch in _chunks(items, batch_size):
        merged.update(await _one(batch))
    return merged


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

async def grade_task(
    *,
    prompt: str,
    files: List[Path],
    items: List[RubricItem],
    judge: Optional[JudgeClient],
    max_bundle_chars: int = 60000,
    use_rules: bool = True,
    batch_size: int = 25,
) -> Dict[str, Any]:
    """Score one task. Returns weighted score 0-100 plus per-item detail."""
    bundle_text = render_bundle(files, max_bundle_chars)

    verdicts: Dict[str, Dict[str, Any]] = {}
    pending: List[RubricItem] = []
    for it in items:
        rule = rule_check(it, files) if use_rules else None
        if rule is None:
            pending.append(it)
        else:
            verdicts[it.item_id] = {
                "satisfied": rule,
                "reason": "deterministic rule (filename / extension / worksheet name)",
            }

    if pending and judge is not None:
        verdicts.update(
            await judge_items(judge, prompt, bundle_text, pending, batch_size=batch_size)
        )
    else:
        for it in pending:
            verdicts.setdefault(it.item_id, {"satisfied": False, "reason": "no judge configured"})

    detail: List[Dict[str, Any]] = []
    earned = 0.0
    total = 0.0
    required_failed = 0
    for it in items:
        v = verdicts.get(it.item_id, {"satisfied": False, "reason": "missing verdict"})
        got = it.points if v["satisfied"] else 0.0
        earned += got
        total += it.points
        if it.required and not v["satisfied"]:
            required_failed += 1
        detail.append(
            {
                "id": it.item_id,
                "criterion": it.criterion,
                "points": it.points,
                "earned": got,
                "satisfied": v["satisfied"],
                "reason": v["reason"],
            }
        )

    weighted = (earned / total * 100.0) if total else 0.0
    return {
        "score": round(weighted, 2),
        "earned": earned,
        "points_possible": total,
        "n_items": len(items),
        "n_satisfied": sum(1 for d in detail if d["satisfied"]),
        "required_failed": required_failed,
        "files": [p.name for p in files],
        "items": detail,
    }
