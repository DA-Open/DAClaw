"""P0 file connectors: read one text file, write one text file.

Both work inside one configured root, so a path never escapes it. They follow
the connector contract: the gateway has already checked policy, registered the
action and reserved the budget; a connector only executes an
``AuthorizedCall`` and returns a ``ToolResult``. Long content goes to
ArtifactStore and the summary keeps only a prefix plus the artifact ID.
"""

from __future__ import annotations

from pathlib import Path

from ...contracts import (
    ArtifactInput,
    AuthorizedCall,
    ErrorCode,
    ReconcileResult,
    TaskError,
    ToolResult,
    ToolSpec,
    ToolStatus,
)
from ...interfaces import ArtifactStore

MAX_SUMMARY_CHARS = 8000


def _failed(message: str) -> ToolResult:
    return ToolResult(
        status=ToolStatus.FAILED,
        summary=message,
        error=TaskError(code=ErrorCode.TOOL_ERROR, message=message),
    )


class ReadFileConnector:
    spec = ToolSpec(
        name="files.read",
        description="Read a UTF-8 text file inside the workspace root.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path relative to the root"}},
            "required": ["path"],
        },
        read_only=True,
        side_effects=False,
        idempotent=True,
    )

    def __init__(
        self,
        root: Path,
        *,
        artifacts: ArtifactStore | None = None,
        max_bytes: int = 200_000,
    ) -> None:
        self.root = Path(root).resolve()
        self._artifacts = artifacts
        self._max_bytes = max_bytes

    async def execute(self, call: AuthorizedCall) -> ToolResult:
        raw_path = str(call.args.get("path") or "").strip()
        if not raw_path:
            return _failed("files.read requires 'path'")
        target = (self.root / raw_path).resolve()
        if not target.is_relative_to(self.root):
            return _failed(f"path escapes the workspace root: {raw_path}")
        if not target.is_file():
            return _failed(f"file not found: {raw_path}")
        data = target.read_bytes()[: self._max_bytes]
        text = data.decode("utf-8", errors="replace")
        data_info = {"path": raw_path, "bytes": len(data)}
        if len(text) <= MAX_SUMMARY_CHARS:
            return ToolResult(status=ToolStatus.SUCCEEDED, summary=text, data=data_info)
        refs: list[str] = []
        summary = text[:MAX_SUMMARY_CHARS]
        if self._artifacts is not None:
            ref = await self._artifacts.put(
                ArtifactInput(
                    scope_id=call.scope_id,
                    task_id=call.task_id,
                    name=f"read/{raw_path}",
                    media_type="text/plain",
                    content=text.encode("utf-8"),
                )
            )
            refs.append(ref.id)
            summary += f"\n\n[truncated; full content: {ref.id}]"
        else:
            summary += "\n\n[truncated]"
        return ToolResult(
            status=ToolStatus.SUCCEEDED,
            summary=summary,
            data=data_info,
            artifact_refs=refs,
        )

    async def reconcile(self, action) -> ReconcileResult:
        return ReconcileResult(status="unsupported")


class WriteFileConnector:
    spec = ToolSpec(
        name="files.write",
        description="Write a UTF-8 text file inside the workspace root and keep it as an artifact.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the root"},
                "content": {"type": "string"},
                "name": {
                    "type": "string",
                    "description": "Artifact name; defaults to the file name",
                },
            },
            "required": ["path", "content"],
        },
        read_only=False,
        side_effects=True,
    )

    def __init__(
        self,
        root: Path,
        artifacts: ArtifactStore,
        *,
        media_type: str = "text/plain",
    ) -> None:
        self.root = Path(root).resolve()
        self._artifacts = artifacts
        self._media_type = media_type

    async def execute(self, call: AuthorizedCall) -> ToolResult:
        raw_path = str(call.args.get("path") or "").strip()
        content = call.args.get("content")
        if not raw_path or not isinstance(content, str):
            return _failed("files.write requires 'path' and string 'content'")
        target = (self.root / raw_path).resolve()
        if not target.is_relative_to(self.root):
            return _failed(f"path escapes the workspace root: {raw_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        ref = await self._artifacts.put(
            ArtifactInput(
                scope_id=call.scope_id,
                task_id=call.task_id,
                name=str(call.args.get("name") or Path(raw_path).name),
                media_type=self._media_type,
                content=content.encode("utf-8"),
            )
        )
        return ToolResult(
            status=ToolStatus.SUCCEEDED,
            summary=f"wrote {raw_path} ({len(content)} chars)",
            data={"path": raw_path, "bytes": len(content.encode("utf-8"))},
            artifact_refs=[ref.id],
        )

    async def reconcile(self, action) -> ReconcileResult:
        return ReconcileResult(status="unsupported")
