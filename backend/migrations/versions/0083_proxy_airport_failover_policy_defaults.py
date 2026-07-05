"""align proxy airport failover policy defaults

Revision ID: 0083_proxy_airport_policy
Revises: 0082_proxy_airport_failover_bind
Create Date: 2026-07-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0083_proxy_airport_policy"
down_revision = "0082_proxy_airport_failover_bind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _has_table("proxy_airport_subscriptions"):
        return
    op.execute(
        "UPDATE proxy_airport_subscriptions "
        "SET failover_policy = 'same_subscription_first' "
        "WHERE failover_policy = 'priority'"
    )
    op.execute(
        "UPDATE proxy_airport_subscriptions "
        "SET failback_cooldown_minutes = 1440 "
        "WHERE failback_cooldown_minutes = 0 AND auto_failback_enabled = false"
    )
    op.execute("UPDATE proxy_airport_subscriptions SET auto_failback_enabled = false")
    _set_server_default("failover_policy", "same_subscription_first")
    _set_server_default("failback_cooldown_minutes", "1440")


def downgrade() -> None:
    if not _has_table("proxy_airport_subscriptions"):
        return
    _set_server_default("failover_policy", "priority")
    _set_server_default("failback_cooldown_minutes", "0")


def _set_server_default(column_name: str, value: str) -> None:
    if _has_column("proxy_airport_subscriptions", column_name):
        op.alter_column("proxy_airport_subscriptions", column_name, server_default=value)


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    return column_name in {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}
