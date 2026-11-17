"""P0 connectors: read a file, read an artifact, write an artifact.

All three are local and side-effect free from the outside world's point of
view (``artifacts.write`` only writes into this task's own artifact area), so
none of them needs approval or reconciliation (spec 7 step 3: only services
that can be asked reliably implement ``reconcile``).

Path checks for ``files.read`` live in the gateway's policy step, not here:
by the time a connector runs the call is already authorised (``AuthorizedCall``).
The connector still refuses anything that is not a regular file, as a second
line of defence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...contracts import (
    ActionRef,
    Actor,
    ArtifactInput,
    AuthorizedCall,
    ErrorCode,
    HarnessError,
    NotFound,
    ReconcileResult,
    TaskError,
    ToolResult,
    ToolSpec,
    ToolStatus,
)
from ...interfaces import ArtifactStore

# Longest text returned inline to the model. Longer content is stored as an
# artifact and the model gets a preview plus the artifact ID to page through.
INLINE_CHARS = 6000
MAX_READ_BYTES = 5 * 1024 * 1024
MAX_WRITE_BYTES = 5 * 1024 * 1024


def _failed(code: ErrorCode, message: str) -> ToolResult:
    return ToolResult(status=ToolStatus.FAILED, error=TaskError(code=code, message=message))


def _page(text: str, offset: int, limit: int) -> tuple[str, bool]:
    lines = text.splitlines()
    chunk = lines[offset - 1 : offset - 1 + limit]
    return "\n".join(chunk), offset - 1 + limit < len(lines)


def _int_arg(args: dict[str, Any], name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(args.get(name, default))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    return max(low, min(value, high))


FILES_READ = ToolSpec(
    name="files.read",
    description=(
        "Read a text file the user allowed or the task named as input. "
        "Use offset and limit (lines) to page through long files."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path of the file to read."},
            "offset": {"type": "integer", "minimum": 1, "description": "First line, 1-based (default 1)."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "description": "Lines to return (default 400)."},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    read_only=True,
    side_effects=False,
    idempotent=True,
)

ARTIFACTS_READ = ToolSpec(
    name="artifacts.read",
    description="Read an artifact of this task (a draft, a stored tool result) by its ID, with line paging.",
    input_schema={
        "type": "object",
        "properties": {
            "artifact_id": {"type": "string"},
            "offset": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
        },
        "required": ["artifact_id"],
        "additionalProperties": False,
    },
    read_only=True,
    side_effects=False,
    idempotent=True,
)

ARTIFACTS_WRITE = ToolSpec(
    name="artifacts.write",
    description=(
        "Write a named artifact for this task (for example the draft). Returns its ID; "
        "list the IDs in submit_result."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 120},
            "content": {"type": "string"},
            "media_type": {"type": "string", "description": "Default text/markdown."},
        },
        "required": ["name", "content"],
        "additionalProperties": False,
    },
    read_only=False,
    # Writes only into the task's own artifact area: nothing outside changes.
    side_effects=False,
    idempotent=True,
)


class _Base:
    spec: ToolSpec

    async def reconcile(self, action: ActionRef) -> ReconcileResult:
        # Local and synchronous: a call either finished or raised. There is no
        # remote side to ask.
        return ReconcileResult(status="unsupported", detail=f"{self.spec.name} has no remote state")


class FilesReadConnector(_Base):
    spec = FILES_READ

    def __init__(self, artifacts: ArtifactStore) -> None:
        self._artifacts = artifacts

    async def execute(self, call: AuthorizedCall) -> ToolResult:
        try:
            offset = _int_arg(call.args, "offset", 1, 1, 10**9)
            limit = _int_arg(call.args, "limit", 400, 1, 2000)
        except ValueError as exc:
            return _failed(ErrorCode.INVALID_REQUEST, str(exc))
        path = Path(str(call.args.get("path", ""))).expanduser().resolve()
        if not path.is_file():
            return _failed(ErrorCode.NOT_FOUND, f"no such file: {call.args.get('path')}")
        size = path.stat().st_size
        if size > MAX_READ_BYTES:
            return _failed(ErrorCode.INVALID_REQUEST, f"file is {size} bytes, over the {MAX_READ_BYTES} byte limit")
        raw = path.read_bytes()
        if b"\x00" in raw[:8192]:
            return _failed(ErrorCode.INVALID_REQUEST, "file looks binary; only text files can be read")
        text = raw.decode("utf-8", errors="replace")
        chunk, more = _page(text, offset, limit)
        total = len(text.splitlines())
        head = f"{path.name}: lines {offset}-{min(offset + limit - 1, total)} of {total}"
        if more:
            head += f" (more: call again with offset={offset + limit})"
        if len(chunk) <= INLINE_CHARS:
            return ToolResult(status=ToolStatus.SUCCEEDED, summary=f"{head}\n{chunk}", data={"lines": total})
        ref = await self._artifacts.put(
            ArtifactInput(
                scope_id=call.scope_id,
                task_id=call.task_id,
                name=f"read:{path.name}:{offset}",
                media_type="text/plain",
                content=chunk.encode("utf-8"),
                source_refs=[f"file:{path}"],
            )
        )
        return ToolResult(
            status=ToolStatus.SUCCEEDED,
            summary=(
                f"{head}\nThis page is long ({len(chunk)} chars); stored as artifact {ref.id}. "
                f"Read it with artifacts.read. Preview:\n{chunk[:INLINE_CHARS]}"
            ),
            artifact_refs=[ref.id],
            data={"lines": total},
        )


class ArtifactsReadConnector(_Base):
    spec = ARTIFACTS_READ

    def __init__(self, artifacts: ArtifactStore) -> None:
        self._artifacts = artifacts

    async def execute(self, call: AuthorizedCall) -> ToolResult:
        try:
            offset = _int_arg(call.args, "offset", 1, 1, 10**9)
            limit = _int_arg(call.args, "limit", 400, 1, 2000)
        except ValueError as exc:
            return _failed(ErrorCode.INVALID_REQUEST, str(exc))
        artifact_id = str(call.args.get("artifact_id", ""))
        # The store checks the scope; the actor ID is not used for P0 checks.
        actor = Actor(actor_id="harness", scope_id=call.scope_id)
        try:
            content = await self._artifacts.read(artifact_id, actor)
        except NotFound:
            return _failed(ErrorCode.NOT_FOUND, f"no such artifact: {artifact_id}")
        if not content.ref.media_type.startswith(("text/", "application/json")):
            return _failed(ErrorCode.INVALID_REQUEST, f"artifact is {content.ref.media_type}, not text")
        text = content.content.decode("utf-8", errors="replace")
        chunk, more = _page(text, offset, limit)
        total = len(text.splitlines())
        head = f"{content.ref.name or artifact_id}: lines {offset}-{min(offset + limit - 1, total)} of {total}"
        if more:
            head += f" (more: call again with offset={offset + limit})"
        if len(chunk) > INLINE_CHARS:
            chunk = chunk[:INLINE_CHARS] + "\n[page truncated; use a smaller limit]"
        return ToolResult(status=ToolStatus.SUCCEEDED, summary=f"{head}\n{chunk}", data={"lines": total})


class ArtifactsWriteConnector(_Base):
    spec = ARTIFACTS_WRITE

    def __init__(self, artifacts: ArtifactStore) -> None:
        self._artifacts = artifacts

    async def execute(self, call: AuthorizedCall) -> ToolResult:
        name = str(call.args.get("name", "")).strip()
        content = call.args.get("content")
        if not name or not isinstance(content, str):
            return _failed(ErrorCode.INVALID_REQUEST, "name and content (a string) are required")
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            return _failed(ErrorCode.INVALID_REQUEST, f"content is {len(data)} bytes, over the {MAX_WRITE_BYTES} byte limit")
        media_type = str(call.args.get("media_type") or "text/markdown")
        try:
            ref = await self._artifacts.put(
                ArtifactInput(
                    scope_id=call.scope_id,
                    task_id=call.task_id,
                    name=name,
                    media_type=media_type,
                    content=data,
                    source_refs=[f"action:{call.action_id}"],
                )
            )
        except HarnessError as exc:
            return _failed(exc.code, exc.message)
        return ToolResult(
            status=ToolStatus.SUCCEEDED,
            summary=f"wrote artifact {name!r} as {ref.id} ({ref.size_bytes} bytes)",
            artifact_refs=[ref.id],
            receipt_ref=ref.id,
        )


def p0_connectors(artifacts: ArtifactStore) -> list:
    return [FilesReadConnector(artifacts), ArtifactsReadConnector(artifacts), ArtifactsWriteConnector(artifacts)]
