"""ai notifications and operation task plans

Revision ID: 0019_ai_notifications
Revises: 0018_drop_legacy_authorization
Create Date: 2026-05-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0019_ai_notifications"
down_revision = "0018_drop_legacy_authorization"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _inspector():
    return sa.inspect(_bind())


def _table_exists(name: str) -> bool:
    return _inspector().has_table(name)


def _column_exists(table: str, column: str) -> bool:
    if not _table_exists(table):
        return False
    return column in {item["name"] for item in _inspector().get_columns(table)}


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    if _table_exists(table) and not _column_exists(table, column.name):
        op.add_column(table, column)


def upgrade() -> None:
    _add_column_if_missing("tenants", sa.Column("telegram_bot_token_ciphertext", sa.Text(), nullable=False, server_default=""))
    _add_column_if_missing("tenants", sa.Column("admin_chat_id", sa.String(length=120), nullable=False, server_default=""))
    _add_column_if_missing("tenants", sa.Column("notify_ai_failures_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))

    _add_column_if_missing("operation_tasks", sa.Column("actual_quantity", sa.Integer(), nullable=False, server_default="1"))
    _add_column_if_missing("operation_tasks", sa.Column("quantity_jitter_ratio", sa.Integer(), nullable=False, server_default="15"))
    _add_column_if_missing("operation_tasks", sa.Column("content_mode", sa.String(length=20), nullable=False, server_default="literal"))
    if _table_exists("operation_tasks"):
        _bind().execute(
            sa.text(
                """
                UPDATE operation_tasks
                SET actual_quantity = quantity
                WHERE actual_quantity = 1 AND quantity IS NOT NULL AND quantity > 1
                """
            )
        )

    _add_column_if_missing("operation_task_attempts", sa.Column("content", sa.Text(), nullable=False, server_default=""))
    _add_column_if_missing("operation_task_attempts", sa.Column("reaction", sa.String(length=32), nullable=False, server_default=""))
    _add_column_if_missing("operation_task_attempts", sa.Column("idempotency_key", sa.String(length=100), nullable=False, server_default=""))
    _add_column_if_missing("operation_task_attempts", sa.Column("planned_delay_seconds", sa.Integer(), nullable=False, server_default="0"))
    _add_column_if_missing("operation_task_attempts", sa.Column("scheduled_at", sa.DateTime(), nullable=False, server_default=sa.func.now()))
    if _table_exists("operation_task_attempts") and _column_exists("operation_task_attempts", "executed_at"):
        op.alter_column("operation_task_attempts", "executed_at", existing_type=sa.DateTime(), nullable=True)
    if _table_exists("operation_task_attempts") and _table_exists("operation_tasks"):
        _bind().execute(
            sa.text(
                """
                UPDATE operation_task_attempts
                SET content = COALESCE((SELECT operation_tasks.content FROM operation_tasks WHERE operation_tasks.id = operation_task_attempts.task_id), '')
                WHERE content = ''
                """
            )
        )
        _bind().execute(
            sa.text(
                """
                UPDATE operation_task_attempts
                SET reaction = COALESCE((SELECT operation_tasks.reaction FROM operation_tasks WHERE operation_tasks.id = operation_task_attempts.task_id), '')
                WHERE reaction = ''
                """
            )
        )


def downgrade() -> None:
    for table, columns in (
        ("operation_task_attempts", ("scheduled_at", "planned_delay_seconds", "idempotency_key", "reaction", "content")),
        ("operation_tasks", ("content_mode", "quantity_jitter_ratio", "actual_quantity")),
        ("tenants", ("notify_ai_failures_enabled", "admin_chat_id", "telegram_bot_token_ciphertext")),
    ):
        if not _table_exists(table):
            continue
        for column in columns:
            if _column_exists(table, column):
                op.drop_column(table, column)
