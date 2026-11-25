"""PostgreSQL tables for the harness (spec 3), defined once.

The repositories (``storage/postgres.py``) take table and column objects from
here, and Alembic compares migrations against this metadata, so a table or
column name is written in one place only.

Typed columns hold what queries filter, sort or lock on (status, lane,
lease, keys). Nested values that are only read back whole (acceptance,
limits, usage, error, payloads, checks) are ``jsonb`` and validated by the
pydantic models in ``contracts.py``.

Not here yet (later phases): grants, approvals, subscriptions,
subscription_events, memories, outbox, delivery_counters.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

# Stable constraint names, so migrations and error handling can refer to them.
metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

TS = DateTime(timezone=True)

tasks = Table(
    "tasks",
    metadata,
    Column("id", Text, primary_key=True),
    Column("scope_id", Text, nullable=False),
    Column("actor_id", Text, nullable=False),
    Column("request_key", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("goal", Text, nullable=False),
    Column("input_refs", JSONB, nullable=False),
    Column("capability_ref", Text, nullable=False),
    Column("acceptance", JSONB, nullable=False),
    Column("limits", JSONB, nullable=False),
    Column("usage", JSONB, nullable=False),
    Column("status", Text, nullable=False),
    Column("wait_condition", Text),
    Column("priority", Integer, nullable=False),
    Column("deadline", TS),
    Column("result_ref", Text),
    Column("checkpoint_ref", Text),
    Column("error", JSONB),
    Column("version", Integer, nullable=False),
    Column("lease_epoch", Integer, nullable=False),
    Column("current_attempt_id", Text),
    Column("cancel_requested_at", TS),
    Column("cancel_reason", Text),
    Column("agent_role", Text, nullable=False),
    Column("agent_profile_ref", Text, nullable=False),
    Column("run_lane", Text, nullable=False),
    Column("trigger_type", Text, nullable=False),
    Column("trigger_ref", Text),
    Column("subscription_id", Text),
    Column("conversation_id", Text),
    Column("subject_ref", Text),
    Column("source_task_id", Text),
    Column("result_expires_at", TS),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    UniqueConstraint("scope_id", "request_key"),
)
# The claim query reads only queued rows of some lanes in priority order; a
# partial index keeps it from scanning finished tasks (FOR UPDATE SKIP LOCKED).
Index(
    "ix_tasks_queue",
    tasks.c.run_lane,
    tasks.c.priority.desc(),
    tasks.c.created_at,
    tasks.c.id,
    postgresql_where=text("status = 'queued'"),
)

attempts = Table(
    "attempts",
    metadata,
    Column("id", Text, primary_key=True),
    Column("task_id", Text, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column("worker_id", Text, nullable=False),
    Column("lease_epoch", Integer, nullable=False),
    Column("lease_expires_at", TS, nullable=False),
    Column("heartbeat_at", TS, nullable=False),
    Column("started_at", TS, nullable=False),
    Column("ended_at", TS),
    Column("agent_profile_version", Text),
    Column("model_version", Text),
    Column("capability_version", Text),
    Column("context_ref", Text),
    Column("exit_reason", Text),
)
Index("ix_attempts_task_id", attempts.c.task_id)

events = Table(
    "events",
    metadata,
    # seq is the SSE cursor (Last-Event-ID): increasing, never reused.
    Column("seq", BigInteger, Identity(always=True), primary_key=True),
    Column("id", Text, nullable=False, unique=True),
    Column("scope_id", Text, nullable=False),
    Column("task_id", Text),
    Column("attempt_id", Text),
    Column("type", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("source_event_id", Text),
    Column("subject_ref", Text),
    Column("payload", JSONB, nullable=False),
    Column("payload_ref", Text),
    Column("occurred_at", TS, nullable=False),
    Column("received_at", TS, nullable=False),
    Column("processed_at", TS),
)
Index("ix_events_scope_task_seq", events.c.scope_id, events.c.task_id, events.c.seq)
# Outside events are deduplicated on (scope_id, source, source_event_id); the
# harness's own events have no source_event_id.
Index(
    "uq_events_source_event",
    events.c.scope_id,
    events.c.source,
    events.c.source_event_id,
    unique=True,
    postgresql_where=text("source_event_id IS NOT NULL"),
)

actions = Table(
    "actions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("task_id", Text, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column("attempt_id", Text, nullable=False),
    Column("logical_key", Text, nullable=False),
    Column("tool", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("args_ref", Text),
    Column("read_only", Boolean, nullable=False),
    Column("grant_ref", Text),
    Column("approval_ref", Text),
    Column("status", Text, nullable=False),
    Column("result_ref", Text),
    Column("receipt_ref", Text),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    UniqueConstraint("task_id", "logical_key"),
)

artifacts = Table(
    "artifacts",
    metadata,
    Column("id", Text, primary_key=True),
    Column("scope_id", Text, nullable=False),
    Column("task_id", Text),
    Column("name", Text),
    Column("uri", Text, nullable=False),
    Column("media_type", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("source_refs", JSONB, nullable=False),
    Column("created_at", TS, nullable=False),
)
Index("ix_artifacts_scope_task", artifacts.c.scope_id, artifacts.c.task_id)

verifications = Table(
    "verifications",
    metadata,
    Column("id", Text, primary_key=True),
    Column("task_id", Text, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column("result_ref", Text, nullable=False),
    Column("checker_version", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("checks", JSONB, nullable=False),
    Column("unverified_items", JSONB, nullable=False),
    Column("created_at", TS, nullable=False),
)
Index("ix_verifications_task_id", verifications.c.task_id)

# One row per call_id (spec 6.2: idempotent accounting per call). Spec column
# name: usage_records.
usage_reservations = Table(
    "usage_reservations",
    metadata,
    Column("call_id", Text, primary_key=True),
    Column("task_id", Text, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column("attempt_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("reserved", JSONB, nullable=False),
    Column("settled", JSONB),
    Column("status", Text, nullable=False),
    # A refused reservation is recorded too, so repeating the call ID gives the
    # same answer (ReservationResult is idempotent per call ID).
    Column("granted", Boolean, nullable=False),
    Column("exceeded", JSONB, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("settled_at", TS),
)
Index("ix_usage_reservations_task_status", usage_reservations.c.task_id, usage_reservations.c.status)

subscriptions = Table("subscriptions", metadata,
    Column("id", Text, primary_key=True), Column("scope_id", Text, nullable=False, index=True),
    Column("status", Text, nullable=False), Column("body", JSONB, nullable=False))
subscription_events = Table("subscription_events", metadata,
    Column("subscription_id", Text, ForeignKey("subscriptions.id"), primary_key=True),
    Column("event_id", Text, ForeignKey("events.id"), primary_key=True),
    Column("scope_id", Text, nullable=False), Column("decision", Text, nullable=False),
    Column("task_id", Text, ForeignKey("tasks.id")), Column("body", JSONB, nullable=False))
Index("ix_tasks_proactive_running", tasks.c.scope_id, tasks.c.agent_role,
      postgresql_where=text("status = 'running'"))
inbox_items = Table("inbox_items", metadata,
    Column("id", Text, primary_key=True), Column("scope_id", Text, nullable=False, index=True),
    Column("subscription_id", Text, ForeignKey("subscriptions.id"), nullable=False),
    Column("task_id", Text, ForeignKey("tasks.id"), nullable=False, unique=True),
    Column("status", Text, nullable=False), Column("body", JSONB, nullable=False))
approvals = Table("approvals", metadata,
    Column("id", Text, primary_key=True),
    Column("action_id", Text, ForeignKey("actions.id"), nullable=False, index=True),
    Column("request_hash", Text, nullable=False),
    Column("decision", Text, nullable=False),
    Column("body", JSONB, nullable=False),
    UniqueConstraint("action_id", "request_hash"))
delivery_counters = Table("delivery_counters", metadata,
    Column("scope_id", Text, primary_key=True), Column("dedup_key", Text, primary_key=True),
    Column("body", JSONB, nullable=False))

memories = Table("memories", metadata,
    Column("id", Text, primary_key=True), Column("scope_id", Text, nullable=False),
    Column("kind", Text, nullable=False), Column("status", Text, nullable=False),
    Column("version", Integer, nullable=False), Column("supersedes_id", Text),
    Column("subject_ref", Text), Column("source_ref", Text, nullable=False),
    Column("ended_at", TS), Column("end_reason", Text), Column("expires_at", TS),
    Column("updated_at", TS, nullable=False), Column("summary_lc", Text, nullable=False),
    Column("tags_lc", ARRAY(Text), nullable=False), Column("body", JSONB, nullable=False))
Index("uq_memories_supersedes", memories.c.supersedes_id, unique=True,
      postgresql_where=text("supersedes_id IS NOT NULL"))
Index("ix_memories_scope_kind", memories.c.scope_id, memories.c.kind,
      postgresql_where=text("ended_at IS NULL"))
Index("ix_memories_scope_subject", memories.c.scope_id, memories.c.subject_ref,
      postgresql_where=text("ended_at IS NULL AND subject_ref IS NOT NULL"))
Index("uq_memories_task", memories.c.scope_id, memories.c.source_ref, memories.c.kind, unique=True,
      postgresql_where=text("source_ref LIKE 'task:%'"))

__all__ = [
    "actions",
    "approvals",
    "artifacts",
    "attempts",
    "events",
    "metadata",
    "tasks",
    "usage_reservations",
    "verifications",
]
