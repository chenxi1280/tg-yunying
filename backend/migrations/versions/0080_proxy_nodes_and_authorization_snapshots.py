"""persist proxy node connection data and snapshot slot scope

Revision ID: 0080_proxy_node_snapshot_scope
Revises: 0079_proxy_binding_slot_scope
Create Date: 2026-07-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0080_proxy_node_snapshot_scope"
down_revision = "0079_proxy_binding_slot_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _add_proxy_node_columns()
    _add_authorization_snapshot_scope_columns()
    _create_index_if_missing(
        "ix_tg_authorization_snapshot_slot",
        "tg_account_authorization_snapshots",
        ["tenant_id", "account_id", "authorization_id", "developer_app_id", "session_role"],
    )


def downgrade() -> None:
    _drop_index_if_exists("ix_tg_authorization_snapshot_slot", "tg_account_authorization_snapshots")
    _drop_column_if_exists("tg_account_authorization_snapshots", "session_role")
    _drop_column_if_exists("tg_account_authorization_snapshots", "developer_app_id")
    _drop_column_if_exists("tg_account_authorization_snapshots", "authorization_id")
    _drop_column_if_exists("proxy_airport_nodes", "node_config_ciphertext")
    _drop_column_if_exists("proxy_airport_nodes", "proxy_port")
    _drop_column_if_exists("proxy_airport_nodes", "proxy_host")


def _add_proxy_node_columns() -> None:
    if not _has_column("proxy_airport_nodes", "proxy_host"):
        op.add_column("proxy_airport_nodes", sa.Column("proxy_host", sa.String(length=255), nullable=False, server_default=""))
    if not _has_column("proxy_airport_nodes", "proxy_port"):
        op.add_column("proxy_airport_nodes", sa.Column("proxy_port", sa.Integer(), nullable=False, server_default="0"))
    if not _has_column("proxy_airport_nodes", "node_config_ciphertext"):
        op.add_column("proxy_airport_nodes", sa.Column("node_config_ciphertext", sa.Text(), nullable=False, server_default=""))


def _add_authorization_snapshot_scope_columns() -> None:
    if not _has_column("tg_account_authorization_snapshots", "authorization_id"):
        op.add_column("tg_account_authorization_snapshots", sa.Column("authorization_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_tg_authorization_snapshot_authorization",
            "tg_account_authorization_snapshots",
            "tg_account_authorizations",
            ["authorization_id"],
            ["id"],
        )
    if not _has_column("tg_account_authorization_snapshots", "developer_app_id"):
        op.add_column("tg_account_authorization_snapshots", sa.Column("developer_app_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_tg_authorization_snapshot_developer_app",
            "tg_account_authorization_snapshots",
            "telegram_developer_apps",
            ["developer_app_id"],
            ["id"],
        )
    if not _has_column("tg_account_authorization_snapshots", "session_role"):
        op.add_column("tg_account_authorization_snapshots", sa.Column("session_role", sa.String(length=24), nullable=False, server_default=""))


def _has_column(table_name: str, column_name: str) -> bool:
    return column_name in {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _create_index_if_missing(name: str, table_name: str, columns: list[str]) -> None:
    if name not in {row["name"] for row in sa.inspect(op.get_bind()).get_indexes(table_name)}:
        op.create_index(name, table_name, columns)


def _drop_index_if_exists(name: str, table_name: str) -> None:
    if name in {row["name"] for row in sa.inspect(op.get_bind()).get_indexes(table_name)}:
        op.drop_index(name, table_name=table_name)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _has_column(table_name, column_name):
        op.drop_column(table_name, column_name)
