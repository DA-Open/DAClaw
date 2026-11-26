"""Rules-only admission for analysis watch: no SQL, no file reads.

The controller sees a source_changed event and a subscription that points at a
baseline. Everything needed to decide is already stored: the baseline's source
digest, the previous decision, the task list and the budget counters.
"""
from __future__ import annotations

from datetime import datetime

from ..contracts import (
    AdmissionDecision,
    Subscription,
    TaskStatus,
)
from .analysis_types import SourceChangedEvent

POLICY_VERSION = "analysis-admission@1"
UNFINISHED = {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.CANCELLING, TaskStatus.WAITING}


def active(sub: Subscription, now: datetime) -> bool:
    from ..contracts import SubscriptionStatus

    return sub.status is SubscriptionStatus.ACTIVE and (sub.expires_at is None or sub.expires_at > now)


def decide_analysis(
    sub: Subscription,
    event,
    baseline: dict,
    tasks,
    records,
    daily_usage,
    now: datetime,
    scope_limit: int,
    allowed: bool,
) -> AdmissionDecision:
    """Admit, skip or defer; the reason string is what tests and traces read."""
    data = SourceChangedEvent.model_validate(event.payload)
    own = [t for t in tasks if t.subscription_id == sub.id]
    prior = [r for r in records if r.task_id and any(t.id == r.task_id for t in own)]
    source = next((s for s in baseline["sources"] if s["path"] == data.path), None)
    last_admit = next((r for r in reversed(records) if r.decision == "admit"), None)
    old_sha = last_admit.input_snapshot.get("new_sha256") if last_admit else None

    decision, reason = "admit", "source_changed"
    if not active(sub, now):
        decision, reason = "skip", "subscription_inactive"
    elif source is None:
        decision, reason = "skip", "source_not_in_baseline"
    elif data.sha256 == source["sha256"] or data.sha256 == old_sha:
        decision, reason = "skip", "unchanged_source"
    elif any(t.status in UNFINISHED for t in own):
        decision, reason = "defer", "subscription_busy"
    elif (daily_usage.model_calls >= sub.daily_budget.model_calls
          or daily_usage.tool_calls >= sub.daily_budget.tool_calls
          or daily_usage.total_tokens >= sub.daily_budget.total_tokens):
        decision, reason = "defer", "daily_budget"
    elif sum(t.agent_role.value == "proactive" and t.status in UNFINISHED for t in tasks) >= scope_limit:
        decision, reason = "defer", "scope_capacity"
    elif not allowed:
        decision, reason = "skip", "resource_permission_revoked"

    return AdmissionDecision(
        subscription_id=sub.id,
        event_id=event.id,
        decision=decision,
        reason=reason,
        policy_version=POLICY_VERSION,
        decided_at=now,
        input_snapshot={
            "kind": "source_changed",
            "baseline_ref": sub.baseline_ref,
            "path": data.path,
            "old_sha256": source["sha256"] if source else None,
            "new_sha256": data.sha256,
            "watch": sub.watch,
            "subscription_version": sub.version,
            "daily_usage": daily_usage.model_dump(),
            "scope_limit": scope_limit,
            "resources_allowed": allowed,
        },
    )


__all__ = ["decide_analysis"]
