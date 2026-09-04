"""Add immutable account-group binding revisions for unified engagement tasks.

Revision ID: 0197_unified_engagement_binding
Revises: 0196_comment_plan_safety
"""

from alembic import op
import sqlalchemy as sa


revision = "0197_unified_engagement_binding"
down_revision = "0196_comment_plan_safety"
branch_labels = None
depends_on = None


TABLE = "task_account_group_binding_set_revisions"
ACTIVE_INDEX = "uq_task_account_group_binding_active"
TENANT_INDEX = "ix_task_account_group_binding_tenant"
SNAPSHOT_TABLE = "account_group_membership_snapshot_sets"
SNAPSHOT_INDEX = "ix_account_group_membership_snapshot_binding"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_lifecycle_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("binding_set_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("account_selection_mode", sa.String(24), nullable=False, server_default="group"),
        sa.Column("account_group_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("concurrency_limit_per_group", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("group_contracts", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("binding_set_hash", sa.String(64), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(24), nullable=False, server_default="active"),
        sa.Column(
            "supersedes_revision_id",
            sa.String(36),
            sa.ForeignKey(f"{TABLE}.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "task_id",
            "task_lifecycle_epoch",
            "binding_set_revision",
            name="uq_task_account_group_binding_revision",
        ),
    )
    op.create_index(
        ACTIVE_INDEX,
        TABLE,
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
        sqlite_where=sa.text("state = 'active'"),
    )
    op.create_index(TENANT_INDEX, TABLE, ["tenant_id", "task_id"])
    op.create_table(
        SNAPSHOT_TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_lifecycle_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("binding_set_revision_id", sa.String(36), sa.ForeignKey(f"{TABLE}.id"), nullable=False),
        sa.Column("participation_unit", sa.String(160), nullable=False),
        sa.Column("snapshot_set_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("group_memberships", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("member_account_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("account_origin_groups", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("member_union_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="frozen"),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "task_id",
            "task_lifecycle_epoch",
            "participation_unit",
            "snapshot_set_revision",
            name="uq_account_group_membership_snapshot_revision",
        ),
    )
    op.create_index(
        SNAPSHOT_INDEX,
        SNAPSHOT_TABLE,
        ["binding_set_revision_id", "participation_unit"],
    )


def downgrade() -> None:
    op.drop_index(SNAPSHOT_INDEX, table_name=SNAPSHOT_TABLE)
    op.drop_table(SNAPSHOT_TABLE)
    op.drop_index(TENANT_INDEX, table_name=TABLE)
    op.drop_index(ACTIVE_INDEX, table_name=TABLE)
    op.drop_table(TABLE)
