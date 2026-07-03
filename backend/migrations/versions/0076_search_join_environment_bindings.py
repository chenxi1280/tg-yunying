"""add search join account environment bindings

Revision ID: 0076_search_join_environment
Revises: 0075_search_join_group
Create Date: 2026-07-03
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0076_search_join_environment"
down_revision = "0075_search_join_group"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_account_environment_bindings()
    _create_fingerprint_combo_history()


def downgrade() -> None:
    _drop_index_if_exists("ix_fingerprint_combo_history_account", "fingerprint_combo_history")
    _drop_index_if_exists("ix_account_environment_identity", "account_environment_bindings")
    _drop_index_if_exists("ix_account_environment_account", "account_environment_bindings")
    if _has_table("fingerprint_combo_history"):
        op.drop_table("fingerprint_combo_history")
    if _has_table("account_environment_bindings"):
        op.drop_table("account_environment_bindings")


def _create_account_environment_bindings() -> None:
    if _has_table("account_environment_bindings"):
        return
    op.create_table(
        "account_environment_bindings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("authorization_id", sa.Integer(), sa.ForeignKey("tg_account_authorizations.id"), nullable=False),
        sa.Column("session_role", sa.String(length=24), nullable=False, server_default="primary"),
        sa.Column("proxy_binding_id", sa.Integer(), sa.ForeignKey("account_proxy_bindings.id"), nullable=True),
        sa.Column("proxy_id", sa.Integer(), sa.ForeignKey("account_proxies.id"), nullable=True),
        sa.Column("device_model", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("system_version", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("app_version", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("platform", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("lang_code", sa.String(length=16), nullable=False, server_default="zh"),
        sa.Column("system_lang_code", sa.String(length=16), nullable=False, server_default="zh-CN"),
        sa.Column("lang_pack", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("region_code", sa.String(length=16), nullable=False, server_default="CN"),
        sa.Column("client_identity_key", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("fingerprint_locked", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("health_score", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("unbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "authorization_id", "session_role", name="uq_account_environment_authorization_role"),
    )
    _create_index_if_missing("ix_account_environment_account", "account_environment_bindings", ["tenant_id", "account_id", "status"])
    _create_index_if_missing("ix_account_environment_identity", "account_environment_bindings", ["tenant_id", "client_identity_key"])


def _create_fingerprint_combo_history() -> None:
    if _has_table("fingerprint_combo_history"):
        return
    op.create_table(
        "fingerprint_combo_history",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=True),
        sa.Column("authorization_id", sa.Integer(), sa.ForeignKey("tg_account_authorizations.id"), nullable=True),
        sa.Column("session_role", sa.String(length=24), nullable=False, server_default=""),
        sa.Column("combo_key", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("device_model", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("system_version", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("app_version", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("platform", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("lang_code", sa.String(length=16), nullable=False, server_default="zh"),
        sa.Column("system_lang_code", sa.String(length=16), nullable=False, server_default="zh-CN"),
        sa.Column("region_code", sa.String(length=16), nullable=False, server_default="CN"),
        sa.Column("usage_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_bound_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_bound_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="active"),
        sa.UniqueConstraint("tenant_id", "combo_key", name="uq_fingerprint_combo_history_key"),
    )
    _create_index_if_missing(
        "ix_fingerprint_combo_history_account",
        "fingerprint_combo_history",
        ["tenant_id", "account_id", "authorization_id"],
    )


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _index_names(table_name: str) -> set[str]:
    if not _has_table(table_name):
        return set()
    return {row["name"] for row in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _create_index_if_missing(name: str, table_name: str, columns: list[str]) -> None:
    if name not in _index_names(table_name):
        op.create_index(name, table_name, columns)


def _drop_index_if_exists(name: str, table_name: str) -> None:
    if name in _index_names(table_name):
        op.drop_index(name, table_name=table_name)
