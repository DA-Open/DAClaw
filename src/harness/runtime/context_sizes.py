"""Size of each part of a model input (the "context bill" for the harness).

The Runner records one ``context_built`` event per model call with these
sizes, so "why did this call cost so much" can be answered from events alone,
without loading the stored input. Sections follow the order ContextBuilder
uses (spec 6.1) plus what the loop adds afterwards.

Counts use the same estimate as the budget reservation (about 3 characters
per token), so the sections add up to the reserved input size. Real usage per
call is in the ``model_call`` event.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from ..contracts import ContextSection, ContextSizes, ToolSpec, canonical_json

# Section names, in the order they appear in the input.
SYSTEM = "system"  # profile instructions + harness rules
TASK_BRIEF = "task_brief"  # goal, acceptance, inputs, progress, artifacts
HISTORY = "history"  # model turns: text and tool calls
TOOL_RESULTS = "tool_results"
BUDGET_REMINDERS = "budget_reminders"
INJECTED = "injected"  # background-result pointers (P2), user messages added later
TOOL_SPECS = "tool_specs"

ORDER = (SYSTEM, TASK_BRIEF, HISTORY, TOOL_RESULTS, BUDGET_REMINDERS, INJECTED, TOOL_SPECS)


def _tokens(chars: int) -> int:
    return chars // 3


def _section_of(index: int, message: dict[str, Any], brief_index: int | None) -> str:
    role = message.get("role")
    kind = message.get("kind")
    if role == "system":
        return SYSTEM
    if role == "tool":
        return TOOL_RESULTS
    if role == "assistant":
        return HISTORY
    if kind == "budget_reminder":
        return BUDGET_REMINDERS
    if index == brief_index:
        return TASK_BRIEF
    return INJECTED


def measure(messages: Sequence[dict[str, Any]], tools: Iterable[ToolSpec] = ()) -> ContextSizes:
    """Split one model input into sections and size each one."""
    brief_index = next(
        (i for i, m in enumerate(messages) if m.get("role") == "user" and not m.get("kind")),
        None,
    )
    chars: dict[str, int] = {}
    counts: dict[str, int] = {}
    for i, message in enumerate(messages):
        name = _section_of(i, message, brief_index)
        chars[name] = chars.get(name, 0) + len(canonical_json(message))
        counts[name] = counts.get(name, 0) + 1
    tool_list = list(tools)
    if tool_list:
        chars[TOOL_SPECS] = sum(len(canonical_json(t.model_dump(mode="json"))) for t in tool_list)
        counts[TOOL_SPECS] = len(tool_list)

    sections = [
        ContextSection(name=name, items=counts[name], chars=chars[name], est_tokens=_tokens(chars[name]))
        for name in ORDER
        if name in chars
    ]
    # Wrapping the message list in a JSON array adds brackets and commas; the
    # budget estimate counts them, so the total does too.
    total_chars = len(canonical_json(list(messages))) + chars.get(TOOL_SPECS, 0)
    return ContextSizes(sections=sections, total_chars=total_chars, est_tokens=total_chars // 3 + 1)
