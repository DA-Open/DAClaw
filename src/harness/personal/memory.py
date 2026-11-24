"""Trusted memory writes and conservative, content-based candidate deduplication."""
import logging
import unicodedata
from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from ..contracts import Actor, ArtifactInput, InvalidTransition, MemoryKind, MemoryRecord, MemoryStatus, new_id
from ..interfaces import ArtifactStore, MemoryStore
from ..storage.memory_store import changed, is_current

log = logging.getLogger(__name__)
# Whole-scope comparisons must not silently omit older records at the list API's default page size.
ALL_RECORDS = 2**31 - 1


class MemoryRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=4000)
    summary: str = Field(min_length=1, max_length=300)
    tags: list[str] = Field(max_length=16)
    expires_at: datetime | None = None


def _tags(kind, tags, expires_at):
    result = list(tags)
    if kind == MemoryKind.PREFERENCE:
        result = [t for t in result if t != "temporary"]
        if expires_at is not None:
            result.append("temporary")
    return result


def _expiry(expires_at, now):
    if expires_at is not None and (expires_at.utcoffset() is None or expires_at <= now):
        raise ValueError("expires_at must include a timezone and be later than now")


def _normalized(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold()
                   if not c.isspace() and unicodedata.category(c)[0] not in "PZ")


class MemoryService:
    def __init__(self, memories: MemoryStore, artifacts: ArtifactStore, clock: Callable[[], datetime]):
        self.memories = memories
        self.artifacts = artifacts
        self.clock = clock

    async def _content(self, record, text):
        ref = await self.artifacts.put(ArtifactInput(scope_id=record.scope_id, task_id=None,
            name=f"memory/{record.id}", media_type="text/plain", content=text.encode("utf-8"),
            source_refs=[record.source_ref]))
        return changed(record, content_ref=ref.id)

    async def remember(self, scope_id, actor_id, kind, text, *, status, summary, tags,
                       source_ref, subject_ref, expires_at):
        now = self.clock()
        _expiry(expires_at, now)
        record = MemoryRecord(scope_id=scope_id, kind=kind, status=status, summary=summary,
            tags=_tags(kind, tags, expires_at), source_ref=source_ref, subject_ref=subject_ref,
            expires_at=expires_at, content_ref="", created_at=now, updated_at=now)
        record = await self._content(record, text)
        try:
            return await self.memories.put(record)
        except IntegrityError:
            if not source_ref.startswith("task:"):
                raise
            prior = await self.memories.list(scope_id, now=now, kinds=[kind], source_ref=source_ref,
                                             include_ended=True, limit=1)
            if not prior:
                raise
            return prior[0]

    async def read(self, scope_id, actor_id, memory_id):
        record = await self.memories.get(scope_id, memory_id)
        content = await self.artifacts.read(record.content_ref, Actor(actor_id=actor_id, scope_id=scope_id))
        return record, content.content.decode("utf-8")

    async def _current(self, scope_id, memory_id):
        record = await self.memories.get(scope_id, memory_id)
        if not is_current(record, self.clock()):
            raise InvalidTransition("memory is no longer current")
        return record

    def _replacement(self, old, actor_id, **updates):
        # User edits have their own provenance; the original task key stays unique in history.
        return changed(old, id=new_id("mem"), source_ref=f"user:{actor_id}",
                       created_at=self.clock(), updated_at=self.clock(), **updates)

    async def revise(self, scope_id, actor_id, memory_id, revision: MemoryRevision):
        old = await self._current(scope_id, memory_id)
        expiry = revision.expires_at if "expires_at" in revision.model_fields_set else old.expires_at
        _expiry(expiry, self.clock())
        replacement = self._replacement(old, actor_id, summary=revision.summary,
            tags=_tags(old.kind, revision.tags, expiry), expires_at=expiry)
        replacement = await self._content(replacement, revision.text)
        return await self.memories.supersede(scope_id, memory_id, replacement, self.clock())

    async def confirm(self, scope_id, actor_id, memory_id):
        old = await self._current(scope_id, memory_id)
        replacement = self._replacement(old, actor_id, status=MemoryStatus.CONFIRMED)
        return await self.memories.supersede(scope_id, memory_id, replacement, self.clock())

    async def reject(self, scope_id, actor_id, memory_id):
        old = await self._current(scope_id, memory_id)
        if old.status != MemoryStatus.CANDIDATE:
            raise InvalidTransition("only candidates can be rejected")
        return await self.memories.retract(scope_id, memory_id, "rejected", self.clock())

    async def forget(self, scope_id, actor_id, memory_id):
        await self._current(scope_id, memory_id)
        return await self.memories.retract(scope_id, memory_id, "retracted", self.clock())

    async def propose(self, scope_id, kind, text, *, summary, tags, source_ref):
        now = self.clock()
        current = await self.memories.list(scope_id, now=now, limit=ALL_RECORDS)
        if sum(r.status == MemoryStatus.CANDIDATE for r in current) >= 10:
            log.warning("memory candidate limit (10) reached for scope %s", scope_id)
            return None
        rejected = await self.memories.list(scope_id, now=now, statuses=[MemoryStatus.CANDIDATE],
            include_ended=True, end_reasons=["rejected"], limit=ALL_RECORDS)
        for record in [*current, *rejected]:
            if record.kind == kind and _normalized(record.summary) == _normalized(summary):
                _, prior = await self.read(scope_id, "harness", record.id)
                if _normalized(prior) == _normalized(text):
                    return None
        return await self.remember(scope_id, "harness", kind, text, status=MemoryStatus.CANDIDATE,
            summary=summary, tags=tags, source_ref=source_ref, subject_ref=None, expires_at=None)
