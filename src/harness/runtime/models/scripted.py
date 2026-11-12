"""A fake model that answers from a script, for tests and offline runs.

Each script entry is either a fixed ``AssistantStep`` or a function of the
``ContextBundle`` that returns one (so a test can react to what the Runner
sent, e.g. "call submit_result when no tools are offered"). An entry that is
an exception is raised instead, to test model failures.

Usage defaults to one model call and a token count from the message size,
so budget tests behave like a real model without a network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from typing import Any

from ...contracts import AssistantStep, CandidateResult, ContextBundle, StepKind, ToolCall, Usage, new_id
from ..messages import Message, ToolCallPart
from .base import estimate_tokens

ScriptEntry = AssistantStep | BaseException | Callable[[ContextBundle], AssistantStep]


class ScriptExhausted(AssertionError):
    pass


class ScriptedModelAdapter:
    protocol = "scripted"

    def __init__(
        self,
        script: Iterable[ScriptEntry] = (),
        *,
        model: str = "scripted",
        max_output_tokens: int = 1024,
        output_tokens: int = 20,
        repeat_last: bool = False,
    ) -> None:
        self.model = model
        self.max_output_tokens = max_output_tokens
        self._script = list(script)
        self._output_tokens = output_tokens
        self._repeat_last = repeat_last
        self.calls: list[ContextBundle] = []
        self.max_tokens_seen: list[int | None] = []
        self._lock = asyncio.Lock()

    async def next_step(
        self, context: ContextBundle, *, max_output_tokens: int | None = None
    ) -> AssistantStep:
        async with self._lock:
            index = len(self.calls)
            self.calls.append(context)
            self.max_tokens_seen.append(max_output_tokens)
            if index < len(self._script):
                entry = self._script[index]
            elif self._repeat_last and self._script:
                entry = self._script[-1]
            else:
                raise ScriptExhausted(f"scripted model has no step {index + 1}")
        if isinstance(entry, BaseException):
            raise entry
        step = entry(context) if callable(entry) else entry
        if step.usage == Usage():
            step = step.model_copy(
                update={
                    "usage": Usage(
                        model_calls=1,
                        input_tokens=estimate_tokens(context.messages, context.tool_specs),
                        output_tokens=self._output_tokens,
                    )
                }
            )
        if step.message is None:
            step = step.model_copy(update={"message": _message_for(step)})
        return step


def tool_step(*calls: tuple[str, dict[str, Any]] | ToolCall, text: str = "") -> AssistantStep:
    """``tool_step(("files.read", {"path": "a.md"}))``"""
    tool_calls = [
        c if isinstance(c, ToolCall) else ToolCall(tool=c[0], args=c[1], native_call_id=new_id("call"))
        for c in calls
    ]
    tool_calls = [c if c.native_call_id else c.model_copy(update={"native_call_id": new_id("call")}) for c in tool_calls]
    step = AssistantStep(kind=StepKind.TOOL_CALLS, tool_calls=tool_calls, model="scripted")
    if text:
        step = step.model_copy(update={"message": _message_for(step, text)})
    return step


def result_step(summary: str, artifacts: dict[str, str] | None = None, evidence: list[str] | None = None) -> AssistantStep:
    return AssistantStep(
        kind=StepKind.CANDIDATE_RESULT,
        candidate=CandidateResult(
            summary=summary,
            artifact_refs=dict(artifacts or {}),
            evidence_refs=list(evidence or []),
        ),
        model="scripted",
    )


def _message_for(step: AssistantStep, text: str = "") -> dict[str, Any]:
    if step.kind is StepKind.CANDIDATE_RESULT and step.candidate is not None:
        return Message(role="assistant", content=step.candidate.summary).to_dict()
    return Message(
        role="assistant",
        content=text,
        tool_calls=[
            ToolCallPart(id=c.native_call_id or new_id("call"), name=c.tool, args=c.args)
            for c in step.tool_calls
        ],
    ).to_dict()
