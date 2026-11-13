"""A minimal in-memory ArtifactStore (spec 5.2, 3).

The real one (content in ``artifact_root``, row in ``artifacts``) belongs to
the results module (PR-F). Until it lands, this store is what the tool
gateway, the file connectors and ``TaskService.checkpoint`` need: content is
kept in memory, metadata goes through ``ArtifactRepository``, so scope checks
and per-task listings behave like the real store.

Like the real store, content is written first and the row second; a crash
between the two leaves unreferenced content, never a row without content.
"""

from __future__ import annotations

import hashlib

from ..contracts import (
    Actor,
    Artifact,
    ArtifactContent,
    ArtifactInput,
    ArtifactRef,
    new_id,
)
from .repositories import Repositories


class InMemoryArtifactStore:
    def __init__(self, repos: Repositories) -> None:
        self._repos = repos
        self.content: dict[str, bytes] = {}

    async def put(self, artifact: ArtifactInput) -> ArtifactRef:
        stored = Artifact(
            scope_id=artifact.scope_id,
            task_id=artifact.task_id,
            name=artifact.name,
            uri=f"mem://{new_id('blob')}",
            media_type=artifact.media_type,
            content_hash="sha256:" + hashlib.sha256(artifact.content).hexdigest(),
            size_bytes=len(artifact.content),
            source_refs=list(artifact.source_refs),
        )
        await self._repos.artifacts.add(stored)
        self.content[stored.id] = artifact.content
        return ArtifactRef.of(stored)

    async def read(
        self,
        reference: ArtifactRef | str,
        actor: Actor,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> ArtifactContent:
        ref_id = reference if isinstance(reference, str) else reference.id
        artifact = await self._repos.artifacts.get(actor.scope_id, ref_id)
        data = self.content[ref_id]
        end = None if length is None else offset + length
        return ArtifactContent(
            ref=ArtifactRef.of(artifact),
            content=data[offset:end],
            offset=offset,
        )
