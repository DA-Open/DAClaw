"""PostgreSQL approvals (asyncpg + SQLAlchemy 2.0 Core)."""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..contracts import Approval
from . import schema as s


def _approval(row) -> Approval:
    return Approval.model_validate(json.loads(row["body"]))


class PgApprovalRepository:
    """One row per (action_id, request_hash); a later decision replaces it."""

    def __init__(self, db) -> None:
        self._db = db

    async def add(self, approval: Approval) -> Approval:
        async with self._db.engine.begin() as conn:
            row = (
                await conn.execute(
                    pg_insert(s.approvals)
                    .values(
                        id=approval.action_id + ":" + approval.request_hash[:32],
                        action_id=approval.action_id,
                        request_hash=approval.request_hash,
                        decision=str(approval.decision),
                        body=approval.model_dump_json(),
                    )
                    .on_conflict_do_update(
                        index_elements=["action_id", "request_hash"],
                        set_={"decision": str(approval.decision), "body": approval.model_dump_json()},
                    )
                    .returning(*s.approvals.c)
                )
            ).mappings().one()
        return _approval(row)

    async def get(self, action_id: str, request_hash: str) -> Approval | None:
        async with self._db.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(s.approvals).where(
                        s.approvals.c.action_id == action_id,
                        s.approvals.c.request_hash == request_hash,
                    )
                )
            ).mappings().first()
        return _approval(row) if row is not None else None


__all__ = ["PgApprovalRepository"]
