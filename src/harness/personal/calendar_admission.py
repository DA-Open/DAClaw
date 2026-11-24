"""Rules-only admission for a calendar window; no model calls."""
from ..contracts import AdmissionDecision, AgentRole, SubscriptionStatus, TaskStatus


def decide_personal(sub, event, window, changes, tasks, daily_usage, now, scope_limit, allowed):
    unfinished = [t for t in tasks if t.status in {
        TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.CANCELLING, TaskStatus.WAITING,
    }]
    decision, reason = "admit", "window_changes"
    if sub.status != SubscriptionStatus.ACTIVE or (sub.expires_at is not None and sub.expires_at <= now):
        decision, reason = "skip", "subscription_inactive"
    elif not allowed:
        decision, reason = "skip", "resource_permission_revoked"
    elif not changes:
        decision, reason = "skip", "no_changes"
    elif any(t.subscription_id == sub.id for t in unfinished):
        decision, reason = "defer", "subscription_busy"
    elif (daily_usage.model_calls >= sub.daily_budget.model_calls
          or daily_usage.tool_calls >= sub.daily_budget.tool_calls
          or daily_usage.total_tokens >= sub.daily_budget.total_tokens):
        decision, reason = "defer", "daily_budget"
    elif sum(t.agent_role == AgentRole.PROACTIVE for t in unfinished) >= scope_limit:
        decision, reason = "defer", "scope_capacity"
    return AdmissionDecision(
        subscription_id=sub.id, event_id=event.id, decision=decision, reason=reason,
        policy_version="personal-admission@1", decided_at=now,
        input_snapshot={"kind": window.kind, "since": window.since.isoformat(), "until": window.until.isoformat(),
                        "changes": [f"event:{change.id}" for change in changes],
                        "subscription_version": sub.version, "daily_usage": daily_usage.model_dump(),
                        "daily_budget": sub.daily_budget.model_dump(),
                        "scope_limit": scope_limit, "resources_allowed": allowed},
    )
