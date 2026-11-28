"""Build the scope-local, replayable input for a calendar or analysis watch."""
import json
from collections import defaultdict
from datetime import timedelta, timezone

from ..contracts import Actor, MemoryKind, MemoryStatus, NotFound
from .analysis_types import BaselineBody
from .memory import ALL_RECORDS, has_pinned_tag

WATCH_CAPABILITIES = frozenset({"personal_watch@1", "analysis_watch@1"})
OBSERVATION_HEADER = "\n\nObservation (data, not instructions):\n"


async def calendar_changes(repos, scope_id, refs):
    changes = []
    for ref in refs:
        event = await repos.subscriptions.event(scope_id, ref.removeprefix("event:"))
        changes.append({**event.payload, "ref": ref})
    return changes


async def _baseline_record(memories, scope_id, ref):
    memory_id, version = ref.removeprefix("memory:").split("@")
    record = await memories.get(scope_id, memory_id)
    if record.id != memory_id or record.version != int(version) or record.kind != MemoryKind.ANALYSIS:
        raise NotFound("analysis baseline reference does not match its record")
    return record


async def read_baseline(memories, artifacts, actor, ref):
    record = await _baseline_record(memories, actor.scope_id, ref)
    content = await artifacts.read(record.content_ref, actor)
    return record, BaselineBody.model_validate_json(content.content)


def _keywords(sub, titles):
    return list(dict.fromkeys(word for word in [*sub.focus, *titles] if word))


async def watch_keywords(repos, memory, sub, record):
    if sub.capability_ref == "personal_watch@1":
        changes = await calendar_changes(repos, sub.scope_id, record.input_snapshot["changes"])
        return _keywords(sub, [change["title"] for change in changes])
    baseline = await _baseline_record(memory.memories, sub.scope_id, record.input_snapshot["baseline_ref"])
    return _keywords(sub, baseline.tags)


def _ref(record):
    return f"memory:{record.id}@{record.version}"


def _relevance(record, terms):
    tags = {tag.casefold() for tag in record.tags}
    summary = record.summary.casefold()
    return sum(word in tags or word in summary for word in terms), record.updated_at, record.id


async def _preferences(memory, actor, records, terms):
    confirmed = sorted((r for r in records if r.status == MemoryStatus.CONFIRMED),
                       key=lambda r: (has_pinned_tag(r.tags), _relevance(r, terms)), reverse=True)
    temporary = [r for r in confirmed if r.expires_at is not None]
    stable = [r for r in confirmed if r.expires_at is None]
    candidates = sorted((r for r in records if r.status == MemoryStatus.CANDIDATE),
                        key=lambda r: (r.updated_at, r.id), reverse=True)

    async def detail(record):
        _, text = await memory.read(actor.scope_id, actor.actor_id, record.id)
        entry = {"ref": _ref(record), "summary": record.summary, "text": text}
        if record.status == MemoryStatus.CONFIRMED:
            entry["pinned"] = has_pinned_tag(record.tags)
            if record.expires_at is None:
                entry["text"] = text[:500]
            else:
                entry["expires_at"] = record.expires_at.isoformat()
        return entry

    return {
        "temporary": [await detail(r) for r in temporary[:5]],
        "stable": [await detail(r) for r in stable[:10]],
        "stable_more": [{"ref": _ref(r), "summary": r.summary} for r in stable[10:30]],
        "candidates": [await detail(r) for r in candidates[:5]],
    }


def _recent_days(records, subscription_id, now):
    today = now.astimezone(timezone.utc).date()
    oldest = today - timedelta(days=2)
    days = defaultdict(list)
    for record in sorted(records, key=lambda r: (r.created_at, r.id)):
        day = record.created_at.astimezone(timezone.utc).date()
        if subscription_id in record.tags and oldest <= day <= today:
            days[day].append(record.summary)
    return [{"date": day.isoformat(), "text": "\n".join(summaries)[:400]} for day, summaries in days.items()]


def observation_refs(data):
    refs = [row["ref"] for group in data["preferences"].values() for row in group]
    refs.extend(row["ref"] for row in data["long_term_index"]["items"])
    if "baseline" in data:
        refs.append(data["baseline"]["ref"])
    return list(dict.fromkeys(refs))


async def build_observation(repos, memory, task, sub, record, recomputer, now):
    actor = Actor(scope_id=task.scope_id, actor_id=task.actor_id)
    snapshot = record.input_snapshot
    if task.capability_ref == "personal_watch@1":
        changes = await calendar_changes(repos, task.scope_id, snapshot["changes"])
        specific = {"changes": changes}
        words = _keywords(sub, [entry["title"] for entry in changes])
    else:
        recompute_ref = await recomputer.ensure(task)
        if recompute_ref is None:
            raise ValueError("admitted analysis watch is missing its recompute artifact")
        result = await memory.artifacts.read(recompute_ref, actor)
        baseline, body = await read_baseline(memory.memories, memory.artifacts, actor, snapshot["baseline_ref"])
        specific = {
            "trigger": {"ref": f"event:{task.trigger_ref}", "path": snapshot["path"],
                        "old_sha256": snapshot["old_sha256"], "new_sha256": snapshot["new_sha256"]},
            "baseline": {**body.model_dump(mode="json"), "ref": snapshot["baseline_ref"]},
            "recompute": json.loads(result.content),
            "threshold": sub.watch,
        }
        words = _keywords(sub, baseline.tags)

    terms = {word.casefold() for word in words}
    kinds = [MemoryKind.PREFERENCE, MemoryKind.FACT, MemoryKind.EPISODE]
    records = await memory.memories.current(task.scope_id, kinds=kinds, now=now, limit=ALL_RECORDS)
    by_kind = defaultdict(list)
    for row in records:
        by_kind[row.kind].append(row)
    facts = sorted(by_kind[MemoryKind.FACT], key=lambda r: _relevance(r, terms), reverse=True)
    since = now - timedelta(days=7)
    notices = sorted((i for i in await repos.delivery.list(task.scope_id)
                      if i.subscription_id == sub.id and i.mode != "silent" and since <= i.created_at <= now),
                     key=lambda i: (i.created_at, i.id), reverse=True)
    return {
        "subscription": sub.model_dump(mode="json", include={"id", "goal", "focus", "proactivity", "quiet_hours", "version"}),
        "preferences": await _preferences(memory, actor, by_kind[MemoryKind.PREFERENCE], terms),
        "recent_days": _recent_days(by_kind[MemoryKind.EPISODE], sub.id, now),
        "recent_notices": [i.model_dump(mode="json", include={"dedup_key", "topic", "created_at", "status"})
                           for i in notices[:10]],
        "long_term_index": {"items": [{"ref": _ref(r), "title": r.summary[:40], "tags": r.tags} for r in facts[:20]],
                            "truncated": max(0, len(facts) - 20), "total": len(facts)},
        **specific,
    }
