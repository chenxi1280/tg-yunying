"""add proxy airport failover binding facts

Revision ID: 0082_proxy_airport_failover_bind
Revises: 0081_proxy_airport_multi_source
Create Date: 2026-07-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0082_proxy_airport_failover_bind"
down_revision = "0081_proxy_airport_multi_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _add_binding_columns()
    _extend_failover_events()
    _create_exit_observations()
    _create_warmup_states()
    _create_index_if_missing("ix_account_proxy_binding_airport_node", "account_proxy_bindings", ["tenant_id", "proxy_airport_node_id", "status"])


def downgrade() -> None:
    _drop_index_if_exists("ix_account_proxy_binding_airport_node", "account_proxy_bindings")
    _drop_table_if_exists("account_proxy_warmup_states")
    _drop_table_if_exists("proxy_exit_ip_observations")
    for column in ["observed_error", "outcome", "session_role", "developer_app_id"]:
        _drop_column_if_exists("proxy_node_failover_events", column)
    for column in [
        "binding_generation",
        "last_failover_at",
        "observed_exit_isp",
        "observed_exit_asn",
        "observed_exit_country",
        "observed_exit_ip",
        "proxy_airport_node_id",
    ]:
        _drop_column_if_exists("account_proxy_bindings", column)


def _add_binding_columns() -> None:
    _add_column_if_missing("account_proxy_bindings", sa.Column("proxy_airport_node_id", sa.Integer(), sa.ForeignKey("proxy_airport_nodes.id"), nullable=True))
    _add_column_if_missing("account_proxy_bindings", sa.Column("observed_exit_ip", sa.String(length=64), nullable=False, server_default=""))
    _add_column_if_missing("account_proxy_bindings", sa.Column("observed_exit_country", sa.String(length=16), nullable=False, server_default=""))
    _add_column_if_missing("account_proxy_bindings", sa.Column("observed_exit_asn", sa.String(length=80), nullable=False, server_default=""))
    _add_column_if_missing("account_proxy_bindings", sa.Column("observed_exit_isp", sa.String(length=120), nullable=False, server_default=""))
    _add_column_if_missing("account_proxy_bindings", sa.Column("last_failover_at", sa.DateTime(), nullable=True))
    _add_column_if_missing("account_proxy_bindings", sa.Column("binding_generation", sa.Integer(), nullable=False, server_default="1"))


def _extend_failover_events() -> None:
    _add_column_if_missing("proxy_node_failover_events", sa.Column("developer_app_id", sa.Integer(), nullable=True))
    _add_column_if_missing("proxy_node_failover_events", sa.Column("session_role", sa.String(length=24), nullable=False, server_default=""))
    _add_column_if_missing("proxy_node_failover_events", sa.Column("outcome", sa.String(length=40), nullable=False, server_default=""))
    _add_column_if_missing("proxy_node_failover_events", sa.Column("observed_error", sa.Text(), nullable=False, server_default=""))


def _create_exit_observations() -> None:
    if _has_table("proxy_exit_ip_observations"):
        return
    op.create_table(
        "proxy_exit_ip_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("proxy_node_id", sa.Integer(), sa.ForeignKey("proxy_airport_nodes.id"), nullable=True),
        sa.Column("proxy_binding_id", sa.Integer(), sa.ForeignKey("account_proxy_bindings.id"), nullable=True),
        sa.Column("observed_exit_ip", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("observed_exit_country", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("observed_exit_asn", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("observed_exit_isp", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("check_source", sa.String(length=40), nullable=False, server_default="failover"),
        sa.Column("raw_response", sa.Text(), nullable=False, server_default=""),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def _create_warmup_states() -> None:
    if _has_table("account_proxy_warmup_states"):
        return
    op.create_table(
        "account_proxy_warmup_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("developer_app_id", sa.Integer(), sa.ForeignKey("telegram_developer_apps.id"), nullable=True),
        sa.Column("authorization_id", sa.Integer(), sa.ForeignKey("tg_account_authorizations.id"), nullable=True),
        sa.Column("session_role", sa.String(length=24), nullable=False, server_default=""),
        sa.Column("proxy_binding_id", sa.Integer(), sa.ForeignKey("account_proxy_bindings.id"), nullable=True),
        sa.Column("stage", sa.String(length=40), nullable=False, server_default="pending_warmup"),
        sa.Column("reset_reason", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("daily_actions_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_actions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage_started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("first_action_at", sa.DateTime(), nullable=True),
    )


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if not _has_column(table_name, column.name):
        op.add_column(table_name, column)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _has_column(table_name, column_name):
        op.drop_column(table_name, column_name)


def _create_index_if_missing(name: str, table_name: str, columns: list[str]) -> None:
    if name not in {row["name"] for row in sa.inspect(op.get_bind()).get_indexes(table_name)}:
        op.create_index(name, table_name, columns)


def _drop_index_if_exists(name: str, table_name: str) -> None:
    if name in {row["name"] for row in sa.inspect(op.get_bind()).get_indexes(table_name)}:
        op.drop_index(name, table_name=table_name)


def _drop_table_if_exists(table_name: str) -> None:
    if _has_table(table_name):
        op.drop_table(table_name)


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return column_name in {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}
