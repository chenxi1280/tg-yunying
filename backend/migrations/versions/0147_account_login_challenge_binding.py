"""Bind account login challenges to durable flow state.

Revision ID: 0147_login_challenge_binding
Revises: 0146_ai_reply_remote_fact_index
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0147_login_challenge_binding"
down_revision = "0146_ai_reply_remote_fact_index"
branch_labels = None
depends_on = None


TABLE_NAME = "tg_login_flows"
FLOW_COLUMNS = (
    sa.Column("flow_version", sa.Integer(), nullable=False, server_default="1"),
    sa.Column("challenge_sent_at", sa.DateTime(), nullable=True),
    sa.Column("temporary_session_ciphertext", sa.Text(), nullable=True),
    sa.Column("phone_code_hash_ciphertext", sa.Text(), nullable=True),
    sa.Column("superseded_by_flow_id", sa.Integer(), nullable=True),
    sa.Column("remote_error_type", sa.String(length=80), nullable=False, server_default=""),
)


def _column_exists(column_name: str) -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(TABLE_NAME)
    return any(column["name"] == column_name for column in columns)


def upgrade() -> None:
    for column in FLOW_COLUMNS:
        if not _column_exists(column.name):
            op.add_column(TABLE_NAME, column)


def downgrade() -> None:
    for column in reversed(FLOW_COLUMNS):
        if _column_exists(column.name):
            op.drop_column(TABLE_NAME, column.name)
