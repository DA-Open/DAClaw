"""In-memory approvals: the human decision that lets a side-effecting call run."""
from __future__ import annotations

from ..contracts import Approval


def _copy(approval: Approval) -> Approval:
    return approval.model_copy(deep=True)


class InMemoryApprovalRepository:
    """One row per (action_id, request_hash), held in the shared store.

    The store's lock is what makes "check, decide, requeue" atomic for the
    in-memory system: tasks.decide_approval writes here under the same lock.
    """

    def __init__(self, store) -> None:
        self._s = store

    async def add(self, approval: Approval) -> Approval:
        async with self._s.lock:
            key = (approval.action_id, approval.request_hash)
            self._s.approvals[key] = _copy(approval)
            return _copy(self._s.approvals[key])

    async def get(self, action_id: str, request_hash: str) -> Approval | None:
        async with self._s.lock:
            found = self._s.approvals.get((action_id, request_hash))
            return _copy(found) if found is not None else None


__all__ = ["InMemoryApprovalRepository"]
