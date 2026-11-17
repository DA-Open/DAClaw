"""inbox tables; revision 0003."""
from alembic import op
from sqlalchemy import Column, ForeignKey, Index, MetaData, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = depends_on = None

def upgrade():
    metadata = MetaData(naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })
    tasks = Table("tasks", metadata, Column("id", Text, primary_key=True), Column("scope_id", Text), Column("agent_role", Text))
    Table("events", metadata, Column("id", Text, primary_key=True))
    Table("subscriptions", metadata, Column("id", Text, primary_key=True))
    inbox_items = Table("inbox_items", metadata,
        Column("id", Text, primary_key=True), Column("scope_id", Text, nullable=False, index=True),
        Column("subscription_id", Text, ForeignKey("subscriptions.id"), nullable=False),
        Column("task_id", Text, ForeignKey("tasks.id"), nullable=False, unique=True),
        Column("status", Text, nullable=False), Column("body", JSONB, nullable=False))
    delivery_counters = Table("delivery_counters", metadata,
        Column("scope_id", Text, primary_key=True), Column("dedup_key", Text, primary_key=True),
        Column("body", JSONB, nullable=False))
    for table in (inbox_items, delivery_counters):
        table.create(op.get_bind())

def downgrade():
    op.drop_table("delivery_counters")
    op.drop_table("inbox_items")
