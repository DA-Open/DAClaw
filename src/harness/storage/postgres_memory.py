"""Memory queries and row-locked version transitions in PostgreSQL."""
from sqlalchemy import case, insert, or_, select, update

from ..contracts import InvalidTransition, MemoryRecord, NotFound
from . import schema as s
from .memory_store import changed, is_current, query_terms, replacement_version


def _values(record):
    row = record.model_dump(include={"id", "scope_id", "kind", "status", "version", "supersedes_id",
        "subject_ref", "source_ref", "ended_at", "end_reason", "expires_at", "updated_at"})
    return {**row, "summary_lc": record.summary.lower(), "tags_lc": [t.lower() for t in record.tags],
            "body": record.model_dump(mode="json")}


def _query(scope_id, now, kinds, include_ended=False):
    query = select(s.memories.c.body).where(s.memories.c.scope_id == scope_id)
    if not include_ended:
        query = query.where(s.memories.c.ended_at.is_(None),
                            or_(s.memories.c.expires_at.is_(None), s.memories.c.expires_at > now))
    if kinds is not None:
        query = query.where(s.memories.c.kind.in_(kinds))
    return query


class PgMemoryStore:
    def __init__(self, db):
        self._db = db

    async def put(self, record):
        async with self._db.engine.begin() as conn:
            await conn.execute(insert(s.memories).values(**_values(record)))
        return record.model_copy(deep=True)

    async def get(self, scope_id, memory_id):
        async with self._db.engine.connect() as conn:
            body = (await conn.execute(select(s.memories.c.body).where(
                s.memories.c.scope_id == scope_id, s.memories.c.id == memory_id))).scalar_one_or_none()
        if body is None:
            raise NotFound("memory not found")
        return MemoryRecord.model_validate(body)

    async def _read(self, query, limit):
        async with self._db.engine.connect() as conn:
            rows = (await conn.execute(query.limit(limit))).scalars().all()
        return [MemoryRecord.model_validate(row) for row in rows]

    async def list(self, scope_id, *, now, kinds=None, statuses=None, include_ended=False,
                   end_reasons=None, source_ref=None, limit=200):
        query = _query(scope_id, now, kinds, include_ended)
        if statuses is not None:
            query = query.where(s.memories.c.status.in_(statuses))
        if end_reasons is not None:
            query = query.where(s.memories.c.end_reason.in_(end_reasons))
        if source_ref is not None:
            query = query.where(s.memories.c.source_ref == source_ref)
        return await self._read(query.order_by(s.memories.c.updated_at.desc()), limit)

    async def current(self, scope_id, *, now, kinds, subject_ref=None, limit=50):
        query = _query(scope_id, now, kinds)
        if subject_ref is not None:
            query = query.where(s.memories.c.subject_ref == subject_ref)
        return await self._read(query.order_by(s.memories.c.updated_at.desc()), limit)

    async def search(self, scope_id, query, *, now, kinds=None, limit=20):
        matches = []
        for term in query_terms(query):
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            matches.append(or_(s.memories.c.summary_lc.like(f"%{escaped}%", escape="\\"),
                               s.memories.c.tags_lc.any(term)))
        score = sum(case((match, 1), else_=0) for match in matches)
        statement = _query(scope_id, now, kinds).where(or_(*matches))
        return await self._read(statement.order_by(score.desc(), s.memories.c.updated_at.desc()), limit)

    async def _lock(self, conn, scope_id, memory_id, now):
        body = (await conn.execute(select(s.memories.c.body).where(s.memories.c.scope_id == scope_id,
            s.memories.c.id == memory_id).with_for_update())).scalar_one_or_none()
        if body is None:
            raise InvalidTransition("memory is not current in this scope")
        record = MemoryRecord.model_validate(body)
        if not is_current(record, now):
            raise InvalidTransition("memory is no longer current")
        return record

    async def _end(self, conn, old, reason, now):
        ended = changed(old, ended_at=now, end_reason=reason)
        await conn.execute(update(s.memories).where(s.memories.c.scope_id == old.scope_id,
            s.memories.c.id == old.id).values(ended_at=now, end_reason=reason, body=ended.model_dump(mode="json")))
        return ended

    async def supersede(self, scope_id, memory_id, replacement, now):
        async with self._db.engine.begin() as conn:
            old = await self._lock(conn, scope_id, memory_id, now)
            new = replacement_version(old, replacement, now)
            await self._end(conn, old, "superseded", now)
            await conn.execute(insert(s.memories).values(**_values(new)))
            return new

    async def retract(self, scope_id, memory_id, reason, now):
        async with self._db.engine.begin() as conn:
            old = await self._lock(conn, scope_id, memory_id, now)
            return await self._end(conn, old, reason, now)
