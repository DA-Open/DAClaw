"""approvals table; revision 0005.

The memory store and this table are deliberately separate from the subscription
tables: an approval belongs to an action, not to a task or a scope.
"""
from alembic import op
from sqlalchemy import Column, ForeignKey, MetaData, Table, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = depends_on = None


def upgrade():
    metadata = MetaData(naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })
    Table("actions", metadata, Column("id", Text, primary_key=True))
    approvals = Table("approvals", metadata,
        Column("id", Text, primary_key=True),
        Column("action_id", Text, ForeignKey("actions.id"), nullable=False, index=True),
        Column("request_hash", Text, nullable=False),
        Column("decision", Text, nullable=False),
        Column("body", JSONB, nullable=False),
        UniqueConstraint("action_id", "request_hash"))
    approvals.create(op.get_bind())


def downgrade():
    op.drop_table("approvals")
