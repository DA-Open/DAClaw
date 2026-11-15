"""ArtifactStore backed by a local directory (spec 1.2, 3; P0).

Content is written to files under ``root``, then the ``artifacts`` row is
added through ``ArtifactRepository`` (spec: "完整内容存文件／对象存储"). A
deployment can swap this for object storage behind the same interface.

Layout: ``<root>/<scope_id>/<sha256[:2]>/<sha256>``. Files are named by
content hash, so identical content is stored once; each ``put`` still gets
its own artifact row (its own ID, name and task), because two artifacts with
the same bytes are still two results.

Writes are atomic: content goes to a temporary file in the same directory and
is renamed into place, so a crash never leaves a half-written file under the
final name. ``read`` checks the reader's scope through the repository before
touching the file, and re-checks the content hash so a changed or truncated
file is reported instead of returned.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from ..contracts import (
    Actor,
    Artifact,
    ArtifactContent,
    ArtifactInput,
    ArtifactRef,
    HarnessError,
    NotFound,
)
from ..storage.repositories import ArtifactRepository

_SAFE_SCOPE = re.compile(r"[^A-Za-z0-9_.-]")


class ArtifactIntegrityError(HarnessError):
    """The stored file no longer matches the hash recorded for it."""


class FileArtifactStore:
    """``interfaces.ArtifactStore`` writing to a local directory."""

    def __init__(self, root: Path | str, artifacts: ArtifactRepository) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._repo = artifacts

    @property
    def root(self) -> Path:
        return self._root

    def _path_for(self, scope_id: str, digest: str) -> Path:
        scope_dir = _SAFE_SCOPE.sub("_", scope_id) or "_"
        return self._root / scope_dir / digest[:2] / digest

    async def put(self, artifact: ArtifactInput) -> ArtifactRef:
        digest = hashlib.sha256(artifact.content).hexdigest()
        path = self._path_for(artifact.scope_id, digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(artifact.content)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        stored = await self._repo.add(
            Artifact(
                scope_id=artifact.scope_id,
                task_id=artifact.task_id,
                name=artifact.name,
                uri=path.as_uri(),
                media_type=artifact.media_type,
                content_hash=f"sha256:{digest}",
                size_bytes=len(artifact.content),
                source_refs=list(artifact.source_refs),
            )
        )
        return ArtifactRef.of(stored)

    async def read(
        self,
        reference: ArtifactRef | str,
        actor: Actor,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> ArtifactContent:
        artifact_id = reference if isinstance(reference, str) else reference.id
        # Scope check first: another scope's artifact is NotFound, the same as
        # one that does not exist (spec 3.2).
        artifact = await self._repo.get(actor.scope_id, artifact_id)
        path = self._file_of(artifact)
        if not path.is_file():
            raise NotFound(f"content of artifact {artifact_id} is missing")
        data = path.read_bytes()
        if f"sha256:{hashlib.sha256(data).hexdigest()}" != artifact.content_hash:
            raise ArtifactIntegrityError(f"content of artifact {artifact_id} does not match its hash")
        if offset < 0 or (length is not None and length < 0):
            raise ValueError("offset and length must be >= 0")
        end = None if length is None else offset + length
        return ArtifactContent(ref=ArtifactRef.of(artifact), content=data[offset:end], offset=offset)

    def _file_of(self, artifact: Artifact) -> Path:
        """Map the stored URI back to a path, refusing anything outside root."""
        if not artifact.uri.startswith("file:"):
            raise NotFound(f"artifact {artifact.id} is not stored in this store")
        from urllib.parse import unquote, urlparse

        parsed = urlparse(artifact.uri)
        raw = unquote(parsed.path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:", raw):
            raw = raw[1:]
        path = Path(raw).resolve()
        if path != self._root and self._root not in path.parents:
            raise NotFound(f"artifact {artifact.id} points outside the store")
        return path
