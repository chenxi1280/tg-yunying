"""Make channel comment Plan creation concurrency-safe.

Revision ID: 0196_comment_plan_safety
Revises: 0195_comment_grounding_snapshot
"""

from alembic import context, op
import sqlalchemy as sa


revision = "0196_comment_plan_safety"
down_revision = "0195_comment_grounding_snapshot"
branch_labels = None
depends_on = None


TABLE = "channel_comment_plan_contracts"
ACTIVE_INDEX = "uq_channel_comment_plan_active"


def upgrade() -> None:
    if not _has_column(TABLE, "eligibility_snapshot_state"):
        op.add_column(TABLE, sa.Column(
            "eligibility_snapshot_state",
            sa.String(32),
            nullable=False,
            server_default="ready",
        ))
    if not _has_index(TABLE, ACTIVE_INDEX):
        op.create_index(
            ACTIVE_INDEX,
            TABLE,
            ["task_id", "channel_message_id"],
            unique=True,
            postgresql_where=sa.text("contract_state = 'open'"),
            sqlite_where=sa.text("contract_state = 'open'"),
        )


def downgrade() -> None:
    if _has_index(TABLE, ACTIVE_INDEX):
        op.drop_index(ACTIVE_INDEX, table_name=TABLE)
    if _has_column(TABLE, "eligibility_snapshot_state"):
        op.drop_column(TABLE, "eligibility_snapshot_state")


def _has_column(table: str, column: str) -> bool:
    if context.is_offline_mode():
        return False
    return any(row["name"] == column for row in sa.inspect(op.get_bind()).get_columns(table))


def _has_index(table: str, index: str) -> bool:
    if context.is_offline_mode():
        return False
    return any(row["name"] == index for row in sa.inspect(op.get_bind()).get_indexes(table))
