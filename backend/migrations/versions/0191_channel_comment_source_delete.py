"""Add channel comment append-only lifecycle events.

Revision ID: 0191_comment_source_delete
Revises: 0190_comment_content_revision
"""

from alembic import context, op
import sqlalchemy as sa


revision = "0191_comment_source_delete"
down_revision = "0190_comment_content_revision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _has_table("channel_comment_plan_lifecycle_events"):
        _create_lifecycle_events()


def _create_lifecycle_events() -> None:
    op.create_table(
        "channel_comment_plan_lifecycle_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("plan_contract_id", sa.String(length=36), nullable=False),
        sa.Column("lifecycle_epoch", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task_revision", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=120), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("event_state", sa.String(length=32), nullable=False),
        sa.Column("result_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["plan_contract_id"], ["channel_comment_plan_contracts.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_contract_id", "lifecycle_epoch", "event_type", "evidence_hash",
            name="uq_channel_comment_plan_lifecycle_event",
        ),
    )


def _has_table(table_name: str) -> bool:
    if context.is_offline_mode():
        return False
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def downgrade() -> None:
    op.drop_table("channel_comment_plan_lifecycle_events")
