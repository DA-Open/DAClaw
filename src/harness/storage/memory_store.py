"""Versioned memories, with one store lock for each in-memory transaction."""
from ..contracts import InvalidTransition, MemoryRecord, NotFound


def is_current(record, now):
    return record.ended_at is None and (record.expires_at is None or record.expires_at > now)


def changed(record, **updates):
    return MemoryRecord.model_validate({**record.model_dump(), **updates})


def replacement_version(old, replacement, now):
    if replacement.scope_id != old.scope_id or replacement.id == old.id or not is_current(replacement, now):
        raise InvalidTransition("replacement must be a new, current record in the same scope")
    return changed(replacement, supersedes_id=old.id, version=old.version + 1, updated_at=now)


def query_terms(query):
    terms = query.lower().split()
    if not terms:
        raise ValueError("query must contain a keyword")
    return terms


class InMemoryMemoryStore:
    def __init__(self, store):
        self._s = store
        store.memories = {}

    def _insert(self, record):
        if record.id in self._s.memories:
            raise InvalidTransition("memory ID already exists")
        if record.supersedes_id is not None and any(
            r.supersedes_id == record.supersedes_id for r in self._s.memories.values()
        ):
            raise InvalidTransition("memory was already superseded")
        self._s.memories[record.id] = record.model_copy(deep=True)
        return record.model_copy(deep=True)

    async def put(self, record):
        async with self._s.lock:
            if record.source_ref.startswith("task:"):
                prior = self._list(record.scope_id, now=record.created_at, kinds=[record.kind],
                                   source_ref=record.source_ref, include_ended=True, limit=1)
                if prior:
                    return prior[0]
            return self._insert(record)

    async def get(self, scope_id, memory_id):
        async with self._s.lock:
            record = self._s.memories.get(memory_id)
            if record is None or record.scope_id != scope_id:
                raise NotFound("memory not found")
            return record.model_copy(deep=True)

    def _list(self, scope_id, *, now, kinds=None, statuses=None, include_ended=False,
              end_reasons=None, source_ref=None, limit=200):
        rows = [r for r in self._s.memories.values() if r.scope_id == scope_id
                and (include_ended or is_current(r, now))
                and (kinds is None or r.kind in kinds) and (statuses is None or r.status in statuses)
                and (end_reasons is None or r.end_reason in end_reasons)
                and (source_ref is None or r.source_ref == source_ref)]
        return [r.model_copy(deep=True) for r in sorted(rows, key=lambda r: r.updated_at, reverse=True)[:limit]]

    async def list(self, scope_id, *, now, kinds=None, statuses=None, include_ended=False,
                   end_reasons=None, source_ref=None, limit=200):
        async with self._s.lock:
            return self._list(scope_id, now=now, kinds=kinds, statuses=statuses, include_ended=include_ended,
                              end_reasons=end_reasons, source_ref=source_ref, limit=limit)

    async def current(self, scope_id, *, now, kinds, subject_ref=None, limit=50):
        async with self._s.lock:
            rows = self._list(scope_id, now=now, kinds=kinds, limit=len(self._s.memories))
            return [r for r in rows if subject_ref is None or r.subject_ref == subject_ref][:limit]

    async def search(self, scope_id, query, *, now, kinds=None, limit=20):
        terms = query_terms(query)
        async with self._s.lock:
            rows = self._list(scope_id, now=now, kinds=kinds, limit=len(self._s.memories))
            scored = [(sum(t in r.summary.lower() or t in [tag.lower() for tag in r.tags] for t in terms), r)
                      for r in rows]
            scored.sort(key=lambda pair: (pair[0], pair[1].updated_at), reverse=True)
            return [r for score, r in scored if score > 0][:limit]

    def _changing(self, scope_id, memory_id, now):
        record = self._s.memories.get(memory_id)
        if record is None or record.scope_id != scope_id or not is_current(record, now):
            raise InvalidTransition("memory is not current in this scope")
        return record

    async def supersede(self, scope_id, memory_id, replacement, now):
        async with self._s.lock:
            old = self._changing(scope_id, memory_id, now)
            new = replacement_version(old, replacement, now)
            if new.source_ref.startswith("task:") and self._list(
                scope_id, now=now, kinds=[new.kind], source_ref=new.source_ref, include_ended=True, limit=1
            ):
                raise InvalidTransition("task already has a memory of this kind")
            result = self._insert(new)
            self._s.memories[old.id] = changed(old, ended_at=now, end_reason="superseded")
            return result

    async def retract(self, scope_id, memory_id, reason, now):
        async with self._s.lock:
            old = self._changing(scope_id, memory_id, now)
            ended = changed(old, ended_at=now, end_reason=reason)
            self._s.memories[old.id] = ended
            return ended.model_copy(deep=True)
