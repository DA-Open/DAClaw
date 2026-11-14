"""Provider-neutral transcript used in ``ContextBundle.messages``.

The Runner and ContextBuilder only write these shapes; each model adapter
converts them to its wire format and back. Keeping one format means a task
can switch model or provider between attempts without rewriting history.

A message is a plain dict (so it fits ``ContextBundle.messages``), validated
through :class:`Message`:

* ``system`` / ``user``: ``content`` text.
* ``assistant``: optional ``content`` text, ``tool_calls``, the model's
  ``reasoning`` text if it returned any, and ``native``: the provider's own
  output, which the same protocol sends back unchanged (Anthropic thinking
  signatures, Responses reasoning items) so tool-call pairing keeps working.
* ``tool``: the answer to one tool call, by ``tool_call_id``.

Every tool call in an assistant message gets exactly one ``tool`` message
before the next model call; providers reject the request otherwise.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..contracts import ToolSpec

# The model ends its work by calling this tool (or by answering in plain
# text). The adapter turns the call into a ``candidate_result`` step; it never
# reaches ToolGateway.
SUBMIT_RESULT = "submit_result"

SUBMIT_RESULT_SPEC = ToolSpec(
    name=SUBMIT_RESULT,
    description=(
        "Finish the task. Call it alone, after the other tools have finished. "
        "List the artifacts you wrote by name and ID; the result is checked "
        "against the task's acceptance conditions."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "What was done and what the result is."},
            "artifacts": {
                "type": "object",
                "description": 'Artifacts that make up the result, name -> artifact ID, e.g. {"draft": "art_..."}.',
                "additionalProperties": {"type": "string"},
            },
            "evidence": {
                "type": "array",
                "description": "Artifact or action IDs that support the result.",
                "items": {"type": "string"},
            },
        },
        "required": ["summary"],
    },
    read_only=True,
    side_effects=False,
    idempotent=True,
)

Role = Literal["system", "user", "assistant", "tool"]


class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolCallPart(_Msg):
    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    # Set when the model's arguments were not a JSON object. ``raw_args`` keeps
    # what it sent so the transcript stays faithful.
    invalid: str | None = None
    raw_args: str | None = None


class Message(_Msg):
    role: Role
    content: str = ""
    tool_calls: list[ToolCallPart] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    is_error: bool = False
    reasoning: str | None = None
    native: dict[str, Any] | None = None
    # What wrote the message when it is not the user or the model, e.g.
    # "budget_reminder". Adapters ignore it.
    kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_defaults=True) | {"role": self.role}


def system(text: str) -> dict[str, Any]:
    return Message(role="system", content=text).to_dict()


def user(text: str, kind: str | None = None) -> dict[str, Any]:
    return Message(role="user", content=text, kind=kind).to_dict()


def tool_result(call_id: str, name: str, content: str, *, is_error: bool = False) -> dict[str, Any]:
    return Message(
        role="tool", tool_call_id=call_id, name=name, content=content, is_error=is_error
    ).to_dict()


def parse(messages: list[dict[str, Any]]) -> list[Message]:
    return [Message.model_validate(m) for m in messages]


def native_for(message: Message, protocol: str) -> dict[str, Any] | None:
    """The provider output stored on ``message`` if it came from ``protocol``."""
    if message.native and message.native.get("protocol") == protocol:
        return message.native
    return None
