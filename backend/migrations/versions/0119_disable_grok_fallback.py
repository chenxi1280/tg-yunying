"""Disable Grok CLI fallback for AI group generation.

Revision ID: 0119_disable_grok_fallback
Revises: 0118_outbound_tgt_ref_markers
Create Date: 2026-07-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0119_disable_grok_fallback"
down_revision = "0118_outbound_tgt_ref_markers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "tenant_ai_settings" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("tenant_ai_settings")}
    if "ai_group_grok_fallback_enabled" not in columns:
        return
    op.execute(sa.text("UPDATE tenant_ai_settings SET ai_group_grok_fallback_enabled = false"))
    with op.batch_alter_table("tenant_ai_settings") as batch:
        batch.alter_column(
            "ai_group_grok_fallback_enabled",
            existing_type=sa.Boolean(),
            server_default=sa.false(),
            existing_nullable=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "tenant_ai_settings" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("tenant_ai_settings")}
    if "ai_group_grok_fallback_enabled" not in columns:
        return
    with op.batch_alter_table("tenant_ai_settings") as batch:
        batch.alter_column(
            "ai_group_grok_fallback_enabled",
            existing_type=sa.Boolean(),
            server_default=sa.true(),
            existing_nullable=False,
        )
