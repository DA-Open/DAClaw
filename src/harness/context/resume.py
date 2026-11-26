"""Restore durable tool progress while rebuilding current permissions."""

import re

from pydantic import BaseModel, ValidationError

from ..contracts import Actor, AgentProfile, AttemptLease, Checkpoint, ContextBundle, HarnessError, NotFound, Task
from ..interfaces import ArtifactStore, ContextBuilder
from ..runtime import messages as msg


class CheckpointUnreadable(HarnessError):
    """A checkpoint cannot be restored; starting over would risk repeating work."""


class _SavedCheckpoint(BaseModel):
    checkpoint: Checkpoint
    messages: list[msg.Message]


class ResumingContextBuilder:
    def __init__(self, builder: ContextBuilder, artifacts: ArtifactStore) -> None:
        self._builder = builder
        self._artifacts = artifacts

    async def build(self, task: Task, profile: AgentProfile, lease: AttemptLease) -> ContextBundle:
        context = await self._builder.build(task, profile, lease)
        if task.checkpoint_ref is None:
            return context
        try:
            artifact = await self._artifacts.read(
                task.checkpoint_ref, Actor(scope_id=task.scope_id, actor_id=task.actor_id)
            )
            saved = _SavedCheckpoint.model_validate_json(artifact.content)
        except (NotFound, ValidationError) as exc:
            raise CheckpointUnreadable(f"checkpoint {task.checkpoint_ref}: {exc}") from exc
        messages = context.messages[:1] + [message.to_dict() for message in saved.messages[1:]]
        if not saved.checkpoint.pending_calls and task.input_refs:
            # Match complete references in briefs and user prose. Prefer the full
            # name when two paths share a prefix, including a prefix ending in a space.
            refs = sorted(set(task.input_refs), key=len, reverse=True)
            literals = [re.escape(ref) or r"(?<=^- )$" for ref in refs]
            pattern = re.compile(r"(?<!\S)(?:" + "|".join(literals) + r")(?!\S)", re.MULTILINE)
            shown = {
                match.group() for message in saved.messages[1:] if message.role == "user"
                for match in pattern.finditer(message.content)
            }
            new_refs = [ref for ref in task.input_refs if ref not in shown]
            if new_refs:
                messages.append(msg.user("Additional inputs provided on resume:\n" + "\n".join(f"- {r}" for r in new_refs)))
        return context.model_copy(update={
            "messages": messages,
            "checkpoint": saved.checkpoint,
        })
