"""Test doubles for modules other PRs implement (ToolGateway: PR-E;
Verifier and ArtifactStore: PR-F), plus helpers to set up a claimed task.

The fake gateway follows the ownership table in ``src/harness/README.md``:
it registers the action and reserves the tool budget in one step through
``ActionRepository.register``, so Runner tests see the budget behaviour the
real gateway will give.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from harness.contracts import (
    Acceptance,
    Action,
    ActionStatus,
    Actor,
    AgentProfile,
    AgentRole,
    Artifact,
    ArtifactContent,
    ArtifactInput,
    ArtifactRef,
    CandidateResult,
    CheckResult,
    ErrorCode,
    ExecutionContext,
    Limits,
    RunLane,
    Task,
    TaskError,
    TaskOrigin,
    TaskRequest,
    ToolCall,
    ToolOutcome,
    ToolSpec,
    ToolStatus,
    TriggerType,
    Usage,
    VerificationResult,
    VerificationStatus,
)
from harness.storage.memory import InMemoryRepositories

T0 = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)
ACTOR = Actor(actor_id="u1", scope_id="s1")
TTL = 3600


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


MAIN_PROFILE = AgentProfile(
    role=AgentRole.MAIN,
    version="1",
    instructions_ref="main@1",
    tool_policy_ref="main_tools@1",
    default_lane=RunLane.FOREGROUND,
    default_delivery_mode="reply",
)

READ = ToolSpec(
    name="files.read",
    description="Read a text file.",
    input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    read_only=True,
    side_effects=False,
)
WRITE = ToolSpec(
    name="artifacts.write",
    description="Write an artifact.",
    input_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}, "content": {"type": "string"}},
        "required": ["name", "content"],
    },
    read_only=False,
    side_effects=False,
)


async def claimed_task(
    repos: InMemoryRepositories,
    *,
    limits: Limits | None = None,
    acceptance: Acceptance | None = None,
    goal: str = "Summarise notes.md into a draft",
    now: datetime = T0,
) -> tuple[Task, ExecutionContext]:
    request = TaskRequest(
        request_key="k1",
        goal=goal,
        capability_ref="document_draft@1",
        limits=limits or Limits(),
        acceptance=acceptance or Acceptance(required_artifacts=["draft"]),
    )
    origin = TaskOrigin(
        agent_role=AgentRole.MAIN,
        agent_profile_ref="main@1",
        run_lane=RunLane.FOREGROUND,
        trigger_type=TriggerType.USER,
    )
    created = await repos.tasks.create(Task.new(request, ACTOR, origin, now=now))
    lease = await repos.tasks.claim("w1", [RunLane.FOREGROUND], TTL, now)
    assert lease is not None
    task = await repos.tasks.get(ACTOR.scope_id, created.task.id)
    execution = ExecutionContext(
        lease=lease, actor=ACTOR, agent_role=AgentRole.MAIN, run_lane=RunLane.FOREGROUND
    )
    return task, execution


class MemoryArtifactStore:
    def __init__(self, repos: InMemoryRepositories) -> None:
        self._repos = repos
        self.content: dict[str, bytes] = {}

    async def put(self, artifact: ArtifactInput) -> ArtifactRef:
        stored = Artifact(
            scope_id=artifact.scope_id,
            task_id=artifact.task_id,
            name=artifact.name,
            uri=f"mem://{len(self.content)}",
            media_type=artifact.media_type,
            content_hash="sha256:" + hashlib.sha256(artifact.content).hexdigest(),
            size_bytes=len(artifact.content),
            source_refs=list(artifact.source_refs),
        )
        await self._repos.artifacts.add(stored)
        self.content[stored.id] = artifact.content
        return ArtifactRef.of(stored)

    async def read(self, reference, actor: Actor, *, offset: int = 0, length: int | None = None) -> ArtifactContent:
        ref_id = reference if isinstance(reference, str) else reference.id
        artifact = await self._repos.artifacts.get(actor.scope_id, ref_id)
        data = self.content[ref_id]
        end = None if length is None else offset + length
        return ArtifactContent(ref=ArtifactRef.of(artifact), content=data[offset:end], offset=offset)

    def json(self, ref_id: str) -> Any:
        import json

        return json.loads(self.content[ref_id])


Handler = Callable[[ToolCall], "ToolOutcome | dict[str, Any]"]


class FakeGateway:
    """Registers each call as an action (reserving one tool call), then
    answers from ``handlers`` by tool name. A handler may return a
    ``ToolOutcome``; a dict becomes a succeeded outcome. ``artifacts.write``
    without a handler stores its content as an artifact."""

    def __init__(
        self,
        repos: InMemoryRepositories,
        artifacts: MemoryArtifactStore,
        handlers: dict[str, Handler] | None = None,
    ) -> None:
        self._repos = repos
        self._artifacts = artifacts
        self.handlers = handlers or {}
        self.calls: list[ToolCall] = []

    async def invoke(self, execution: ExecutionContext, call: ToolCall) -> ToolOutcome:
        assert call.logical_key, "the Runner must set logical_key"
        self.calls.append(call)
        action = Action(
            task_id=execution.lease.task_id,
            attempt_id=execution.lease.attempt_id,
            logical_key=call.logical_key,
            tool=call.tool,
            request_hash=call.request_hash(),
            read_only=call.tool == READ.name,
        )
        registration = await self._repos.actions.register(action, reserve=Usage(tool_calls=1))
        if registration.action is None:
            return ToolOutcome(
                status=ToolStatus.DENIED,
                error=TaskError(code=ErrorCode.BUDGET_EXHAUSTED, message="no tool calls left"),
            )
        action_id = registration.action.id
        handler = self.handlers.get(call.tool)
        result = handler(call) if handler is not None else {}
        if isinstance(result, ToolOutcome):
            outcome = result.model_copy(update={"action_id": result.action_id or action_id})
        else:
            outcome = await self._default(execution, call, action_id, result)
        if registration.created:
            if outcome.status is not ToolStatus.UNKNOWN:
                await self._repos.usage.settle(action_id, Usage(tool_calls=1), T0)
            status = {
                ToolStatus.SUCCEEDED: ActionStatus.SUCCEEDED,
                ToolStatus.UNKNOWN: ActionStatus.UNKNOWN,
            }.get(outcome.status, ActionStatus.FAILED)
            await self._repos.actions.update(action_id, status, now=T0)
        return outcome

    async def _default(self, execution, call, action_id, result: dict[str, Any]) -> ToolOutcome:
        refs = []
        if call.tool == WRITE.name:
            ref = await self._artifacts.put(
                ArtifactInput(
                    scope_id=execution.actor.scope_id,
                    task_id=execution.lease.task_id,
                    name=call.args.get("name"),
                    media_type="text/markdown",
                    content=str(call.args.get("content", "")).encode(),
                )
            )
            refs.append(ref.id)
        return ToolOutcome(
            action_id=action_id,
            status=ToolStatus.SUCCEEDED,
            summary=result.get("summary", f"{call.tool} ok"),
            artifact_refs=refs,
        )


class FakeVerifier:
    """Passes when every required artifact name is in the candidate, unless
    ``status`` forces a result. Stores the verification like the real one."""

    def __init__(self, repos: InMemoryRepositories, status: VerificationStatus | None = None) -> None:
        self._repos = repos
        self.status = status
        self.seen: list[CandidateResult] = []

    async def verify(self, result: CandidateResult, task: Task) -> VerificationResult:
        self.seen.append(result)
        missing = [n for n in task.acceptance.required_artifacts if n not in result.artifact_refs]
        status = self.status or (VerificationStatus.FAILED if missing else VerificationStatus.PASSED)
        result_ref = next(iter(result.artifact_refs.values()), "none")
        check = CheckResult(id="artifact_readable", status=status, detail=f"missing: {missing}" if missing else "")
        verification = VerificationResult(
            task_id=task.id,
            result_ref=result_ref,
            checker_version="fake@1",
            status=status,
            checks=[check],
        )
        return await self._repos.verifications.add(verification)
