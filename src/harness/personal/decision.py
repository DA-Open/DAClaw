"""Verification of proactive decisions against the task's recorded inputs and recomputation."""
import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Literal

import duckdb
from pydantic import AwareDatetime, Field, TypeAdapter, ValidationError, model_validator

from ..contracts import ActionStatus, Actor, CheckResult, EventType, MemoryKind, NotFound, _Model
from ..tools.connectors.data import ReadonlySource, file_digest, open_readonly, run_readonly
from .analysis_types import RecomputeResult
from .observation import read_baseline
from .recompute import INLINE_ROWS, _plain

QUERY_PATTERN = r"query:art_[0-9a-f]{32}"
EvidenceRef = Annotated[str, Field(pattern=rf"^(event:evt_[0-9a-f]{{32}}|memory:mem_[0-9a-f]{{32}}@[1-9]\d*|{QUERY_PATTERN})$")]
EvidenceRefs = Annotated[list[EvidenceRef], Field(min_length=1, max_length=12)]
Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]
ROWS = TypeAdapter(list[list[Any]])
TOLERANCE = Decimal("0.005")


class DecisionDraft(_Model):
    title: str = Field(min_length=1, max_length=80)
    body: str = Field(min_length=1, max_length=800)


class Claim(_Model):
    query_ref: str = Field(pattern=rf"^{QUERY_PATTERN}$")
    column: str = Field(min_length=1)
    old: Number
    new: Number
    delta: Number


class MemoryProposal(_Model):
    kind: Literal["preference", "fact"]
    summary: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=1000)
    evidence_refs: EvidenceRefs


class ProactiveDecision(_Model):
    schema_version: Literal["1"]
    decision: Literal["propose_notify", "silent"]
    rationale: str = Field(min_length=1, max_length=400)
    evidence_refs: EvidenceRefs
    draft: DecisionDraft | None = None
    valid_until: AwareDatetime | None = None
    claims: list[Claim] = Field(default_factory=list, max_length=6)
    observation_digest: str = Field(min_length=1, max_length=200)
    memory_proposals: list[MemoryProposal] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def delivery_fields(self):
        notify = self.decision == "propose_notify"
        if (self.draft is not None) != notify or (self.valid_until is not None) != notify:
            raise ValueError("a notification needs draft and valid_until; both must be null for silent")
        return self


def _actor(task):
    return Actor(scope_id=task.scope_id, actor_id=task.actor_id)


async def read_decision(artifacts, task, ref) -> ProactiveDecision:
    stored = await artifacts.read(ref, _actor(task))
    return ProactiveDecision.model_validate_json(stored.content)


async def read_recompute(repos, artifacts, task) -> RecomputeResult:
    # A filename alone does not identify the artifact produced by the trusted recomputer.
    cursor = 0
    while events := await repos.events.list(task.scope_id, task_id=task.id, after_seq=cursor):
        for event in events:
            if event.type == EventType.RECOMPUTE_RECORDED:
                stored = await artifacts.read(event.payload["recompute_ref"], _actor(task))
                return RecomputeResult.model_validate_json(stored.content)
        cursor = events[-1].seq
    raise ValueError("this task has no recorded recompute")


def _supports_claim(claim, query, columns):
    if claim.column not in columns:
        return False
    index = columns.index(claim.column)
    # C3 stores the baseline's CSV cells as strings and the new SQL values as JSON.
    # Require all three numbers to refer to the same row and column.
    for old_row, new_row in zip(query.old_values, query.new_values):
        if index >= min(len(old_row), len(new_row)):
            continue
        try:
            old, new = Decimal(str(old_row[index])), Decimal(str(new_row[index]))
        except InvalidOperation:
            continue
        if old.is_finite() and new.is_finite():
            numbers = zip((claim.old, claim.new, claim.delta), (old, new, new - old))
            if all(abs(Decimal(str(actual)) - expected) <= TOLERANCE for actual, expected in numbers):
                return True
    return False


class WatchChecks:
    def __init__(self, repos, artifacts, memories):
        self.repos = repos
        self.artifacts = artifacts
        self.memories = memories

    async def decision_schema(self, spec, ctx):
        return await self._check(spec, ctx, self._schema)

    async def decision_evidence(self, spec, ctx):
        return await self._check(spec, ctx, self._evidence)

    async def recompute_replay(self, spec, ctx):
        return await self._check(spec, ctx, self._replay)

    async def _check(self, spec, ctx, validate):
        # The Verifier receives empty submission evidence even when the decision cites a tool read.
        actions = await self.repos.actions.list_for_task(ctx.task.id)
        raw, problem = await ctx.read_text("decision")
        if problem:
            return CheckResult(id=spec.id, status="failed", detail=problem)
        try:
            decision = ProactiveDecision.model_validate_json(raw)
            refs = await validate(ctx, decision, actions)
        except ValidationError as exc:
            error = exc.errors()[0]
            location = ".".join(map(str, error["loc"])) or "decision"
            return CheckResult(id=spec.id, status="failed", detail=f"{location}: {error['msg']}")
        except (ValueError, NotFound, OSError, duckdb.Error) as exc:
            return CheckResult(id=spec.id, status="failed", detail=str(exc))
        return CheckResult(id=spec.id, status="passed", evidence_refs=refs)

    async def _schema(self, ctx, decision, _actions):
        if ctx.task.capability_ref == "personal_watch@1":
            if "claims" in decision.model_fields_set:
                raise ValueError("claims only apply to analysis_watch")
        else:
            recompute = await read_recompute(self.repos, self.artifacts, ctx.task)
            if recompute.error and decision.decision == "silent":
                raise ValueError("a recompute error requires a notification")
            if decision.decision == "propose_notify" and not recompute.error and not decision.claims:
                raise ValueError("an analysis notification requires at least one claim")
        return []

    async def _read_refs(self, actor, actions):
        refs = set()
        for action in actions:
            if action.tool == "memory.read" and action.status == ActionStatus.SUCCEEDED:
                stored = await self.artifacts.read(action.result_ref, actor)
                refs.add(json.loads(stored.content)["data"]["ref"])
        return refs

    async def _memory_evidence(self, scope, ref, context_refs, read_refs, baseline_ref):
        memory_id, version = ref.removeprefix("memory:").split("@")
        try:
            record = await self.memories.get(scope, memory_id)
        except NotFound:
            return False
        if record.id != memory_id or record.version != int(version):
            return False
        if record.kind == MemoryKind.PREFERENCE:
            return ref in context_refs or ref in read_refs
        if record.kind == MemoryKind.FACT:
            return ref in read_refs
        return record.kind == MemoryKind.ANALYSIS and ref == baseline_ref

    async def _evidence(self, ctx, decision, actions):
        task = ctx.task
        sub = await self.repos.subscriptions.get(task.scope_id, task.subscription_id)
        if decision.decision == "propose_notify":
            if not any(ref.startswith("event:") for ref in decision.evidence_refs):
                raise ValueError("a notification must cite a trigger event")
            if not task.created_at < decision.valid_until < task.created_at + timedelta(seconds=sub.result_ttl_seconds):
                raise ValueError("valid_until must fall strictly inside the task's result TTL")

        records = await self.repos.subscriptions.decisions(task.scope_id, sub.id)
        admission = next(row for row in records if row.task_id == task.id)
        if task.capability_ref == "personal_watch@1":
            allowed = set(admission.input_snapshot["changes"])
        else:
            recompute = await read_recompute(self.repos, self.artifacts, task)
            allowed = {f"event:{task.trigger_ref}", *(query.query_ref for query in recompute.queries)}
        attempt = await self.repos.tasks.get_attempt(task.scope_id, task.current_attempt_id)
        stored = await self.artifacts.read(attempt.context_ref, ctx.actor)
        context_refs = set(json.loads(stored.content)["context"]["memory_refs"])
        read_refs = await self._read_refs(ctx.actor, actions)
        refs = list(decision.evidence_refs)
        for proposal in decision.memory_proposals:
            refs.extend(proposal.evidence_refs)
        refs = list(dict.fromkeys(refs))
        for ref in refs:
            if ref.startswith("memory:"):
                valid = await self._memory_evidence(task.scope_id, ref, context_refs, read_refs, sub.baseline_ref)
            else:
                valid = ref in allowed
            if not valid:
                raise ValueError(f"unresolved evidence in this task: {ref}")
        return refs

    async def _replay(self, ctx, decision, _actions):
        task = ctx.task
        recompute = await read_recompute(self.repos, self.artifacts, task)
        if recompute.error:
            if decision.claims:
                raise ValueError("a recompute error cannot support numeric claims")
            return []

        sub = await self.repos.subscriptions.get(task.scope_id, task.subscription_id)
        records = await self.repos.subscriptions.decisions(task.scope_id, sub.id)
        admission = next(row for row in records if row.task_id == task.id)
        watched = Path(admission.input_snapshot["path"]).expanduser().resolve()
        if file_digest(watched) != recompute.new_sha256:
            raise ValueError("source changed again")
        claims = {}
        for claim in decision.claims:
            claims.setdefault(claim.query_ref, []).append(claim)
        if claims.keys() - {query.query_ref for query in recompute.queries}:
            raise ValueError("claim query_ref is outside this recompute")

        _, body = await read_baseline(self.memories, self.artifacts, ctx.actor, sub.baseline_ref)
        sources = [ReadonlySource(path=Path(source.path), kind=source.kind, primary=source.table, tables=source.tables)
                   for source in body.sources]
        con = open_readonly(sources)
        try:
            for query in recompute.queries:
                stored = await self.artifacts.read(query.query_ref.removeprefix("query:"), ctx.actor)
                evidence = json.loads(stored.content)
                for claim in claims.get(query.query_ref, []):
                    if not _supports_claim(claim, query, evidence["columns"]):
                        raise ValueError(f"claim differs from recompute: {claim.query_ref} {claim.column}")
                result = run_readonly(con, evidence["sql"])
                rows = [[_plain(cell) for cell in row] for row in result.rows[:INLINE_ROWS]]
                if (result.columns != evidence["columns"] or result.row_count != query.row_count
                        or ROWS.dump_python(rows, mode="json") != query.new_values):
                    raise ValueError(f"SQL replay differs from recompute: {query.query_ref}")
        finally:
            con.close()
        return []
