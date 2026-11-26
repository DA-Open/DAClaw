"""Rules-only admission. Repositories serialize decisions and task insertion."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..personal.analysis_types import AnalysisWatch, BaselineBody, SourceChangedEvent
from ..personal.analysis_admission import decide_analysis
from ..personal.memory import is_current
from ..contracts import (
    Acceptance, Actor, AdmissionDecision, AgentRole, CheckSpec, Event,
    EventType, Limits, RunLane, Subscription, SubscriptionStatus, Task, TaskOrigin,
    TaskRef, TaskRequest, TaskStatus, TriggerType, Usage, utcnow,
)
from ..proactive_types import DataEvent, default_daily_budget
from ..personal.calendar_admission import decide_personal
from ..personal.calendar_types import PersonalWindowEvent

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


def _daily_budget_at_creation(sub: Subscription) -> Usage:
    if "daily_budget" not in sub.model_fields_set:
        return default_daily_budget()
    requested = sub.daily_budget.model_dump(exclude_unset=True)
    if not requested:
        return sub.daily_budget
    ceilings = default_daily_budget().model_dump()
    if "input_tokens" in requested or "output_tokens" in requested:
        ceilings["input_tokens"] = 0
    return Usage(**(ceilings | requested))


def daily_budget_reached(used: Usage, budget: Usage) -> bool:
    return (
        used.model_calls >= budget.model_calls
        or used.tool_calls >= budget.tool_calls
        or used.total_tokens >= budget.total_tokens
    )


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
    def __init__(self, repos, tasks, policy, *, memories=None, scope_limit=2, clock=utcnow):
        self.repos, self.tasks, self.policy = repos, tasks, policy
        self.memories = memories
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
        if sub.capability_ref == "personal_watch@1":
            if not sub.schedule or sub.event_filter or sub.baseline_ref is not None or sub.watch is not None:
                raise ValueError("personal_watch requires schedule and empty event_filter, baseline_ref and watch")
            if sub.resources != [sub.subject_ref] or Path(sub.subject_ref).suffix != ".json":
                raise ValueError("personal_watch resources must be exactly the subject_ref JSON file")
            if not self.policy.check_path(Path(sub.subject_ref).expanduser().resolve(),
                                          scope_id=sub.scope_id, role=AgentRole.MAIN).allowed:
                raise PermissionError("subscription resources exceed the user's main whitelist")
        elif sub.capability_ref == "analysis_watch@1":
            await self._check_analysis_subscription(sub)
        elif sub.capability_ref != "data_card@1":
            raise ValueError("Q1-Q3 subscriptions support data_card@1")
        self.tasks.prepare(self._request(sub, "validate"), Actor(scope_id=sub.scope_id, actor_id=sub.owner_id),
                           self._origin(sub, "validate"))
        if sub.schedule:
            from croniter import croniter
            if not croniter.is_valid(sub.schedule) or len(sub.schedule.split()) != 5:
                raise ValueError("schedule must be a five-field UTC cron expression")
        return await self.repos.subscriptions.create(sub.model_copy(
            update={"daily_budget": _daily_budget_at_creation(sub)}
        ))

    async def _check_analysis_subscription(self, sub: Subscription) -> None:
        if not sub.baseline_ref or not sub.watch:
            raise ValueError("analysis_watch requires baseline_ref and watch")
        if sub.schedule or sub.event_filter != {"kind": "source_changed"}:
            raise ValueError('analysis_watch requires empty schedule and event_filter {"kind": "source_changed"}')
        body = await self._baseline(sub)
        spec = AnalysisWatch.model_validate(sub.watch)
        if spec.query_index >= len(body.queries):
            raise ValueError("watch.query_index is not in the baseline")
        query = body.queries[spec.query_index]
        if spec.column not in query.columns:
            raise ValueError(f"watch.column {spec.column!r} is not in that query")
        if query.row_count != 1:
            raise ValueError("analysis_watch can only watch a query with exactly one row")
        if sub.subject_ref != body.sources[0].path:
            raise ValueError("subject_ref must be the baseline's first source")
        if sorted(sub.resources) != sorted(source.path for source in body.sources):
            raise ValueError("resources must be exactly the baseline's sources")
        for path in sub.resources:
            for role in (AgentRole.PROACTIVE, AgentRole.MAIN):
                if not self.policy.check_path(Path(path).expanduser().resolve(), scope_id=sub.scope_id,
                                              role=role).allowed:
                    raise PermissionError("subscription resources exceed both whitelists")

    async def _baseline(self, sub: Subscription) -> BaselineBody:
        if self.memories is None:
            raise ValueError("analysis_watch needs the memory service")
        memory_id, _, version = (sub.baseline_ref or "").removeprefix("memory:").partition("@")
        record, text = await self.memories.read(sub.scope_id, "harness", memory_id)
        if record.kind.value != "analysis" or str(record.version) != version:
            raise ValueError("baseline_ref must name a current ANALYSIS memory")
        if not is_current(record, self.clock()):
            raise ValueError("baseline_ref must name a current ANALYSIS memory")
        body = BaselineBody.model_validate_json(text)
        if not body.sources or sub.subject_ref not in {source.path for source in body.sources}:
            raise ValueError("baseline does not depend on this subject")
        return body

    def _request(self, sub, event_id):
        if sub.capability_ref == "analysis_watch@1":
            return TaskRequest(
                request_key=f"proactive:{sub.id}:{event_id}", goal=sub.goal,
                input_refs=list(sub.resources), subject_ref=sub.subject_ref, capability_ref=sub.capability_ref,
                limits=sub.limits, acceptance=Acceptance(required_artifacts=["decision"], checks=[
                    CheckSpec(id="artifact_readable"), CheckSpec(id="decision_schema"),
                    CheckSpec(id="decision_evidence"), CheckSpec(id="recompute_replay")]),
            )
        if sub.capability_ref == "personal_watch@1":
            return TaskRequest(
                request_key=f"proactive:{sub.id}:{event_id}", goal=sub.goal,
                input_refs=[sub.subject_ref], subject_ref=sub.subject_ref, capability_ref=sub.capability_ref,
                limits=sub.limits, acceptance=Acceptance(required_artifacts=["decision"], checks=[
                    CheckSpec(id="artifact_readable"), CheckSpec(id="decision_schema"), CheckSpec(id="decision_evidence")]),
            )
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
        if event.payload.get("kind") == "personal_window":
            PersonalWindowEvent.model_validate(event.payload)
        elif event.payload.get("kind") == "source_changed":
            SourceChangedEvent.model_validate(event.payload)
        else:
            DataEvent.model_validate({k: event.payload.get(k) for k in DataEvent.model_fields})
        event = await self.repos.events.append(event)
        out = []
        for sub in await self.repos.subscriptions.list_for_scope(event.scope_id):
            if not matches(sub, event):
                continue
            now = self.clock()
            if sub.capability_ref == "personal_watch@1":
                window = PersonalWindowEvent.model_validate(event.payload)
                changes = await self.repos.subscriptions.changes(
                    event.scope_id, sub.subject_ref, window.since, window.until, source="mock_calendar")
            baseline = None
            if sub.capability_ref == "analysis_watch@1":
                try:
                    baseline = await self._baseline(sub)
                except (ValueError, NotFound):
                    # The baseline ended: record it for this subscription and keep
                    # going, the other subscriptions still need their decision.
                    baseline = None
            def evaluate(current, tasks, records, usage):
                if current.capability_ref == "analysis_watch@1":
                    record = decide_analysis(current, event, baseline.model_dump(), tasks, records, usage, now,
                                             self.scope_limit, resources_allowed(current, self.policy))
                elif current.capability_ref == "personal_watch@1":
                    record = decide_personal(current, event, window, changes, tasks, usage, now, self.scope_limit,
                                             resources_allowed(current, self.policy))
                else:
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
