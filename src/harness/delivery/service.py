"""Freshness, permission checks and pointer-only handoff to Main."""
import json
from pathlib import Path

from ..contracts import Actor, AgentRole, EventType, InjectedMaterial, NotFound, TaskStatus, utcnow
from ..personal.decision import read_decision, read_recompute
from ..personal.memory import MemoryService
from ..personal.observation import WATCH_CAPABILITIES, watch_keywords
from ..personal.watch_delivery import watch_item
from ..tasks.proactive_controller import active, resources_allowed
from .rules import delivery_item


class DeliveryService:
    def __init__(self, repos, artifacts, policy, *, memory: MemoryService, clock=utcnow):
        self.repos, self.artifacts, self.policy, self.clock = repos, artifacts, policy, clock
        self.memory = memory

    async def prepare(self, task, outcome):
        if not task.subscription_id or outcome.status != TaskStatus.SUCCEEDED:
            return None
        sub = await self.repos.subscriptions.get(task.scope_id, task.subscription_id)
        records = await self.repos.subscriptions.decisions(task.scope_id, sub.id)
        record = next(r for r in records if r.task_id == task.id)
        if task.capability_ref in WATCH_CAPABILITIES:
            decision = await read_decision(self.artifacts, task, outcome.result_ref)
            recompute = None
            if task.capability_ref == "analysis_watch@1":
                recompute = await read_recompute(self.repos, self.artifacts, task)
            keywords = []
            if decision.decision == "propose_notify":
                keywords = await watch_keywords(self.repos, self.memory, sub, record)
            allowed = resources_allowed(sub, self.policy) and self.policy.check_path(
                Path(sub.subject_ref).resolve(), scope_id=task.scope_id, role=AgentRole.MAIN).allowed
            return watch_item(task.model_copy(update={"result_ref": outcome.result_ref}), sub, record, decision,
                              recompute, self.clock(), keywords=keywords, allowed=allowed)
        card = json.loads((await self.artifacts.read(outcome.result_ref,
            Actor(scope_id=task.scope_id, actor_id=task.actor_id))).content)
        return delivery_item(task.model_copy(update={"result_ref": outcome.result_ref}), sub, record,
                             card, self.clock(), allowed=resources_allowed(sub, self.policy))

    async def accessible(self, scope_id, item, *, explicit=False):
        now = self.clock()
        sub = await self.repos.subscriptions.get(scope_id, item.subscription_id)
        if not active(sub, now) or not resources_allowed(sub, self.policy):
            return False
        if not self.policy.check_path(Path(item.subject_ref).resolve(), scope_id=scope_id, role=AgentRole.MAIN).allowed:
            return False
        task = await self.repos.tasks.get(scope_id, item.task_id)
        if task.status != TaskStatus.SUCCEEDED or task.result_ref != item.result_ref:
            return False
        verified = await self.repos.verifications.list_for_task(task.id)
        verdict = next((v for v in verified if v.status == "passed" and v.result_ref == item.result_ref), None)
        if verdict is None:
            return False
        if explicit:
            return True
        if item.valid_until <= now or task.result_expires_at is None or task.result_expires_at <= now:
            return False
        if task.capability_ref == "personal_watch@1":
            evidence = next(c.evidence_refs for c in verdict.checks if c.id == "decision_evidence")
            return await self._calendar_current(item, sub, evidence, now)
        latest = await self.repos.subscriptions.latest_event(scope_id, item.subject_ref)
        if task.capability_ref == "analysis_watch@1":
            return await self._analysis_current(item, latest)
        return latest is not None and latest.payload["data_version"] == item.data_version

    async def _calendar_current(self, item, sub, evidence, now):
        # The verifier persisted the references it resolved from this particular decision.
        changes = await self.repos.subscriptions.changes(item.scope_id, item.subject_ref, sub.created_at, now,
                                                        source="mock_calendar")
        cited = {ref.removeprefix("event:") for ref in evidence if ref.startswith("event:")}
        referenced = [(e.payload["item_id"], e.payload["version"]) for e in changes if e.id in cited]
        newest = {}
        for event in changes:
            key, version = event.payload["item_id"], event.payload["version"]
            newest[key] = max(version, newest.get(key, 0))
        return len(referenced) == len(cited) and all(newest[key] == version for key, version in referenced)

    async def _analysis_current(self, item, latest):
        if latest is None:
            return False
        if latest.payload.get("kind") == "source_changed":
            return latest.payload["sha256"] == item.data_version
        # Some subjects also have unrelated external events. Read up to the observed high-water mark.
        version, after = None, 0
        while after < latest.seq:
            page = await self.repos.events.list(item.scope_id, after_seq=after)
            for event in page:
                if (event.seq <= latest.seq and event.type == EventType.EXTERNAL and event.subject_ref == item.subject_ref
                        and event.payload.get("kind") == "source_changed"):
                    version = event.payload["sha256"]
            after = page[-1].seq
        return version == item.data_version

    async def inbox(self, scope_id):
        return [i for i in await self.repos.delivery.list(scope_id) if i.mode != "silent"
                and i.status != "cancelled" and await self.accessible(scope_id, i)]

    async def reserve_notice(self, request):
        from ..contracts import NoticeReservation
        items = await self.repos.delivery.list(request.scope_id)
        item = next((i for i in items if i.result_ref == request.result_ref), None)
        if item is None or not await self.accessible(request.scope_id, item):
            return NoticeReservation(outcome="suppressed", dedup_key=request.dedup_key, reason="result unavailable")
        if request.dedup_key != item.dedup_key:
            raise ValueError("notice must use the result's cross-channel dedup key")
        return await self.repos.delivery.reserve_notice(item, request.channel, self.clock())

    async def materials(self, task):
        out = []
        recent = await self.repos.tasks.recent_conversation(task.scope_id, task.conversation_id) if task.conversation_id else []
        sources = {ref for t in [task, *recent] for ref in [t.subject_ref, *t.input_refs] if ref}
        text = "\n".join(t.goal for t in [task, *recent]).casefold()
        for item in await self.repos.delivery.list(task.scope_id):
            if item.mode == "silent" or item.status == "cancelled" or not await self.accessible(task.scope_id, item):
                continue
            sub = await self.repos.subscriptions.get(task.scope_id, item.subscription_id)
            words = item.keywords
            if sub.capability_ref not in WATCH_CAPABILITIES:
                words = [Path(item.subject_ref).stem, *sub.focus, *item.keywords]
            related = item.subject_ref in sources or any(word and word.casefold() in text for word in words)
            if not related:
                continue
            out.append(InjectedMaterial(source_task_id=item.task_id, count=1, topic=item.topic[:40],
                importance="high" if item.score >= 7 else "medium" if item.score >= 4 else "low", ref_ids=[item.id]))
        return out[:3]

    async def read(self, scope_id, item_id, *, explicit=False):
        item = await self.repos.delivery.get(scope_id, item_id)
        if not await self.accessible(scope_id, item, explicit=explicit):
            raise NotFound("background result is unavailable, expired or superseded")
        task = await self.repos.tasks.get(scope_id, item.task_id)
        content = await self.artifacts.read(item.result_ref, Actor(scope_id=scope_id, actor_id=task.actor_id))
        result = {"notice": "requested" if explicit else "read", "valid_until": item.valid_until.isoformat()}
        body = json.loads(content.content)
        if task.capability_ref in WATCH_CAPABILITIES:
            return {**result, "decision": body}
        return {**result, "data_version": item.data_version, "card": body}

    async def mention(self, scope_id, item_id):
        item = await self.repos.delivery.get(scope_id, item_id)
        if not await self.accessible(scope_id, item):
            raise NotFound("background result is unavailable, expired or superseded")
        notice = await self.repos.delivery.reserve_notice(item, "main", self.clock())
        return {"notice": notice.outcome, "valid_until": item.valid_until.isoformat()}
