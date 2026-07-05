"""support multiple proxy airport subscriptions

Revision ID: 0081_proxy_airport_multi_source
Revises: 0080_proxy_node_snapshot_scope
Create Date: 2026-07-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0081_proxy_airport_multi_source"
down_revision = "0080_proxy_node_snapshot_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _drop_constraint_if_exists("proxy_airport_subscriptions", "uq_proxy_airport_active_subscription")
    _add_subscription_columns()
    _add_node_observation_columns()
    _create_failover_events()
    _create_index_if_missing("ix_proxy_airport_subscription_priority", "proxy_airport_subscriptions", ["tenant_id", "enabled", "priority"])


def downgrade() -> None:
    _drop_index_if_exists("ix_proxy_airport_subscription_priority", "proxy_airport_subscriptions")
    _drop_table_if_exists("proxy_node_failover_events")
    for column in ["observed_exit_isp", "observed_exit_asn", "observed_exit_country", "observed_exit_ip"]:
        _drop_column_if_exists("proxy_airport_nodes", column)
    for column in [
        "notify_admin_on_all_subscriptions_down",
        "all_subscriptions_down_policy",
        "failback_cooldown_minutes",
        "auto_failback_enabled",
        "failover_policy",
        "enabled",
        "priority",
        "name",
    ]:
        _drop_column_if_exists("proxy_airport_subscriptions", column)
    _create_unique_if_missing("proxy_airport_subscriptions", "uq_proxy_airport_active_subscription", ["tenant_id", "is_active"])


def _add_subscription_columns() -> None:
    _add_column_if_missing("proxy_airport_subscriptions", sa.Column("name", sa.String(length=80), nullable=False, server_default="主订阅"))
    _add_column_if_missing("proxy_airport_subscriptions", sa.Column("priority", sa.Integer(), nullable=False, server_default="10"))
    _add_column_if_missing("proxy_airport_subscriptions", sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")))
    _add_column_if_missing("proxy_airport_subscriptions", sa.Column("failover_policy", sa.String(length=40), nullable=False, server_default="same_subscription_first"))
    _add_column_if_missing("proxy_airport_subscriptions", sa.Column("auto_failback_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    _add_column_if_missing("proxy_airport_subscriptions", sa.Column("failback_cooldown_minutes", sa.Integer(), nullable=False, server_default="1440"))
    _add_column_if_missing("proxy_airport_subscriptions", sa.Column("all_subscriptions_down_policy", sa.String(length=40), nullable=False, server_default="pause_task"))
    _add_column_if_missing("proxy_airport_subscriptions", sa.Column("notify_admin_on_all_subscriptions_down", sa.Boolean(), nullable=False, server_default=sa.text("true")))


def _add_node_observation_columns() -> None:
    _add_column_if_missing("proxy_airport_nodes", sa.Column("observed_exit_ip", sa.String(length=64), nullable=False, server_default=""))
    _add_column_if_missing("proxy_airport_nodes", sa.Column("observed_exit_country", sa.String(length=16), nullable=False, server_default=""))
    _add_column_if_missing("proxy_airport_nodes", sa.Column("observed_exit_asn", sa.String(length=80), nullable=False, server_default=""))
    _add_column_if_missing("proxy_airport_nodes", sa.Column("observed_exit_isp", sa.String(length=120), nullable=False, server_default=""))


def _create_failover_events() -> None:
    if _has_table("proxy_node_failover_events"):
        return
    op.create_table(
        "proxy_node_failover_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("authorization_id", sa.Integer(), nullable=True),
        sa.Column("from_subscription_id", sa.Integer(), nullable=True),
        sa.Column("to_subscription_id", sa.Integer(), nullable=True),
        sa.Column("from_node_id", sa.Integer(), nullable=True),
        sa.Column("to_node_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
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


def _create_unique_if_missing(table_name: str, name: str, columns: list[str]) -> None:
    if name not in {row["name"] for row in sa.inspect(op.get_bind()).get_unique_constraints(table_name)}:
        op.create_unique_constraint(name, table_name, columns)


def _drop_constraint_if_exists(table_name: str, name: str) -> None:
    if name in {row["name"] for row in sa.inspect(op.get_bind()).get_unique_constraints(table_name)}:
        op.drop_constraint(name, table_name, type_="unique")


def _drop_table_if_exists(table_name: str) -> None:
    if _has_table(table_name):
        op.drop_table(table_name)


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    return column_name in {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}
