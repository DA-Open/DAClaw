"""Rules-only admission. Repositories serialize decisions and task insertion."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..contracts import (
    Acceptance, Actor, AdmissionDecision, AgentRole, CheckSpec, Event,
    EventType, Limits, RunLane, Subscription, SubscriptionStatus, Task, TaskOrigin,
    TaskRef, TaskRequest, TaskStatus, TriggerType, Usage, utcnow,
)
from ..proactive_types import DataEvent

POLICY_VERSION = "proactive-admission@1"
UNFINISHED = {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.CANCELLING, TaskStatus.WAITING}


def active(sub: Subscription, now: datetime) -> bool:
    return sub.status == SubscriptionStatus.ACTIVE and (sub.expires_at is None or sub.expires_at > now)


def matches(sub: Subscription, event: Event) -> bool:
    if sub.subject_ref != event.subject_ref:
        return False
    if event.source == "scheduler":
        return event.payload.get("subscription_id") == sub.id
    values = {"source": event.source, "type": event.type, **event.payload}
    return bool(sub.event_filter) and all(values.get(k) == v for k, v in sub.event_filter.items())


def resources_allowed(sub: Subscription, policy) -> bool:
    return bool(sub.resources) and all(policy.check_path(
        Path(p).expanduser().resolve(), scope_id=sub.scope_id, role=AgentRole.PROACTIVE,
    ).allowed for p in sub.resources)


def retain_evaluations(record, prior):
    if prior is None:
        return record
    record.evaluations = list(prior.evaluations)
    if (record.decision, record.reason, record.input_snapshot) != (prior.decision, prior.reason, prior.input_snapshot):
        record.evaluations.append(prior.model_dump(mode="json", exclude={"evaluations"}))
    return record


def daily_budget_reached(used: Usage, budget: Usage) -> bool:
    # The shared contract uses zero for a missing limit. Usage is settled
    # by UTC day, and the token ceiling is total input plus output.
    return any(limit and count >= limit for count, limit in (
        (used.model_calls, budget.model_calls),
        (used.tool_calls, budget.tool_calls),
        (used.total_tokens, budget.total_tokens),
    ))


def decide(sub, event, tasks, records, daily_usage, now, scope_limit, allowed):
    data = DataEvent.model_validate({k: event.payload[k] for k in DataEvent.model_fields})
    own = [t for t in tasks if t.subscription_id == sub.id]
    success = {t.id: t for t in own if t.status == TaskStatus.SUCCEEDED}
    prior = [r for r in records if r.task_id in success]
    decision, reason, kind = "admit", "new_version", "diff" if prior else "baseline"
    if not active(sub, now):
        decision, reason = "skip", "subscription_inactive"
    elif not allowed:
        decision, reason = "skip", "resource_permission_revoked"
    elif any(r.input_snapshot.get("data_version") == data.data_version for r in prior):
        decision, reason = "skip", "version_already_succeeded"
    elif prior and max(prior, key=lambda r: r.decided_at).input_snapshot.get("fingerprint") == data.fingerprint:
        decision, reason = "skip", "unchanged_fingerprint"
    elif any(t.status in UNFINISHED for t in own):
        decision, reason = "defer", "subscription_busy"
    elif daily_budget_reached(daily_usage, sub.daily_budget):
        decision, reason = "defer", "daily_budget"
    elif sum(t.agent_role == AgentRole.PROACTIVE and t.status in UNFINISHED for t in tasks) >= scope_limit:
        decision, reason = "defer", "scope_capacity"
    return AdmissionDecision(
        subscription_id=sub.id, event_id=event.id, decision=decision, reason=reason,
        kind=kind if decision == "admit" else None, policy_version=POLICY_VERSION, decided_at=now,
        input_snapshot={**data.model_dump(), "subject_ref": event.subject_ref,
                        "previous_version": max(prior, key=lambda r: r.decided_at).input_snapshot.get("data_version") if prior else None,
                        "subscription_version": sub.version, "daily_usage": daily_usage.model_dump(),
                        "daily_budget": sub.daily_budget.model_dump(),
                        "scope_limit": scope_limit, "resources_allowed": allowed},
    )


class ProactiveController:
    def __init__(self, repos, tasks, policy, *, scope_limit=2, clock=utcnow):
        self.repos, self.tasks, self.policy = repos, tasks, policy
        self.scope_limit, self.clock = scope_limit, clock

    async def create_subscription(self, sub: Subscription) -> Subscription:
        if sub.expires_at is not None and sub.expires_at.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        if sub.grant_ref is not None:
            raise ValueError("grant_ref is reserved; Q1-Q3 use resource whitelists")
        if not sub.subject_ref or not sub.resources or sub.subject_ref not in sub.resources:
            raise ValueError("subject_ref must be one of the explicitly granted resources")
        if not resources_allowed(sub, self.policy):
            raise PermissionError("subscription resources exceed the user's proactive whitelist")
        if sub.capability_ref != "data_card@1":
            raise ValueError("Q1-Q3 subscriptions support data_card@1")
        self.tasks.prepare(self._request(sub, "validate"), Actor(scope_id=sub.scope_id, actor_id=sub.owner_id),
                           self._origin(sub, "validate"))
        if sub.schedule:
            from croniter import croniter
            if not croniter.is_valid(sub.schedule) or len(sub.schedule.split()) != 5:
                raise ValueError("schedule must be a five-field UTC cron expression")
        return await self.repos.subscriptions.create(sub)

    def _request(self, sub, event_id):
        return TaskRequest(
            request_key=f"proactive:{sub.id}:{event_id}", goal=sub.goal,
            input_refs=list(sub.resources), subject_ref=sub.subject_ref, capability_ref=sub.capability_ref,
            limits=sub.limits, acceptance=Acceptance(required_artifacts=["data_card"], checks=[
                CheckSpec(id="artifact_readable"), CheckSpec(id="data_card_schema"), CheckSpec(id="stats_replay")]),
        )

    @staticmethod
    def _origin(sub, event_id):
        return TaskOrigin(agent_role=AgentRole.PROACTIVE, agent_profile_ref="proactive@1",
                          run_lane=RunLane.BACKGROUND, trigger_type=TriggerType.EVENT,
                          trigger_ref=event_id, subscription_id=sub.id)

    async def on_event(self, event: Event) -> list[TaskRef]:
        if event.type != EventType.EXTERNAL:
            raise ValueError("ProactiveController only accepts external data events")
        DataEvent.model_validate({k: event.payload.get(k) for k in DataEvent.model_fields})
        event = await self.repos.events.append(event)
        out = []
        for sub in await self.repos.subscriptions.list_for_scope(event.scope_id):
            if not matches(sub, event):
                continue
            now = self.clock()
            def evaluate(current, tasks, records, usage):
                record = decide(current, event, tasks, records, usage, now, self.scope_limit,
                                resources_allowed(current, self.policy))
                task = None
                if record.decision == "admit":
                    task = self.tasks.prepare(self._request(current, event.id),
                        Actor(scope_id=current.scope_id, actor_id=current.owner_id), self._origin(current, event.id))
                    previous = [t for t in tasks if t.subscription_id == current.id and t.status == TaskStatus.SUCCEEDED]
                    if previous:
                        task.source_task_id = max(previous, key=lambda t: t.updated_at).id
                    if event.source == "scheduler":
                        task.trigger_type = TriggerType.SCHEDULE
                    record.task_id = task.id
                return record, task
            record = await self.repos.subscriptions.admit(event, sub.id, evaluate, now)
            if record.task_id:
                out.append(TaskRef.of(await self.repos.tasks.get(event.scope_id, record.task_id)))
        await self.repos.subscriptions.mark_processed(event.scope_id, event.id, self.clock())
        return out

    async def revoke(self, scope_id, subscription_id):
        return await self.tasks.revoke_subscription(scope_id, subscription_id)
