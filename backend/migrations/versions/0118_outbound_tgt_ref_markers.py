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
    _add_column_if_missing(
        "message_tasks",
        sa.Column("operation_target_id", sa.Integer(), sa.ForeignKey("operation_targets.id"), nullable=True),
    )
    _add_column_if_missing("message_tasks", sa.Column("target_reference_revision", sa.Integer(), nullable=True))
    _add_column_if_missing("message_tasks", sa.Column("target_reference_snapshot", sa.JSON(), nullable=True))
    _add_column_if_missing(
        "message_tasks",
        sa.Column("gateway_call_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    _add_column_if_missing("operation_tasks", sa.Column("target_reference_revision", sa.Integer(), nullable=True))
    _add_column_if_missing("operation_tasks", sa.Column("target_reference_snapshot", sa.JSON(), nullable=True))
    _add_column_if_missing(
        "operation_task_attempts",
        sa.Column("gateway_call_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    _add_column_if_missing(
        "manual_operation_records",
        sa.Column("gateway_call_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    if not _has_index("message_tasks", "ix_message_tasks_target_lifecycle"):
        op.create_index(
            "ix_message_tasks_target_lifecycle",
            "message_tasks",
            ["tenant_id", "operation_target_id", "target_reference_revision", "status"],
        )


def downgrade() -> None:
    if _has_index("message_tasks", "ix_message_tasks_target_lifecycle"):
        op.drop_index("ix_message_tasks_target_lifecycle", table_name="message_tasks")
    with op.batch_alter_table("manual_operation_records") as batch:
        if _has_column("manual_operation_records", "gateway_call_started_at"):
            batch.drop_column("gateway_call_started_at")
    with op.batch_alter_table("operation_task_attempts") as batch:
        if _has_column("operation_task_attempts", "gateway_call_started_at"):
            batch.drop_column("gateway_call_started_at")
    with op.batch_alter_table("operation_tasks") as batch:
        for column in ("target_reference_snapshot", "target_reference_revision"):
            if _has_column("operation_tasks", column):
                batch.drop_column(column)
    with op.batch_alter_table("message_tasks") as batch:
        for column in (
            "gateway_call_started_at",
            "target_reference_snapshot",
            "target_reference_revision",
            "operation_target_id",
        ):
            if _has_column("message_tasks", column):
                batch.drop_column(column)


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(table_name: str, column_name: str) -> bool:
    if table_name not in _inspector().get_table_names():
        return False
    return any(column["name"] == column_name for column in _inspector().get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    if table_name not in _inspector().get_table_names():
        return False
    return any(index["name"] == index_name for index in _inspector().get_indexes(table_name))


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if _has_column(table_name, column.name):
        return
    op.add_column(table_name, column)
