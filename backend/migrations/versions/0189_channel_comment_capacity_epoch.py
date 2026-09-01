"""Add channel comment capacity allocation epochs.

Revision ID: 0189_comment_capacity_epoch
Revises: 0188_comment_plan_contract
"""

from alembic import context, op
import sqlalchemy as sa


revision = "0189_comment_capacity_epoch"
down_revision = "0188_comment_plan_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _has_table("channel_comment_capacity_allocation_epochs"):
        _create_allocation_epochs()
    if not _has_column("task_comment_capacity_reservations", "allocation_epoch"):
        op.add_column(
            "task_comment_capacity_reservations",
            sa.Column("allocation_epoch", sa.Integer(), nullable=True),
        )


def _create_allocation_epochs() -> None:
    op.create_table(
        "channel_comment_capacity_allocation_epochs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("allocation_epoch", sa.Integer(), nullable=False),
        sa.Column("horizon_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("horizon_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open_plan_set_hash", sa.String(length=64), nullable=False),
        sa.Column("immutable_usage_hash", sa.String(length=64), nullable=False),
        sa.Column("allocation_result_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id", "allocation_epoch",
            name="uq_channel_comment_capacity_allocation_epoch",
        ),
    )


def _has_table(table_name: str) -> bool:
    if context.is_offline_mode():
        return False
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if context.is_offline_mode() or not _has_table(table_name):
        return False
    columns = sa.inspect(op.get_bind()).get_columns(table_name)
    return any(column["name"] == column_name for column in columns)


def downgrade() -> None:
    op.drop_column("task_comment_capacity_reservations", "allocation_epoch")
    op.drop_table("channel_comment_capacity_allocation_epochs")
