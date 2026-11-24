"""In-memory approvals: the human decision that lets a side-effecting call run."""
from __future__ import annotations

import asyncio

from ..contracts import Approval

_COPY = ("action_id", "request_hash", "approver", "decision", "reason", "expires_at", "decided_at")


def _copy(approval: Approval) -> Approval:
    return approval.model_copy(deep=True)


class InMemoryApprovalRepository:
    """One row per (action_id, request_hash); a second decision replaces the first."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], Approval] = {}
        self._lock = asyncio.Lock()

    async def add(self, approval: Approval) -> Approval:
        async with self._lock:
            key = (approval.action_id, approval.request_hash)
            self._rows[key] = _copy(approval)
            return _copy(self._rows[key])

    async def get(self, action_id: str, request_hash: str) -> Approval | None:
        async with self._lock:
            found = self._rows.get((action_id, request_hash))
            return _copy(found) if found is not None else None


__all__ = ["InMemoryApprovalRepository"]
