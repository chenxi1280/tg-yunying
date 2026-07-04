"""add slot scope to account proxy bindings

Revision ID: 0079_proxy_binding_slot_scope
Revises: 0078_account_mask_env_scope
Create Date: 2026-07-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0079_proxy_binding_slot_scope"
down_revision = "0078_account_mask_env_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _add_slot_columns()
    _backfill_slot_scope_from_environment()
    _create_index_if_missing(
        "ix_account_proxy_binding_slot",
        "account_proxy_bindings",
        ["tenant_id", "account_id", "developer_app_id", "authorization_id", "session_role", "status"],
    )
    _create_active_slot_unique_index()


def downgrade() -> None:
    _drop_index_if_exists("uq_account_proxy_binding_active_slot", "account_proxy_bindings")
    _drop_index_if_exists("ix_account_proxy_binding_slot", "account_proxy_bindings")
    _drop_column_if_exists("account_proxy_bindings", "session_role")
    _drop_column_if_exists("account_proxy_bindings", "authorization_id")
    _drop_column_if_exists("account_proxy_bindings", "developer_app_api_id_snapshot")
    _drop_column_if_exists("account_proxy_bindings", "developer_app_id")


def _add_slot_columns() -> None:
    if not _has_column("account_proxy_bindings", "developer_app_id"):
        op.add_column("account_proxy_bindings", sa.Column("developer_app_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_account_proxy_binding_developer_app",
            "account_proxy_bindings",
            "telegram_developer_apps",
            ["developer_app_id"],
            ["id"],
        )
    if not _has_column("account_proxy_bindings", "developer_app_api_id_snapshot"):
        op.add_column("account_proxy_bindings", sa.Column("developer_app_api_id_snapshot", sa.Integer(), nullable=False, server_default="0"))
    if not _has_column("account_proxy_bindings", "authorization_id"):
        op.add_column("account_proxy_bindings", sa.Column("authorization_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_account_proxy_binding_authorization",
            "account_proxy_bindings",
            "tg_account_authorizations",
            ["authorization_id"],
            ["id"],
        )
    if not _has_column("account_proxy_bindings", "session_role"):
        op.add_column("account_proxy_bindings", sa.Column("session_role", sa.String(length=24), nullable=False, server_default=""))


def _backfill_slot_scope_from_environment() -> None:
    if not _has_table("account_environment_bindings") or not _has_table("account_proxy_bindings"):
        return
    op.execute(
        sa.text(
            """
            UPDATE account_proxy_bindings AS binding
            SET
                developer_app_id = env.developer_app_id,
                developer_app_api_id_snapshot = env.developer_app_api_id_snapshot,
                authorization_id = env.authorization_id,
                session_role = env.session_role
            FROM account_environment_bindings AS env
            WHERE env.proxy_binding_id = binding.id
              AND binding.developer_app_id IS NULL
              AND binding.authorization_id IS NULL
              AND binding.session_role = ''
            """
        )
    )


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return column_name in {row["name"] for row in sa.inspect(op.get_bind()).get_columns(table_name)}


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _has_column(table_name, column_name):
        op.drop_column(table_name, column_name)


def _index_names(table_name: str) -> set[str]:
    if not _has_table(table_name):
        return set()
    return {row["name"] for row in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _create_index_if_missing(name: str, table_name: str, columns: list[str]) -> None:
    if name not in _index_names(table_name):
        op.create_index(name, table_name, columns)


def _create_active_slot_unique_index() -> None:
    if "uq_account_proxy_binding_active_slot" in _index_names("account_proxy_bindings"):
        return
    op.create_index(
        "uq_account_proxy_binding_active_slot",
        "account_proxy_bindings",
        ["tenant_id", "account_id", "developer_app_id", "authorization_id", "session_role"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND unbound_at IS NULL AND developer_app_id IS NOT NULL AND authorization_id IS NOT NULL AND session_role != ''"),
        sqlite_where=sa.text("status = 'active' AND unbound_at IS NULL AND developer_app_id IS NOT NULL AND authorization_id IS NOT NULL AND session_role != ''"),
    )


def _drop_index_if_exists(name: str, table_name: str) -> None:
    if name in _index_names(table_name):
        op.drop_index(name, table_name=table_name)
