"""add account mask environment app scope

Revision ID: 0078_account_mask_env_scope
Revises: 0077_search_join_pacing
Create Date: 2026-07-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0078_account_mask_env_scope"
down_revision = "0077_search_join_pacing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _add_environment_app_columns()
    _add_fingerprint_app_columns()
    _backfill_environment_app_scope()
    _replace_environment_unique_constraint()
    _create_proxy_airport_subscriptions()
    _create_proxy_airport_nodes()


def downgrade() -> None:
    _drop_table_if_exists("proxy_airport_nodes")
    _drop_table_if_exists("proxy_airport_subscriptions")
    _restore_environment_unique_constraint()
    _drop_column_if_exists("fingerprint_combo_history", "developer_app_api_id_snapshot")
    _drop_column_if_exists("fingerprint_combo_history", "developer_app_id")
    _drop_column_if_exists("account_environment_bindings", "developer_app_api_id_snapshot")
    _drop_column_if_exists("account_environment_bindings", "developer_app_id")


def _add_environment_app_columns() -> None:
    if not _has_column("account_environment_bindings", "developer_app_id"):
        op.add_column("account_environment_bindings", sa.Column("developer_app_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_account_environment_developer_app",
            "account_environment_bindings",
            "telegram_developer_apps",
            ["developer_app_id"],
            ["id"],
        )
    if not _has_column("account_environment_bindings", "developer_app_api_id_snapshot"):
        op.add_column(
            "account_environment_bindings",
            sa.Column("developer_app_api_id_snapshot", sa.Integer(), nullable=False, server_default="0"),
        )


def _add_fingerprint_app_columns() -> None:
    if not _has_column("fingerprint_combo_history", "developer_app_id"):
        op.add_column("fingerprint_combo_history", sa.Column("developer_app_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_fingerprint_combo_developer_app",
            "fingerprint_combo_history",
            "telegram_developer_apps",
            ["developer_app_id"],
            ["id"],
        )
    if not _has_column("fingerprint_combo_history", "developer_app_api_id_snapshot"):
        op.add_column(
            "fingerprint_combo_history",
            sa.Column("developer_app_api_id_snapshot", sa.Integer(), nullable=False, server_default="0"),
        )


def _backfill_environment_app_scope() -> None:
    if not _has_table("account_environment_bindings") or not _has_table("tg_account_authorizations"):
        return
    op.execute(
        sa.text(
            """
            UPDATE account_environment_bindings AS env
            SET
                developer_app_id = auth.developer_app_id,
                developer_app_api_id_snapshot = COALESCE(NULLIF(auth.developer_app_api_id_snapshot, 0), env.developer_app_api_id_snapshot, 0)
            FROM tg_account_authorizations AS auth
            WHERE env.authorization_id = auth.id
              AND env.account_id = auth.account_id
              AND env.tenant_id = auth.tenant_id
              AND env.session_role = auth.role
              AND env.developer_app_id IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE fingerprint_combo_history AS history
            SET
                developer_app_id = env.developer_app_id,
                developer_app_api_id_snapshot = env.developer_app_api_id_snapshot
            FROM account_environment_bindings AS env
            WHERE history.tenant_id = env.tenant_id
              AND history.account_id = env.account_id
              AND history.authorization_id = env.authorization_id
              AND history.session_role = env.session_role
              AND history.combo_key = env.client_identity_key
              AND history.developer_app_id IS NULL
            """
        )
    )


def _replace_environment_unique_constraint() -> None:
    if _has_unique("account_environment_bindings", "uq_account_environment_authorization_role"):
        op.drop_constraint(
            "uq_account_environment_authorization_role",
            "account_environment_bindings",
            type_="unique",
        )
    if not _has_unique("account_environment_bindings", "uq_account_environment_app_authorization_role"):
        op.create_unique_constraint(
            "uq_account_environment_app_authorization_role",
            "account_environment_bindings",
            ["tenant_id", "account_id", "developer_app_id", "authorization_id", "session_role"],
        )
    _create_index_if_missing(
        "ix_account_environment_app",
        "account_environment_bindings",
        ["tenant_id", "account_id", "developer_app_id", "session_role"],
    )


def _restore_environment_unique_constraint() -> None:
    if _has_unique("account_environment_bindings", "uq_account_environment_app_authorization_role"):
        op.drop_constraint(
            "uq_account_environment_app_authorization_role",
            "account_environment_bindings",
            type_="unique",
        )
    if not _has_unique("account_environment_bindings", "uq_account_environment_authorization_role"):
        op.create_unique_constraint(
            "uq_account_environment_authorization_role",
            "account_environment_bindings",
            ["tenant_id", "authorization_id", "session_role"],
        )


def _create_proxy_airport_subscriptions() -> None:
    if _has_table("proxy_airport_subscriptions"):
        return
    op.create_table(
        "proxy_airport_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("subscription_url_ciphertext", sa.Text(), nullable=False, server_default=""),
        sa.Column("subscription_url_preview", sa.String(length=180), nullable=False, server_default=""),
        sa.Column("provider_type", sa.String(length=40), nullable=False, server_default="clash"),
        sa.Column("sync_status", sa.String(length=30), nullable=False, server_default="not_synced"),
        sa.Column("node_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("healthy_node_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "is_active", name="uq_proxy_airport_active_subscription"),
    )


def _create_proxy_airport_nodes() -> None:
    if _has_table("proxy_airport_nodes"):
        return
    op.create_table(
        "proxy_airport_nodes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("proxy_airport_subscriptions.id"), nullable=False),
        sa.Column("node_key", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("node_name", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("protocol", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="unknown"),
        sa.Column("max_bound_accounts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "subscription_id", "node_key", name="uq_proxy_airport_node_key"),
    )


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return column_name in {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _has_unique(table_name: str, constraint_name: str) -> bool:
    if not _has_table(table_name):
        return False
    rows = sa.inspect(op.get_bind()).get_unique_constraints(table_name)
    return constraint_name in {row["name"] for row in rows}


def _create_index_if_missing(name: str, table_name: str, columns: list[str]) -> None:
    if name not in {row["name"] for row in sa.inspect(op.get_bind()).get_indexes(table_name)}:
        op.create_index(name, table_name, columns)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _has_column(table_name, column_name):
        op.drop_column(table_name, column_name)


def _drop_table_if_exists(table_name: str) -> None:
    if _has_table(table_name):
        op.drop_table(table_name)
