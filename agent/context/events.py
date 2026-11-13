"""Harness injection events -- what the model actually saw.

The agent loop injects context at several points (system prompt layers, external
memory prefetch, plugin hooks). Until now all of it was computed, used, and
thrown away, so nobody could answer "why did this turn cost 59k input tokens".

This module records every injection as a typed event into the same SQLite
state.db that already holds sessions and messages. It is pure observation:
nothing here may change what the model receives, so a run with recording on
must produce the same answer (and the same benchmark score) as one with it off.

Storage layout is a single table, injection_events; see daclaw_state.SessionDB.
Full text is NOT stored by default (preview + sha1 only) -- set
DACLAW_INJECTION_LOG_FULL=1 when replaying a specific run is more important
than database size.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Same ratio the memory budget comments use (~800 tokens for 2200 chars).
CHARS_PER_TOKEN = 2.75
PREVIEW_CHARS = 400

_TRUE = ("1", "true", "yes", "on")


def _no_log() -> bool:
    """DACLAW_NO_INJECTION_LOG=1 disables recording entirely (e.g. benchmarks).

    Read at call time, not import time: callers such as benchmark entry points
    set the variable after this module has already been imported.
    """
    return os.getenv("DACLAW_NO_INJECTION_LOG", "").lower() in _TRUE


def _store_full() -> bool:
    """DACLAW_INJECTION_LOG_FULL=1 also stores the injected text verbatim."""
    return os.getenv("DACLAW_INJECTION_LOG_FULL", "").lower() in _TRUE


_shared_db = None
_shared_db_path = None


def est_tokens(text: Optional[str]) -> int:
    """Rough token estimate; only used for the bill, never for budgeting."""
    if not text:
        return 0
    return int(len(text) / CHARS_PER_TOKEN)


def digest(text: Optional[str]) -> str:
    if not text:
        return ""
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:16]


@dataclass
class InjectionEvent:
    """One piece of context handed to the model.

    kind:    system_prompt | memory_prefetch | plugin_context | ... (free-form)
    source:  which layer / provider / hook produced it
    text:    the injected text (stored as preview only, unless FULL is on)
    turn:    user turn index within the session
    iteration: tool-calling iteration within the turn
    meta:    anything else worth keeping (layer index, table count, ...)
    """

    kind: str
    source: str = ""
    text: str = ""
    turn: int = 0
    iteration: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)
    # For entries whose size is known but whose text is not worth keeping
    # (e.g. the system-prompt remainder). Overrides len(text).
    declared_chars: Optional[int] = None

    @property
    def chars(self) -> int:
        if self.declared_chars is not None:
            return max(0, int(self.declared_chars))
        return len(self.text or "")

    @property
    def tokens(self) -> int:
        return int(self.chars / CHARS_PER_TOKEN)

    def to_row(self) -> Dict[str, Any]:
        text = self.text or ""
        try:
            meta_json = json.dumps(self.meta or {}, ensure_ascii=False, default=str)
        except Exception:
            meta_json = "{}"
        return {
            "kind": self.kind or "unknown",
            "source": self.source or "",
            "chars": self.chars,
            "est_tokens": self.tokens,
            "sha1": digest(text),
            "preview": text[:PREVIEW_CHARS],
            "meta_json": meta_json,
            "turn": int(self.turn or 0),
            "iteration": int(self.iteration or 0),
            "full_text": text if _store_full() else "",
        }


def default_db():
    """Lazily open the state DB under the current DACLAW_HOME.

    The path is resolved per call, not at import time: daclaw_state fixes
    DEFAULT_DB_PATH when it is first imported, so a process that changes
    DACLAW_HOME afterwards (tests, benchmark harnesses, profiles) would
    otherwise keep writing to the original database.

    Opening a second connection is fine here: the DB is WAL with a short busy
    timeout and application-level retry, and events are append-only.
    """
    global _shared_db, _shared_db_path
    try:
        from daclaw_constants import get_daclaw_home

        path = get_daclaw_home() / "state.db"
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("injection logging disabled: %s", exc)
        return None
    if _shared_db is not None and _shared_db_path == path:
        return _shared_db
    try:
        from daclaw_state import SessionDB  # local import: avoid a cycle

        _shared_db = SessionDB(path)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("injection logging disabled: %s", exc)
        return None
    _shared_db_path = path
    return _shared_db


def record(
    session_id: Optional[str],
    events: Iterable[InjectionEvent],
    db=None,
) -> int:
    """Persist events. Never raises: telemetry must not break the agent loop."""
    if _no_log() or not session_id:
        return 0
    rows = [e.to_row() for e in events if e is not None and (e.text or e.meta)]
    if not rows:
        return 0
    try:
        store = db or default_db()
        if store is None:
            return 0
        return store.add_injection_events(session_id, rows)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("injection logging failed: %s", exc)
        return 0


def record_system_prompt_layers(
    session_id: Optional[str],
    layers: List[tuple],
    total_chars: int,
    db=None,
) -> int:
    """Record one system_prompt event per layer, plus a total.

    layers: [(name, text), ...] -- only non-empty layers are recorded.
    """
    events: List[InjectionEvent] = []
    known = 0
    for name, text in layers:
        if not text:
            continue
        known += len(text)
        events.append(
            InjectionEvent(kind="system_prompt", source=name, text=text, turn=0)
        )
    remainder = max(0, total_chars - known)
    if remainder:
        events.append(
            InjectionEvent(
                kind="system_prompt",
                source="other_layers",
                text="",
                turn=0,
                declared_chars=remainder,
                meta={"synthetic": True, "chars": remainder},
            )
        )
    return record(session_id, events, db=db)
