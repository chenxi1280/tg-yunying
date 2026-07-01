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
    _add_column_if_missing(
        "ai_account_voice_profiles",
        sa.Column("mask_name", sa.String(length=80), nullable=False, server_default=""),
    )
    _add_column_if_missing(
        "ai_account_voice_profiles",
        sa.Column("audience_archetype", sa.String(length=120), nullable=False, server_default=""),
    )
    _add_column_if_missing(
        "ai_account_voice_profiles",
        sa.Column("identity_frame", sa.String(length=160), nullable=False, server_default=""),
    )
    _add_column_if_missing(
        "ai_account_voice_profiles",
        sa.Column("preference_tags", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    _drop_column_if_exists("ai_account_voice_profiles", "preference_tags")
    _drop_column_if_exists("ai_account_voice_profiles", "identity_frame")
    _drop_column_if_exists("ai_account_voice_profiles", "audience_archetype")
    _drop_column_if_exists("ai_account_voice_profiles", "mask_name")


def _column_names(table_name: str) -> set[str]:
    return {row["name"] for row in sa.inspect(op.get_bind()).get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _column_names(table_name):
        op.add_column(table_name, column)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if column_name in _column_names(table_name):
        op.drop_column(table_name, column_name)
