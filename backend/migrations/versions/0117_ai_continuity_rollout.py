"""Add explicit rollout state for target gate and continuity ledger.

Revision ID: 0117_ai_continuity_rollout
Revises: 0116_dispatch_fairness_cursor
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0117_ai_continuity_rollout"
down_revision = "0116_dispatch_fairness_cursor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _add_column_if_missing(
        "scheduling_settings",
        sa.Column("outbound_target_gate_mode", sa.String(length=20), server_default="dual_read", nullable=False),
    )
    _add_column_if_missing(
        "scheduling_settings",
        sa.Column("ai_group_send_continuity_v1", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    _add_column_if_missing(
        "scheduling_settings",
        sa.Column("ai_group_continuity_release_anchor", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("scheduling_settings") as batch:
        for column in (
            "ai_group_continuity_release_anchor",
            "ai_group_send_continuity_v1",
            "outbound_target_gate_mode",
        ):
            if _has_column("scheduling_settings", column):
                batch.drop_column(column)


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(table_name: str, column_name: str) -> bool:
    tables = _inspector().get_table_names()
    if table_name not in tables:
        return False
    return any(column["name"] == column_name for column in _inspector().get_columns(table_name))


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if _has_column(table_name, column.name):
        return
    op.add_column(table_name, column)
