"""Restore durable tool progress while rebuilding current permissions."""

from pydantic import BaseModel, ValidationError

from ..contracts import Actor, AgentProfile, AttemptLease, Checkpoint, ContextBundle, HarnessError, NotFound, Task
from ..interfaces import ArtifactStore, ContextBuilder
from ..runtime.messages import Message


class CheckpointUnreadable(HarnessError):
    """A checkpoint cannot be restored; starting over would risk repeating work."""


class _SavedCheckpoint(BaseModel):
    checkpoint: Checkpoint
    messages: list[Message]


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
        return context.model_copy(update={
            "messages": context.messages[:1] + [message.to_dict() for message in saved.messages[1:]],
            "checkpoint": saved.checkpoint,
        })
