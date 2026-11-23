"""Deterministic D gate. No model-based scores or external delivery."""
from datetime import timedelta

from ..proactive_types import InboxItem
from ..tasks.proactive_controller import active

POLICY_VERSION = "proactive-delivery@1"


def quiet(sub, now):
    start, end = sub.quiet_hours
    return (start <= now.hour < end) if start <= end else (now.hour >= start or now.hour < end)


def delivery_item(task, sub, record, card, now, *, allowed):
    findings = card["profile"]["findings"]
    relevant = task.subject_ref == sub.subject_ref
    relevance = 2 if relevant else 0
    impact = max((f["impact"] for f in findings), default=0)
    urgency = max((f["urgency"] for f in findings), default=0)
    score = 2 * relevance + impact + urgency
    offset = {"quiet": 2, "normal": 0, "eager": -1}[sub.proactivity]
    mode = "notify" if score >= 7 + offset else "digest" if score >= 4 + offset else "silent"
    if not relevant or not allowed or not active(sub, now) or quiet(sub, now) or sub.delivery_mode == "silent":
        mode = "silent"
    version = record.input_snapshot["data_version"]
    return InboxItem(scope_id=task.scope_id, subscription_id=sub.id, task_id=task.id,
        result_ref=task.result_ref, subject_ref=task.subject_ref, data_version=version,
        topic=("Data card: " + task.subject_ref.rsplit("/", 1)[-1])[:80], mode=mode, score=score,
        keywords=list(dict.fromkeys(word for table in card["profile"]["tables"] for word in [table["table"], *table["columns"]])),
        dedup_key=f"{sub.id}:{task.subject_ref}:{version}", valid_until=now + timedelta(seconds=sub.result_ttl_seconds),
        created_at=now)
