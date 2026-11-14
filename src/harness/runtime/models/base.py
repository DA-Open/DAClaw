"""Pieces shared by the model adapters.

An adapter does one model call: neutral transcript in, ``AssistantStep``
out. It reports usage; the Runner charges it. It never runs tools, retries a
whole step or decides whether the task is done.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from ...contracts import (
    AssistantStep,
    CandidateResult,
    ContextBundle,
    ErrorCode,
    HarnessError,
    StepKind,
    ToolCall,
    ToolSpec,
    Usage,
    canonical_json,
)
from ..messages import SUBMIT_RESULT, Message, ToolCallPart
from .config import WireProtocol


class ModelError(HarnessError):
    """A model call that produced no usable step.

    ``retryable`` is True for rate limits, server errors, timeouts and
    malformed answers; the Runner may try the step again within its budget.
    ``usage`` is set when the provider billed the call anyway.
    """

    code = ErrorCode.MODEL_ERROR

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        usage: Usage | None = None,
        **details: Any,
    ) -> None:
        super().__init__(message, status_code=status_code, **details)
        self.retryable = retryable
        self.status_code = status_code
        self.usage = usage


class ModelAdapter(Protocol):
    """``interfaces.ModelAdapter`` plus what the Runner reads for budgeting."""

    model: str
    max_output_tokens: int

    async def next_step(self, context: ContextBundle) -> AssistantStep: ...


def retryable_status(status: int | None) -> bool:
    return status is None or status in (408, 409, 429) or status >= 500


# -- token estimate ----------------------------------------------------------


def estimate_tokens(messages: Sequence[dict[str, Any]], tools: Iterable[ToolSpec] = ()) -> int:
    """Rough input size for budget reservations: about 3 characters per token.

    Errs high for English and is close for Chinese; real usage replaces it
    when the call is settled.
    """
    chars = len(canonical_json(list(messages)))
    chars += sum(len(canonical_json(t.model_dump(mode="json"))) for t in tools)
    return chars // 3 + 1


# -- tool names ----------------------------------------------------------------

_WIRE_NAME = re.compile(r"[^A-Za-z0-9_-]")


def wire_name(name: str) -> str:
    """Tool name as providers accept it (``^[A-Za-z0-9_-]{1,64}$``).

    ``files.read`` becomes ``files_read``. Deterministic, so a name in the
    history converts the same way on every call.
    """
    return _WIRE_NAME.sub("_", name)[:64]


class ToolNames:
    """Maps harness tool names to wire names and back for one request."""

    def __init__(self, tools: Iterable[ToolSpec], history: Iterable[str] = ()) -> None:
        self._to_wire: dict[str, str] = {}
        self._from_wire: dict[str, str] = {}
        for name in [t.name for t in tools] + list(history):
            self.add(name)

    def add(self, name: str) -> str:
        if name in self._to_wire:
            return self._to_wire[name]
        wire = wire_name(name)
        other = self._from_wire.get(wire)
        if other is not None and other != name:
            raise ValueError(f"tool names {other!r} and {name!r} map to the same wire name {wire!r}")
        self._to_wire[name] = wire
        self._from_wire[wire] = name
        return wire

    def to_wire(self, name: str) -> str:
        return self.add(name)

    def from_wire(self, wire: str) -> str:
        return self._from_wire.get(wire, wire)


def history_tool_names(messages: Iterable[Message]) -> list[str]:
    return [c.name for m in messages for c in m.tool_calls]


def tool_schema(spec: ToolSpec) -> dict[str, Any]:
    schema = dict(spec.input_schema) if spec.input_schema else {}
    schema.setdefault("type", "object")
    if schema["type"] == "object":
        schema.setdefault("properties", {})
    return schema


# -- parsing the answer -------------------------------------------------------


def parse_args(raw: Any) -> tuple[dict[str, Any], str | None, str | None]:
    """(args, problem, raw text). ``problem`` is None when the args are usable."""
    if isinstance(raw, dict):
        return raw, None, None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}, None, None
    text = raw if isinstance(raw, str) else str(raw)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"tool arguments are not valid JSON ({exc.msg} at position {exc.pos})", text
    if not isinstance(value, dict):
        return {}, "tool arguments must be a JSON object", text
    return value, None, None


class _SubmitArgs(BaseModel):
    summary: str
    artifacts: dict[str, str] = {}
    evidence: list[str] = []


def _candidate_from_submit(part: ToolCallPart) -> CandidateResult | None:
    if part.invalid:
        return None
    try:
        args = _SubmitArgs.model_validate(part.args)
    except ValidationError:
        return None
    return CandidateResult(
        summary=args.summary,
        artifact_refs=dict(args.artifacts),
        evidence_refs=list(args.evidence),
    )


def build_step(
    *,
    model: str,
    protocol: WireProtocol | str,
    text: str,
    calls: list[ToolCallPart],
    usage: Usage,
    reasoning: str | None = None,
    native: dict[str, Any] | None = None,
    native_ref: str | None = None,
) -> AssistantStep:
    """Turn one parsed answer into an ``AssistantStep``.

    * only ``submit_result`` with valid arguments -> ``candidate_result``;
    * any other tool calls -> ``tool_calls`` (a ``submit_result`` among them
      stays a tool call; the Runner answers it with an error);
    * no tool calls but text -> ``candidate_result`` with the text as summary;
    * nothing at all -> retryable ``ModelError``.
    """
    message = Message(
        role="assistant",
        content=text or "",
        tool_calls=calls,
        reasoning=reasoning or None,
        native=native,
    ).to_dict()
    if calls:
        if len(calls) == 1 and calls[0].name == SUBMIT_RESULT:
            candidate = _candidate_from_submit(calls[0])
            if candidate is not None:
                return AssistantStep(
                    kind=StepKind.CANDIDATE_RESULT,
                    candidate=candidate,
                    usage=usage,
                    model=model,
                    native_ref=native_ref,
                    message=message,
                )
        return AssistantStep(
            kind=StepKind.TOOL_CALLS,
            tool_calls=[ToolCall(tool=c.name, args=c.args, native_call_id=c.id) for c in calls],
            usage=usage,
            model=model,
            native_ref=native_ref,
            message=message,
        )
    if text and text.strip():
        return AssistantStep(
            kind=StepKind.CANDIDATE_RESULT,
            candidate=CandidateResult(summary=text.strip()),
            usage=usage,
            model=model,
            native_ref=native_ref,
            message=message,
        )
    raise ModelError(
        f"{protocol} model {model} returned neither text nor tool calls",
        retryable=True,
        usage=usage,
    )


def model_usage(input_tokens: int | None, output_tokens: int | None) -> Usage:
    return Usage(model_calls=1, input_tokens=max(input_tokens or 0, 0), output_tokens=max(output_tokens or 0, 0))


def drop_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [drop_none(v) for v in value]
    return value
