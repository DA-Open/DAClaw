"""Validate a data card and replay the action that produced every statistic."""
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...contracts import ActionStatus, AuthorizedCall, CheckResult, VerificationStatus, canonical_json
from ...tasks.proactive_controller import active, resources_allowed


class Inference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: Literal["grain", "meaning", "unit"]
    value: str
    meaning_source: Literal["inferred"]
    confidence: float = Field(ge=0, le=1)


class DataCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1"]
    subject_ref: str
    data_version: str
    meaning_source: Literal["computed"]
    profile_action_id: str
    profile: dict
    inferences: list[Inference]


def result(check_id, ok, detail):
    return CheckResult(id=check_id, status=VerificationStatus.PASSED if ok else VerificationStatus.FAILED, detail=detail)


async def read_card(ctx):
    raw, problem = await ctx.read_text("data_card")
    if problem:
        raise ValueError(problem)
    return DataCard.model_validate_json(raw)


class DataCardChecks:
    def __init__(self, repos, policy, clock, profile):
        self.repos, self.policy, self.clock = repos, policy, clock
        self.profile = profile

    async def schema(self, spec, ctx):
        try:
            card = await read_card(ctx)
        except ValueError as exc:
            return result(spec.id, False, str(exc))
        event = await self.repos.subscriptions.event(ctx.task.scope_id, ctx.task.trigger_ref)
        ok = card.subject_ref == ctx.task.subject_ref and card.data_version == event.payload["data_version"]
        return result(spec.id, ok, "card schema and source version checked")

    async def replay(self, spec, ctx):
        try:
            card = await read_card(ctx)
        except ValueError as exc:
            return result(spec.id, False, str(exc))
        sub = await self.repos.subscriptions.get(ctx.task.scope_id, ctx.task.subscription_id)
        if not active(sub, self.clock()) or not resources_allowed(sub, self.policy):
            return result(spec.id, False, "subscription or resource permission revoked")
        latest = await self.repos.subscriptions.latest_event(ctx.task.scope_id, ctx.task.subject_ref)
        if latest is None or latest.payload["data_version"] != card.data_version:
            return result(spec.id, False, "source version is no longer current")
        actions = await self.repos.actions.list_for_task(ctx.task.id)
        action = next((a for a in actions if a.id == card.profile_action_id), None)
        if action is None or action.tool != "data.profile" or not action.read_only or action.status != ActionStatus.SUCCEEDED:
            return result(spec.id, False, "profile must reference a successful read-only action of this task")
        args = json.loads((await ctx.artifacts.read(action.args_ref, ctx.actor)).content)
        stored = json.loads((await ctx.artifacts.read(action.result_ref, ctx.actor)).content)
        if Path(args["path"]).resolve() != Path(ctx.task.subject_ref).resolve():
            return result(spec.id, False, "profile action read a different source")
        if canonical_json(stored["data"]["profile"]) != canonical_json(card.profile):
            return result(spec.id, False, "card statistics differ from the recorded action")
        call = AuthorizedCall(
            scope_id=ctx.task.scope_id, task_id=ctx.task.id, attempt_id=action.attempt_id,
            action_id=action.id, tool=action.tool, logical_key=action.logical_key, args=args,
        )
        try:
            replay = await self.profile.replay(call)
        except (TimeoutError, ValueError) as exc:
            return result(spec.id, False, f"could not replay the profile: {exc}")
        return result(spec.id, canonical_json(replay) == canonical_json(card.profile), "replayed profile compared with every computed value")
