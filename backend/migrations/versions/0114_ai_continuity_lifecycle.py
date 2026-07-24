"""AI group send continuity: target lifecycle, send_limit_mode, hard hourly ledger

Revision ID: 0114_ai_continuity_lifecycle
Revises: 0113_group_send_slot_lookup
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0114_ai_continuity_lifecycle"
down_revision = "0113_group_send_slot_lookup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("operation_targets", sa.Column("lifecycle_status", sa.String(length=40), server_default="active", nullable=False))
    op.add_column("operation_targets", sa.Column("lifecycle_reason", sa.String(length=500), server_default="", nullable=False))
    op.add_column("operation_targets", sa.Column("lifecycle_detail", sa.Text(), server_default="", nullable=False))
    op.add_column("operation_targets", sa.Column("lifecycle_at", sa.DateTime(), nullable=True))
    op.add_column("operation_targets", sa.Column("lifecycle_by", sa.String(length=100), server_default="", nullable=False))
    op.add_column("operation_targets", sa.Column("lifecycle_version", sa.Integer(), server_default="1", nullable=False))
    op.add_column("operation_targets", sa.Column("reference_revision", sa.Integer(), server_default="1", nullable=False))

    op.add_column("tasks", sa.Column("config_revision", sa.Integer(), server_default="1", nullable=False))

    op.add_column(
        "tg_groups",
        sa.Column("send_limit_mode", sa.String(length=60), server_default="legacy_group_slot", nullable=True),
    )
    op.execute("UPDATE tg_groups SET send_limit_mode = 'legacy_group_slot' WHERE send_limit_mode IS NULL")
    with op.batch_alter_table("tg_groups") as batch:
        batch.alter_column("send_limit_mode", nullable=False, server_default="legacy_group_slot")

    op.create_table(
        "task_hard_hourly_buckets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("operation_target_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("target_reference_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("bucket_key", sa.String(length=80), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bucket_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(length=50), nullable=False, server_default="Asia/Shanghai"),
        sa.Column("goal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("task_config_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("terminal_blocker_code", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "task_id",
            "operation_target_id",
            "target_reference_revision",
            "bucket_key",
            name="uq_hard_hourly_bucket_epoch",
        ),
    )
    op.create_index(
        "ix_hard_hourly_bucket_lookup",
        "task_hard_hourly_buckets",
        ["tenant_id", "task_id", "operation_target_id", "target_reference_revision", "bucket_start"],
    )

    op.create_table(
        "task_hard_hourly_delivery_credits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("bucket_id", sa.Integer(), sa.ForeignKey("task_hard_hourly_buckets.id"), nullable=False),
        sa.Column("action_id", sa.String(length=36), sa.ForeignKey("actions.id"), nullable=False),
        sa.Column("execution_attempt_id", sa.Integer(), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("remote_message_id", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("action_id", name="uq_hard_hourly_credit_action"),
    )
    op.create_index(
        "ix_hard_hourly_credit_bucket_executed",
        "task_hard_hourly_delivery_credits",
        ["bucket_id", "executed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_hard_hourly_credit_bucket_executed", table_name="task_hard_hourly_delivery_credits")
    op.drop_table("task_hard_hourly_delivery_credits")
    op.drop_index("ix_hard_hourly_bucket_lookup", table_name="task_hard_hourly_buckets")
    op.drop_table("task_hard_hourly_buckets")
    with op.batch_alter_table("tg_groups") as batch:
        batch.drop_column("send_limit_mode")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("config_revision")
    with op.batch_alter_table("operation_targets") as batch:
        batch.drop_column("reference_revision")
        batch.drop_column("lifecycle_version")
        batch.drop_column("lifecycle_by")
        batch.drop_column("lifecycle_at")
        batch.drop_column("lifecycle_detail")
        batch.drop_column("lifecycle_reason")
        batch.drop_column("lifecycle_status")
