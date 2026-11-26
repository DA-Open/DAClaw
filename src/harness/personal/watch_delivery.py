"""Translate a verified watch decision into delivery and memory records."""
from datetime import timedelta

from ..contracts import MemoryKind, MemoryStatus, TaskStatus, canonical_json
from ..delivery.rules import quiet
from ..proactive_types import InboxItem
from ..tasks.proactive_controller import active
from .decision import read_decision
from .memory import MemoryService
from .observation import WATCH_CAPABILITIES


def watch_item(task, sub, record, decision, recompute, now, *, keywords, allowed):
    snapshot = record.input_snapshot
    failed_recompute = task.capability_ref == "analysis_watch@1" and bool(recompute.error)
    if task.capability_ref == "personal_watch@1":
        version = snapshot["until"]
        key = f"{sub.id}:" + "+".join(sorted(snapshot["changes"]))
    else:
        # A failed C3 recompute carries no digest, but admission recorded the triggering version.
        version = snapshot["new_sha256"]
        key = f"{sub.id}:{snapshot['baseline_ref']}:{version}"
        if failed_recompute:
            key += ":error"

    if decision.decision == "propose_notify":
        mode, topic = "notify", decision.draft.title
        score = 8 if failed_recompute else 7
        valid_until = decision.valid_until
    else:
        mode, topic, score, keywords = "silent", decision.observation_digest[:80], 0, []
        valid_until = now + timedelta(seconds=sub.result_ttl_seconds)
    if not allowed or not active(sub, now) or quiet(sub, now) or sub.delivery_mode == "silent":
        mode = "silent"
    return InboxItem(
        scope_id=task.scope_id, subscription_id=sub.id, task_id=task.id, result_ref=task.result_ref,
        subject_ref=sub.subject_ref, data_version=version, dedup_key=key, created_at=now, mode=mode,
        topic=topic, score=score, keywords=keywords, valid_until=valid_until,
    )


class WatchAfterCommit:
    def __init__(self, memory: MemoryService, artifacts):
        self.memory = memory
        self.artifacts = artifacts

    async def __call__(self, task, outcome):
        if task.capability_ref not in WATCH_CAPABILITIES or outcome.status != TaskStatus.SUCCEEDED:
            return
        # Revocation can win the commit race after the worker produced a successful outcome.
        if task.status != TaskStatus.SUCCEEDED:
            return
        decision = await read_decision(self.artifacts, task, task.result_ref)
        body = decision.model_dump(mode="json", include={"decision", "rationale", "evidence_refs"})
        await self.memory.remember(
            task.scope_id, "harness", MemoryKind.EPISODE, canonical_json(body), status=MemoryStatus.CONFIRMED,
            summary=decision.observation_digest, tags=["watch", task.subscription_id], source_ref=f"task:{task.id}",
            subject_ref=None, expires_at=task.created_at + timedelta(days=3),
        )
        for index, proposal in enumerate(decision.memory_proposals):
            await self.memory.propose(task.scope_id, proposal.kind, proposal.text, summary=proposal.summary,
                tags=[], source_ref=f"task:{task.id}:proposal:{index}")
