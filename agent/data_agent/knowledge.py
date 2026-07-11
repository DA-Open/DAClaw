"""Local, deterministic knowledge store for joint data + document analysis."""

from __future__ import annotations

import html
import json
import re
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from daclaw_constants import get_daclaw_home


SUPPORTED_SUFFIXES = {".txt", ".md", ".markdown", ".json", ".html", ".htm", ".pdf", ".docx"}
_TOKEN_RE = re.compile(r"[\w\u3400-\u9fff]+", re.UNICODE)


class KnowledgeError(ValueError):
    pass


def get_knowledge_dir() -> Path:
    path = get_daclaw_home() / "knowledge"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _manifest_path() -> Path:
    return get_knowledge_dir() / "manifest.json"


def _load_manifest() -> Dict[str, Any]:
    path = _manifest_path()
    if not path.is_file():
        return {"version": 1, "sources": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"version": 1, "sources": []}
    except Exception:
        return {"version": 1, "sources": []}


def _save_manifest(data: Dict[str, Any]) -> None:
    _manifest_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise KnowledgeError(f"Unsupported knowledge format: {suffix or '(none)'}")
    if suffix in {".txt", ".md", ".markdown"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".json":
        try:
            return json.dumps(json.loads(path.read_text(encoding="utf-8")), ensure_ascii=False, indent=2)
        except Exception:
            return path.read_text(encoding="utf-8", errors="replace")
    if suffix in {".html", ".htm"}:
        raw = path.read_text(encoding="utf-8", errors="replace")
        raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
        return html.unescape(re.sub(r"<[^>]+>", " ", raw))
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except Exception as exc:
            raise KnowledgeError("PDF knowledge requires pypdf (pip install pypdf).") from exc
        reader = PdfReader(str(path))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(path) as archive:
                xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
        except Exception as exc:
            raise KnowledgeError(f"Could not read DOCX: {exc}") from exc
        return html.unescape(re.sub(r"<[^>]+>", " ", xml))
    raise KnowledgeError(f"Unsupported knowledge format: {suffix}")


def _chunks(text: str, *, size: int = 1200, overlap: int = 160) -> List[str]:
    normalized = re.sub(r"\r\n?", "\n", text)
    normalized = re.sub(r"[ \t]+", " ", normalized)
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", normalized) if part.strip()]
    out: List[str] = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 2 > size:
            out.append(current.strip())
            current = current[-overlap:] + "\n\n" + paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
        while len(current) > size * 1.4:
            out.append(current[:size].strip())
            current = current[size - overlap :]
    if current.strip():
        out.append(current.strip())
    return out or ([normalized[:size]] if normalized.strip() else [])


def connect_document(source: str, *, name: Optional[str] = None) -> Dict[str, Any]:
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise KnowledgeError(f"Knowledge source not found: {path}")
    text = _extract_text(path)
    chunks = _chunks(text)
    if not chunks:
        raise KnowledgeError("No extractable text found in the document.")
    import hashlib

    source_id = "kb_" + hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:10]
    payload = {
        "source_id": source_id,
        "name": name or path.stem,
        "path": str(path),
        "kind": path.suffix.lower().lstrip("."),
        "size": path.stat().st_size,
        "characters": len(text),
        "chunks": chunks,
        "updated_at": time.time(),
    }
    (get_knowledge_dir() / f"{source_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    manifest = _load_manifest()
    sources = [item for item in manifest.get("sources", []) if item.get("source_id") != source_id]
    sources.append({key: value for key, value in payload.items() if key != "chunks"})
    manifest["sources"] = sorted(sources, key=lambda item: item.get("updated_at", 0), reverse=True)
    _save_manifest(manifest)
    return {key: value for key, value in payload.items() if key != "chunks"} | {"chunk_count": len(chunks)}


def list_sources() -> List[Dict[str, Any]]:
    return list(_load_manifest().get("sources") or [])


def _tokens(text: str) -> List[str]:
    return [token.lower() for token in _TOKEN_RE.findall(text or "") if len(token) > 1]


def search(query: str, *, source_ids: Optional[Iterable[str]] = None, limit: int = 8) -> Dict[str, Any]:
    query_tokens = _tokens(query)
    if not query_tokens:
        return {"query": query, "results": [], "sources": 0}
    allowed = set(source_ids or [])
    results: List[Dict[str, Any]] = []
    for source in list_sources():
        source_id = source.get("source_id")
        if allowed and source_id not in allowed:
            continue
        path = get_knowledge_dir() / f"{source_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for index, chunk in enumerate(payload.get("chunks") or []):
            chunk_lower = chunk.lower()
            hits = sum(chunk_lower.count(token) for token in query_tokens)
            coverage = len({token for token in query_tokens if token in chunk_lower})
            phrase_bonus = 4 if query.lower() in chunk_lower else 0
            score = hits + coverage * 2 + phrase_bonus
            if score <= 0:
                continue
            results.append(
                {
                    "source_id": source_id,
                    "source": source.get("name"),
                    "kind": source.get("kind"),
                    "chunk": index,
                    "score": score,
                    "text": chunk[:1600],
                }
            )
    results.sort(key=lambda item: (-item["score"], item["source"], item["chunk"]))
    return {"query": query, "results": results[: max(1, int(limit))], "sources": len(list_sources())}


def remove_source(source_id: str) -> bool:
    manifest = _load_manifest()
    before = len(manifest.get("sources") or [])
    manifest["sources"] = [item for item in manifest.get("sources", []) if item.get("source_id") != source_id]
    _save_manifest(manifest)
    (get_knowledge_dir() / f"{source_id}.json").unlink(missing_ok=True)
    return len(manifest["sources"]) < before

