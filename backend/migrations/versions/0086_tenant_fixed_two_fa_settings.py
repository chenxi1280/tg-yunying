"""add tenant fixed two fa settings

Revision ID: 0086_tenant_fixed_two_fa
Revises: 0085_search_rank_deboost_alerts
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0086_tenant_fixed_two_fa"
down_revision = "0085_search_rank_deboost_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _has_table("tenants"):
        return
    _add_column_if_missing("fixed_two_fa_password_ciphertext", sa.Column("fixed_two_fa_password_ciphertext", sa.Text(), nullable=False, server_default=""))
    _add_column_if_missing("fixed_two_fa_password_set_at", sa.Column("fixed_two_fa_password_set_at", sa.DateTime(), nullable=True))
    _add_column_if_missing("fixed_two_fa_password_set_by", sa.Column("fixed_two_fa_password_set_by", sa.String(length=80), nullable=False, server_default=""))


def downgrade() -> None:
    if not _has_table("tenants"):
        return
    _drop_column_if_exists("fixed_two_fa_password_set_by")
    _drop_column_if_exists("fixed_two_fa_password_set_at")
    _drop_column_if_exists("fixed_two_fa_password_ciphertext")


def _add_column_if_missing(column_name: str, column: sa.Column) -> None:
    if not _has_column(column_name):
        op.add_column("tenants", column)


def _drop_column_if_exists(column_name: str) -> None:
    if _has_column(column_name):
        op.drop_column("tenants", column_name)


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(column_name: str) -> bool:
    return column_name in {column["name"] for column in sa.inspect(op.get_bind()).get_columns("tenants")}
