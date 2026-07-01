"""add account mask fields

Revision ID: 0073_account_mask_fields
Revises: 0072_required_rule_binding
Create Date: 2026-07-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0073_account_mask_fields"
down_revision = "0072_required_rule_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_account_voice_profiles", sa.Column("mask_name", sa.String(length=80), nullable=False, server_default=""))
    op.add_column("ai_account_voice_profiles", sa.Column("audience_archetype", sa.String(length=120), nullable=False, server_default=""))
    op.add_column("ai_account_voice_profiles", sa.Column("identity_frame", sa.String(length=160), nullable=False, server_default=""))
    op.add_column("ai_account_voice_profiles", sa.Column("preference_tags", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    op.drop_column("ai_account_voice_profiles", "preference_tags")
    op.drop_column("ai_account_voice_profiles", "identity_frame")
    op.drop_column("ai_account_voice_profiles", "audience_archetype")
    op.drop_column("ai_account_voice_profiles", "mask_name")
