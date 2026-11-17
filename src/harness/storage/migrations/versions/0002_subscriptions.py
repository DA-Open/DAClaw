"""subscriptions tables; revision 0002."""
from alembic import op
from sqlalchemy import Column, ForeignKey, Index, MetaData, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
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
    for table in (subscriptions, subscription_events):
        table.create(op.get_bind())
    next(iter(tasks.indexes)).create(op.get_bind())

def downgrade():
    op.drop_index("ix_tasks_proactive_running", table_name="tasks")
    op.drop_table("subscription_events")
    op.drop_table("subscriptions")
