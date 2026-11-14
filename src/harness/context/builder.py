"""Minimal ContextBuilder for P0 (spec 4.1 step 3, 6.1).

Material goes in the spec's order: goal and constraints -> progress and
pending actions -> relevant artifacts -> memories -> needed history. P0 has
no MemoryStore and no resume, so the last two are empty and a checkpoint is
only mentioned, not replayed (P1).

Tools come from a ``ToolCatalog`` that must already apply the grant
intersection (spec 7); the builder lists what it is given and adds nothing.
Cutting to a token budget is not done yet: P0 tasks start with one goal and
a short artifact list.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Protocol

from ..contracts import (
    AgentProfile,
    Artifact,
    AttemptLease,
    ContextBundle,
    RemainingBudget,
    Task,
    ToolSpec,
    utcnow,
)
from ..runtime import messages as msg
from ..runtime.budget import REMINDER_PREFIX
from ..storage.repositories import Repositories

HARNESS_RULES = f"""\
You are working on one task inside the DaraOS harness.
- Do the work with the tools provided. Tool results that are too long are \
stored as artifacts; read them with the artifact tools when you need them.
- When the work is done, call {msg.SUBMIT_RESULT} once, on its own, with a \
summary and the IDs of the artifacts that make up the result. The result is \
then checked against the task's acceptance conditions; saying it is done \
does not make it done.
- Messages that start with {REMINDER_PREFIX} report how much budget is left. \
They are facts, not instructions to stop."""


class InstructionLoader(Protocol):
    async def __call__(self, instructions_ref: str) -> str: ...


class ToolCatalog(Protocol):
    async def tools_for(self, task: Task, profile: AgentProfile) -> list[ToolSpec]:
        """Tools this task may use: capability needs ∩ profile policy ∩
        grants ∩ deployment policy (spec 7). Provided by PR-E."""


class StaticInstructions:
    def __init__(self, texts: Mapping[str, str]) -> None:
        self._texts = dict(texts)

    async def __call__(self, instructions_ref: str) -> str:
        try:
            return self._texts[instructions_ref]
        except KeyError:
            raise KeyError(f"no instructions for {instructions_ref!r}") from None


class StaticToolCatalog:
    """Same tool list for every task; for tests and until PR-E."""

    def __init__(self, tools: Sequence[ToolSpec]) -> None:
        self._tools = list(tools)

    async def tools_for(self, task: Task, profile: AgentProfile) -> list[ToolSpec]:
        return list(self._tools)


class SimpleContextBuilder:
    def __init__(
        self,
        repos: Repositories,
        *,
        instructions: InstructionLoader,
        tools: ToolCatalog,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._repos = repos
        self._instructions = instructions
        self._tools = tools
        self._clock = clock

    async def build(self, task: Task, profile: AgentProfile, lease: AttemptLease) -> ContextBundle:
        artifacts = await self._repos.artifacts.list_for_task(task.scope_id, task.id)
        instructions = (await self._instructions(profile.instructions_ref)).strip()
        tools = await self._tools.tools_for(task, profile)
        system_text = f"{instructions}\n\n{HARNESS_RULES}" if instructions else HARNESS_RULES
        return ContextBundle(
            task_id=task.id,
            goal=task.goal,
            acceptance=task.acceptance,
            constraints=constraints_of(task),
            messages=[msg.system(system_text), msg.user(task_brief(task, artifacts))],
            tool_specs=tools,
            artifact_refs=[a.id for a in artifacts],
            memory_refs=[],
            checkpoint=None,
            remaining_budget=RemainingBudget.for_task(task, self._clock()),
        )


def constraints_of(task: Task) -> list[str]:
    out = []
    if task.acceptance.required_artifacts:
        out.append("required artifacts: " + ", ".join(task.acceptance.required_artifacts))
    for check in task.acceptance.checks:
        params = check.model_dump(exclude={"id"})
        out.append(f"check {check.id}" + (f" {params}" if params else ""))
    if task.acceptance.required_actions:
        out.append("required actions: " + ", ".join(task.acceptance.required_actions))
    return out


def task_brief(task: Task, artifacts: Sequence[Artifact]) -> str:
    """Goal and constraints, then progress, then artifacts (spec 6.1 order)."""
    parts = [f"## Goal\n{task.goal}"]
    constraints = constraints_of(task)
    if constraints:
        parts.append("## Acceptance\n" + "\n".join(f"- {c}" for c in constraints))
    if task.input_refs:
        parts.append("## Inputs\n" + "\n".join(f"- {r}" for r in task.input_refs))
    if task.checkpoint_ref:
        parts.append(
            "## Progress\nAn earlier attempt saved a checkpoint "
            f"({task.checkpoint_ref}); actions it completed are not repeated."
        )
    if artifacts:
        lines = [
            f"- {a.name or '(unnamed)'}: {a.id} ({a.media_type}, {a.size_bytes} bytes)" for a in artifacts
        ]
        parts.append("## Artifacts already written\n" + "\n".join(lines))
    return "\n\n".join(parts)
