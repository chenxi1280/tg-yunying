"""operational completion contacts and indexes

Revision ID: 0007_operational
Revises: 0006_profile_sync
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0007_operational"
down_revision = "0006_profile_sync"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def upgrade() -> None:
    if not _table_exists("tg_contacts"):
        op.create_table(
            "tg_contacts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
            sa.Column("peer_id", sa.String(length=120), nullable=False),
            sa.Column("display_name", sa.String(length=160), nullable=False),
            sa.Column("username", sa.String(length=120), nullable=True),
            sa.Column("phone_masked", sa.String(length=60), nullable=False, server_default=""),
            sa.Column("contact_type", sa.String(length=40), nullable=False, server_default="private"),
            sa.Column("is_mutual", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("last_message_at", sa.DateTime(), nullable=True),
            sa.Column("last_synced_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("account_id", "peer_id"),
        )


def downgrade() -> None:
    if _table_exists("tg_contacts"):
        op.drop_table("tg_contacts")
