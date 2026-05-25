"""login flow failure trace

Revision ID: 0053_login_flow_failure_trace
Revises: 0052_comment_author_identity
Create Date: 2026-05-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0053_login_flow_failure_trace"
down_revision = "0052_comment_author_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("tg_login_flows")}
    if "failure_type" not in columns:
        op.add_column("tg_login_flows", sa.Column("failure_type", sa.String(length=80), nullable=False, server_default=""))
        op.alter_column("tg_login_flows", "failure_type", server_default=None)
    if "failure_detail" not in columns:
        op.add_column("tg_login_flows", sa.Column("failure_detail", sa.Text(), nullable=False, server_default=""))
        op.alter_column("tg_login_flows", "failure_detail", server_default=None)
    if "trace_id" not in columns:
        op.add_column("tg_login_flows", sa.Column("trace_id", sa.String(length=80), nullable=False, server_default=""))
        op.alter_column("tg_login_flows", "trace_id", server_default=None)


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("tg_login_flows")}
    if "trace_id" in columns:
        op.drop_column("tg_login_flows", "trace_id")
    if "failure_detail" in columns:
        op.drop_column("tg_login_flows", "failure_detail")
    if "failure_type" in columns:
        op.drop_column("tg_login_flows", "failure_type")
