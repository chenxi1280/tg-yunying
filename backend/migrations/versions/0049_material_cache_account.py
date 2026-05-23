"""material cache execution account

Revision ID: 0049_material_cache_account
Revises: 0048_material_groups
Create Date: 2026-05-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0049_material_cache_account"
down_revision = "0048_material_groups"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(column["name"] == column_name for column in sa.inspect(_bind()).get_columns(table_name))


def upgrade() -> None:
    if _table_exists("material_cache_configs") and not _column_exists("material_cache_configs", "material_cache_account_id"):
        op.add_column("material_cache_configs", sa.Column("material_cache_account_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_material_cache_configs_material_cache_account",
            "material_cache_configs",
            "tg_accounts",
            ["material_cache_account_id"],
            ["id"],
        )


def downgrade() -> None:
    if _column_exists("material_cache_configs", "material_cache_account_id"):
        op.drop_constraint("fk_material_cache_configs_material_cache_account", "material_cache_configs", type_="foreignkey")
        op.drop_column("material_cache_configs", "material_cache_account_id")
