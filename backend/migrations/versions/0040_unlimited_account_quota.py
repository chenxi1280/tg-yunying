"""make tenant account quota unlimited by default

Revision ID: 0040_unlimited_account_quota
Revises: 0039_sender_identity
Create Date: 2026-05-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0040_unlimited_account_quota"
down_revision = "0039_sender_identity"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _columns(table_name: str) -> set[str]:
    inspector = sa.inspect(_bind())
    if not inspector.has_table(table_name):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if "account_quota" not in _columns("tenants"):
        return
    op.alter_column("tenants", "account_quota", server_default="0")
    op.execute("UPDATE tenants SET account_quota = 0 WHERE account_quota > 0")


def downgrade() -> None:
    if "account_quota" not in _columns("tenants"):
        return
    op.alter_column("tenants", "account_quota", server_default=None)
