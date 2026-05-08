"""tg account soft delete

Revision ID: 0016_tg_account_soft_delete
Revises: 0015_processed_target_group
Create Date: 2026-05-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0016_tg_account_soft_delete"
down_revision = "0015_processed_target_group"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def _column_exists(table: str, column: str) -> bool:
    return column in {item["name"] for item in sa.inspect(_bind()).get_columns(table)}


def _index_exists(table: str, index_name: str) -> bool:
    return index_name in {item["name"] for item in sa.inspect(_bind()).get_indexes(table)}


def _drop_account_phone_uniques() -> None:
    inspector = sa.inspect(_bind())
    target_columns = {"tenant_id", "phone_masked"}
    for constraint in inspector.get_unique_constraints("tg_accounts"):
        if set(constraint.get("column_names") or []) == target_columns:
            name = constraint.get("name")
            if name:
                op.drop_constraint(name, "tg_accounts", type_="unique")
    for index in inspector.get_indexes("tg_accounts"):
        if set(index.get("column_names") or []) == target_columns and index.get("unique"):
            name = index.get("name")
            if name and name != "ux_tg_accounts_tenant_phone_active":
                op.drop_index(name, table_name="tg_accounts")


def upgrade() -> None:
    if not _table_exists("tg_accounts"):
        return
    if not _column_exists("tg_accounts", "deleted_at"):
        op.add_column("tg_accounts", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    if not _column_exists("tg_accounts", "deleted_by"):
        op.add_column("tg_accounts", sa.Column("deleted_by", sa.String(length=100), server_default="", nullable=False))
    if not _column_exists("tg_accounts", "delete_reason"):
        op.add_column("tg_accounts", sa.Column("delete_reason", sa.String(length=255), server_default="", nullable=False))
    _drop_account_phone_uniques()
    if not _index_exists("tg_accounts", "ux_tg_accounts_tenant_phone_active"):
        op.create_index(
            "ux_tg_accounts_tenant_phone_active",
            "tg_accounts",
            ["tenant_id", "phone_masked"],
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
        )


def downgrade() -> None:
    if not _table_exists("tg_accounts"):
        return
    if _index_exists("tg_accounts", "ux_tg_accounts_tenant_phone_active"):
        op.drop_index("ux_tg_accounts_tenant_phone_active", table_name="tg_accounts")
    if _column_exists("tg_accounts", "delete_reason"):
        op.drop_column("tg_accounts", "delete_reason")
    if _column_exists("tg_accounts", "deleted_by"):
        op.drop_column("tg_accounts", "deleted_by")
    if _column_exists("tg_accounts", "deleted_at"):
        op.drop_column("tg_accounts", "deleted_at")
    op.create_unique_constraint("uq_tg_accounts_tenant_phone", "tg_accounts", ["tenant_id", "phone_masked"])
