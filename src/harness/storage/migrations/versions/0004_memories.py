"""Versioned memories; revision 0004."""
from alembic import op
from sqlalchemy import Column, DateTime, Index, Integer, MetaData, Table, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0004"
down_revision = "0003"
branch_labels = depends_on = None


def upgrade():
    metadata = MetaData(naming_convention={"pk": "pk_%(table_name)s"})
    ts = DateTime(timezone=True)
    memories = Table("memories", metadata,
        Column("id", Text, primary_key=True), Column("scope_id", Text, nullable=False),
        Column("kind", Text, nullable=False), Column("status", Text, nullable=False),
        Column("version", Integer, nullable=False), Column("supersedes_id", Text),
        Column("subject_ref", Text), Column("source_ref", Text, nullable=False),
        Column("ended_at", ts), Column("end_reason", Text), Column("expires_at", ts),
        Column("updated_at", ts, nullable=False), Column("summary_lc", Text, nullable=False),
        Column("tags_lc", ARRAY(Text), nullable=False), Column("body", JSONB, nullable=False))
    Index("uq_memories_supersedes", memories.c.supersedes_id, unique=True,
          postgresql_where=text("supersedes_id IS NOT NULL"))
    Index("ix_memories_scope_kind", memories.c.scope_id, memories.c.kind,
          postgresql_where=text("ended_at IS NULL"))
    Index("ix_memories_scope_subject", memories.c.scope_id, memories.c.subject_ref,
          postgresql_where=text("ended_at IS NULL AND subject_ref IS NOT NULL"))
    Index("uq_memories_task", memories.c.scope_id, memories.c.source_ref, memories.c.kind, unique=True,
          postgresql_where=text("source_ref LIKE 'task:%'"))
    memories.create(op.get_bind())


def downgrade():
    op.drop_table("memories")
