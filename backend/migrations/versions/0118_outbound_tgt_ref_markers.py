"""Freeze legacy outbound target references and persist gateway markers.

Revision ID: 0118_outbound_tgt_ref_markers
Revises: 0117_ai_continuity_rollout
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0118_outbound_tgt_ref_markers"
down_revision = "0117_ai_continuity_rollout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("message_tasks", sa.Column("operation_target_id", sa.Integer(), sa.ForeignKey("operation_targets.id"), nullable=True))
    op.add_column("message_tasks", sa.Column("target_reference_revision", sa.Integer(), nullable=True))
    op.add_column("message_tasks", sa.Column("target_reference_snapshot", sa.JSON(), nullable=True))
    op.add_column("message_tasks", sa.Column("gateway_call_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("operation_tasks", sa.Column("target_reference_revision", sa.Integer(), nullable=True))
    op.add_column("operation_tasks", sa.Column("target_reference_snapshot", sa.JSON(), nullable=True))
    op.add_column("operation_task_attempts", sa.Column("gateway_call_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("manual_operation_records", sa.Column("gateway_call_started_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_message_tasks_target_lifecycle",
        "message_tasks",
        ["tenant_id", "operation_target_id", "target_reference_revision", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_message_tasks_target_lifecycle", table_name="message_tasks")
    with op.batch_alter_table("manual_operation_records") as batch:
        batch.drop_column("gateway_call_started_at")
    with op.batch_alter_table("operation_task_attempts") as batch:
        batch.drop_column("gateway_call_started_at")
    with op.batch_alter_table("operation_tasks") as batch:
        batch.drop_column("target_reference_snapshot")
        batch.drop_column("target_reference_revision")
    with op.batch_alter_table("message_tasks") as batch:
        batch.drop_column("gateway_call_started_at")
        batch.drop_column("target_reference_snapshot")
        batch.drop_column("target_reference_revision")
        batch.drop_column("operation_target_id")
