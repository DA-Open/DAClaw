"""Freshness, permission checks and pointer-only handoff to Main."""
import json
from pathlib import Path

from ..contracts import Actor, InjectedMaterial, NotFound, TaskStatus, utcnow
from ..tasks.proactive_controller import active, resources_allowed
from .rules import delivery_item


class DeliveryService:
    def __init__(self, repos, artifacts, policy, *, clock=utcnow):
        self.repos, self.artifacts, self.policy, self.clock = repos, artifacts, policy, clock

    async def prepare(self, task, outcome):
        if not task.subscription_id or outcome.status != TaskStatus.SUCCEEDED:
            return None
        sub = await self.repos.subscriptions.get(task.scope_id, task.subscription_id)
        records = await self.repos.subscriptions.decisions(task.scope_id, sub.id)
        record = next(r for r in records if r.task_id == task.id)
        card = json.loads((await self.artifacts.read(outcome.result_ref,
            Actor(scope_id=task.scope_id, actor_id=task.actor_id))).content)
        return delivery_item(task.model_copy(update={"result_ref": outcome.result_ref}), sub, record,
                             card, self.clock(), allowed=resources_allowed(sub, self.policy))

    async def accessible(self, scope_id, item, *, explicit=False):
        now = self.clock()
        sub = await self.repos.subscriptions.get(scope_id, item.subscription_id)
        if not active(sub, now) or not resources_allowed(sub, self.policy):
            return False
        from ..contracts import AgentRole
        if not self.policy.check_path(Path(item.subject_ref).resolve(), scope_id=scope_id, role=AgentRole.MAIN).allowed:
            return False
        task = await self.repos.tasks.get(scope_id, item.task_id)
        if task.status != TaskStatus.SUCCEEDED or task.result_ref != item.result_ref:
            return False
        verified = await self.repos.verifications.list_for_task(task.id)
        if not any(v.status == "passed" and v.result_ref == item.result_ref for v in verified):
            return False
        if explicit:
            return True
        latest = await self.repos.subscriptions.latest_event(scope_id, item.subject_ref)
        return (item.valid_until > now and task.result_expires_at is not None and task.result_expires_at > now
                and latest is not None and latest.payload["data_version"] == item.data_version)

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
            if not await self.accessible(task.scope_id, item):
                continue
            sub = await self.repos.subscriptions.get(task.scope_id, item.subscription_id)
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
        if not explicit:
            notice = await self.repos.delivery.reserve_notice(item, "main", self.clock())
            if notice.outcome != "reserved":
                return {"notice": notice.outcome, "detail": "This result was already mentioned; do not repeat it."}
        task = await self.repos.tasks.get(scope_id, item.task_id)
        content = await self.artifacts.read(item.result_ref, Actor(scope_id=scope_id, actor_id=task.actor_id))
        return {"notice": "requested" if explicit else "reserved", "valid_until": item.valid_until.isoformat(),
                "data_version": item.data_version, "card": json.loads(content.content)}
