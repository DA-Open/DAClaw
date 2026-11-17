"""P0 tables: tasks, attempts, events, actions, artifacts, verifications, usage_reservations.

Revision ID: 0001
Revises:
Create Date: 2026-11-17

Written by hand from storage/schema.py. ``test_postgres_schema.py`` checks
that the database after this migration matches the metadata (columns,
types, nullability, indexes), so the two cannot drift apart.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("request_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("input_refs", JSONB(), nullable=False),
        sa.Column("capability_ref", sa.Text(), nullable=False),
        sa.Column("acceptance", JSONB(), nullable=False),
        sa.Column("limits", JSONB(), nullable=False),
        sa.Column("usage", JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("wait_condition", sa.Text(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("deadline", TS, nullable=True),
        sa.Column("result_ref", sa.Text(), nullable=True),
        sa.Column("checkpoint_ref", sa.Text(), nullable=True),
        sa.Column("error", JSONB(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("lease_epoch", sa.Integer(), nullable=False),
        sa.Column("current_attempt_id", sa.Text(), nullable=True),
        sa.Column("cancel_requested_at", TS, nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("agent_role", sa.Text(), nullable=False),
        sa.Column("agent_profile_ref", sa.Text(), nullable=False),
        sa.Column("run_lane", sa.Text(), nullable=False),
        sa.Column("trigger_type", sa.Text(), nullable=False),
        sa.Column("trigger_ref", sa.Text(), nullable=True),
        sa.Column("subscription_id", sa.Text(), nullable=True),
        sa.Column("conversation_id", sa.Text(), nullable=True),
        sa.Column("subject_ref", sa.Text(), nullable=True),
        sa.Column("source_task_id", sa.Text(), nullable=True),
        sa.Column("result_expires_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.UniqueConstraint("scope_id", "request_key", name="uq_tasks_scope_id_request_key"),
    )
    op.create_index(
        "ix_tasks_queue",
        "tasks",
        ["run_lane", sa.text("priority DESC"), "created_at", "id"],
        postgresql_where=sa.text("status = 'queued'"),
    )

    op.create_table(
        "attempts",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("lease_epoch", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", TS, nullable=False),
        sa.Column("heartbeat_at", TS, nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("ended_at", TS, nullable=True),
        sa.Column("agent_profile_version", sa.Text(), nullable=True),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("capability_version", sa.Text(), nullable=True),
        sa.Column("context_ref", sa.Text(), nullable=True),
        sa.Column("exit_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name="fk_attempts_task_id_tasks", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_attempts"),
    )
    op.create_index("ix_attempts_task_id", "attempts", ["task_id"])

    op.create_table(
        "events",
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("attempt_id", sa.Text(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_event_id", sa.Text(), nullable=True),
        sa.Column("subject_ref", sa.Text(), nullable=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("payload_ref", sa.Text(), nullable=True),
        sa.Column("occurred_at", TS, nullable=False),
        sa.Column("received_at", TS, nullable=False),
        sa.Column("processed_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("seq", name="pk_events"),
        sa.UniqueConstraint("id", name="uq_events_id"),
    )
    op.create_index("ix_events_scope_task_seq", "events", ["scope_id", "task_id", "seq"])
    op.create_index(
        "uq_events_source_event",
        "events",
        ["scope_id", "source", "source_event_id"],
        unique=True,
        postgresql_where=sa.text("source_event_id IS NOT NULL"),
    )

    op.create_table(
        "actions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("attempt_id", sa.Text(), nullable=False),
        sa.Column("logical_key", sa.Text(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("args_ref", sa.Text(), nullable=True),
        sa.Column("read_only", sa.Boolean(), nullable=False),
        sa.Column("grant_ref", sa.Text(), nullable=True),
        sa.Column("approval_ref", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("result_ref", sa.Text(), nullable=True),
        sa.Column("receipt_ref", sa.Text(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name="fk_actions_task_id_tasks", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_actions"),
        sa.UniqueConstraint("task_id", "logical_key", name="uq_actions_task_id_logical_key"),
    )

    op.create_table(
        "artifacts",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("source_refs", JSONB(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_artifacts"),
    )
    op.create_index("ix_artifacts_scope_task", "artifacts", ["scope_id", "task_id"])

    op.create_table(
        "verifications",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("result_ref", sa.Text(), nullable=False),
        sa.Column("checker_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("checks", JSONB(), nullable=False),
        sa.Column("unverified_items", JSONB(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name="fk_verifications_task_id_tasks", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_verifications"),
    )
    op.create_index("ix_verifications_task_id", "verifications", ["task_id"])

    op.create_table(
        "usage_reservations",
        sa.Column("call_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("attempt_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("reserved", JSONB(), nullable=False),
        sa.Column("settled", JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False),
        sa.Column("exceeded", JSONB(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("settled_at", TS, nullable=True),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_usage_reservations_task_id_tasks", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("call_id", name="pk_usage_reservations"),
    )
    op.create_index("ix_usage_reservations_task_status", "usage_reservations", ["task_id", "status"])


def downgrade() -> None:
    for table in ("usage_reservations", "verifications", "artifacts", "actions", "events", "attempts", "tasks"):
        op.drop_table(table)
