"""expand tenant admin chat ids

Revision ID: 0069_expand_admin_chat_ids
Revises: 0068_tenant_bot_webhook_status
Create Date: 2026-06-28 02:30:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0069_expand_admin_chat_ids"
down_revision = "0068_tenant_bot_webhook_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("tenants", "admin_chat_id", existing_type=sa.String(length=120), type_=sa.String(length=1000), existing_nullable=False)


def downgrade() -> None:
    op.alter_column("tenants", "admin_chat_id", existing_type=sa.String(length=1000), type_=sa.String(length=120), existing_nullable=False)
