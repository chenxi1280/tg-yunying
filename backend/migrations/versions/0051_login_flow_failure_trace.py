"""login flow failure trace fields

Revision ID: 0051_login_flow_failure_trace
Revises: 0050_runtime_health_score
Create Date: 2026-05-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0051_login_flow_failure_trace"
down_revision = "0050_runtime_health_score"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return name in sa.inspect(_bind()).get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(column["name"] == column_name for column in sa.inspect(_bind()).get_columns(table_name))


def upgrade() -> None:
    if not _table_exists("tg_login_flows"):
        return
    if not _column_exists("tg_login_flows", "failure_type"):
        op.add_column("tg_login_flows", sa.Column("failure_type", sa.String(length=60), nullable=False, server_default=""))
    if not _column_exists("tg_login_flows", "failure_detail"):
        op.add_column("tg_login_flows", sa.Column("failure_detail", sa.Text(), nullable=False, server_default=""))
    if not _column_exists("tg_login_flows", "trace_id"):
        op.add_column("tg_login_flows", sa.Column("trace_id", sa.String(length=64), nullable=False, server_default=""))


def downgrade() -> None:
    if not _table_exists("tg_login_flows"):
        return
    for column_name in ("trace_id", "failure_detail", "failure_type"):
        if _column_exists("tg_login_flows", column_name):
            op.drop_column("tg_login_flows", column_name)
