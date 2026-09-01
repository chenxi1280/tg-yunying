"""Add channel comment content revision successor ownership.

Revision ID: 0190_comment_content_revision
Revises: 0189_comment_capacity_epoch
"""

from alembic import context, op
import sqlalchemy as sa


revision = "0190_comment_content_revision"
down_revision = "0189_comment_capacity_epoch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _has_column("channel_comment_grounding_assignments", "supersedes_assignment_id"):
        _add_successor_column()
    if not _has_index("channel_comment_grounding_assignments", "uq_channel_comment_grounding_active"):
        _add_active_assignment_index()
    if not _has_table("channel_comment_content_revision_operations"):
        _create_revision_operations()


def _add_successor_column() -> None:
    op.add_column(
        "channel_comment_grounding_assignments",
        sa.Column("supersedes_assignment_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_channel_comment_grounding_supersedes",
        "channel_comment_grounding_assignments",
        "channel_comment_grounding_assignments",
        ["supersedes_assignment_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def _add_active_assignment_index() -> None:
    predicate = sa.text("assignment_state = 'active'")
    op.create_index(
        "uq_channel_comment_grounding_active",
        "channel_comment_grounding_assignments",
        ["plan_contract_id", "target_ordinal"],
        unique=True,
        postgresql_where=predicate,
        sqlite_where=predicate,
    )


def _create_revision_operations() -> None:
    op.create_table(
        "channel_comment_content_revision_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("plan_contract_id", sa.String(length=36), nullable=False),
        sa.Column("from_source_revision_id", sa.String(length=36), nullable=False),
        sa.Column("to_source_revision_id", sa.String(length=36), nullable=False),
        sa.Column("operation_version", sa.Integer(), nullable=False),
        sa.Column("operation_state", sa.String(length=32), nullable=False),
        sa.Column("result_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["plan_contract_id"], ["channel_comment_plan_contracts.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["from_source_revision_id"], ["channel_message_source_revisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["to_source_revision_id"], ["channel_message_source_revisions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_contract_id", "to_source_revision_id",
            name="uq_channel_comment_content_revision_operation",
        ),
    )


def _has_table(table_name: str) -> bool:
    if context.is_offline_mode():
        return False
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if context.is_offline_mode() or not _has_table(table_name):
        return False
    return any(
        column["name"] == column_name
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    )


def _has_index(table_name: str, index_name: str) -> bool:
    if context.is_offline_mode() or not _has_table(table_name):
        return False
    return any(
        index["name"] == index_name
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    )


def downgrade() -> None:
    op.drop_table("channel_comment_content_revision_operations")
    op.drop_index(
        "uq_channel_comment_grounding_active",
        table_name="channel_comment_grounding_assignments",
    )
    op.drop_constraint(
        "fk_channel_comment_grounding_supersedes",
        "channel_comment_grounding_assignments",
        type_="foreignkey",
    )
    op.drop_column("channel_comment_grounding_assignments", "supersedes_assignment_id")
