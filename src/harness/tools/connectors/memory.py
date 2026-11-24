"""Proactive's read-only memory tools; every lookup uses AuthorizedCall.scope_id."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ...contracts import ErrorCode, NotFound, ToolResult, ToolSpec, ToolStatus, canonical_json
from ...storage.memory_store import is_current
from .files import _Base, _failed


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=200)
    kinds: list[Literal["preference", "fact", "analysis", "episode"]] | None = None
    limit: int = Field(5, ge=1, le=10)


class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(pattern=r"^memory:mem_[0-9a-f]{32}@[1-9]\d*$")


def _view(record):
    return dict(ref=f"memory:{record.id}@{record.version}", kind=record.kind, status=record.status,
                summary=record.summary, tags=record.tags)


class MemorySearchConnector(_Base):
    spec = ToolSpec(name="memory.search", description=(
        "输入 1–3 个用空格分开的短关键词，例如 `组会 汇报`，不要输入完整句子。返回当前记忆的摘要和引用。"),
        input_schema=SearchArgs.model_json_schema(), read_only=True, side_effects=False, idempotent=True)

    def __init__(self, memory):
        self.memory = memory

    async def execute(self, call):
        try:
            args = SearchArgs.model_validate(call.args)
            records = await self.memory.memories.search(call.scope_id, args.query, now=self.memory.clock(),
                                                        kinds=args.kinds, limit=args.limit)
        except ValueError as exc:
            return _failed(ErrorCode.INVALID_REQUEST, str(exc))
        items = [{**_view(r), "updated_at": r.updated_at.isoformat()} for r in records]
        return ToolResult(status=ToolStatus.SUCCEEDED, summary=canonical_json(items))


class MemoryReadConnector(_Base):
    spec = ToolSpec(name="memory.read", description="按 memory:<id>@<version> 引用读取当前有效记忆的正文。",
        input_schema=ReadArgs.model_json_schema(), read_only=True, side_effects=False, idempotent=True)

    def __init__(self, memory):
        self.memory = memory

    async def execute(self, call):
        args = ReadArgs.model_validate(call.args)
        memory_id, version = args.ref.removeprefix("memory:").split("@")
        try:
            record, text = await self.memory.read(call.scope_id, "harness", memory_id)
            if not is_current(record, self.memory.clock()) or record.version != int(version):
                raise NotFound("memory is not current")
        except NotFound:
            return _failed(ErrorCode.NOT_FOUND, "memory not found")
        data = {**_view(record), "text": text[:4000]}
        return ToolResult(status=ToolStatus.SUCCEEDED, summary=canonical_json(data), data=data)
