"""add search rank deboost task tables and binding scope columns

Revision ID: 0084_search_rank_deboost
Revises: 0083_proxy_airport_policy
Create Date: 2026-07-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0084_search_rank_deboost"
down_revision = "0083_proxy_airport_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_exempt_groups()
    _create_action_stats()
    _create_group_proxy_bindings()
    _add_binding_scope_column()
    _add_sample_purpose_column()


def downgrade() -> None:
    _drop_column_if_exists("bot_protocol_samples", "sample_purpose")
    _drop_column_if_exists("account_proxy_bindings", "binding_scope")
    _drop_table_if_exists("account_group_proxy_bindings")
    _drop_table_if_exists("search_rank_deboost_action_stats")
    _drop_table_if_exists("search_rank_deboost_exempt_groups")


def _create_exempt_groups() -> None:
    if _has_table("search_rank_deboost_exempt_groups"):
        return
    op.create_table(
        "search_rank_deboost_exempt_groups",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("task_id", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("exempt_group_username", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("exempt_group_peer_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("exempt_group_title", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("exempt_group_match_strategy", sa.String(length=40), nullable=False, server_default="username"),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("selected_by", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("selection_audit_id", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("previous_exempt_group_username", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("previous_exempt_group_peer_id", sa.String(length=64), nullable=False, server_default=""),
        sa.UniqueConstraint("tenant_id", "task_id", name="uq_search_rank_deboost_exempt_group_task"),
    )
    _create_index_if_missing(
        "ix_search_rank_deboost_exempt_group_task",
        "search_rank_deboost_exempt_groups",
        ["tenant_id", "task_id"],
    )


def _create_action_stats() -> None:
    if _has_table("search_rank_deboost_action_stats"):
        return
    op.create_table(
        "search_rank_deboost_action_stats",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("task_id", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("action_id", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("account_pool_id", sa.Integer(), sa.ForeignKey("account_pools.id"), nullable=False),
        sa.Column("proxy_airport_node_id", sa.Integer(), sa.ForeignKey("proxy_airport_nodes.id"), nullable=True),
        sa.Column("observed_exit_ip", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("bot_username", sa.String(length=80), nullable=False, server_default="jisou"),
        sa.Column("keyword_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("competitor_group_username", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("competitor_group_peer_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("competitor_group_title", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("competitor_position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("button_hash", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("button_effect", sa.String(length=40), nullable=False, server_default="navigate_only"),
        sa.Column("join_button_detected", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("joined", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("dwell_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hour_bucket", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("skip_reason", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("join_button_violation", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    _create_index_if_missing(
        "ix_search_rank_deboost_stat_task_time",
        "search_rank_deboost_action_stats",
        ["tenant_id", "task_id", "captured_at"],
    )
    _create_index_if_missing(
        "ix_search_rank_deboost_stat_account_hour",
        "search_rank_deboost_action_stats",
        ["tenant_id", "account_id", "hour_bucket"],
    )
    _create_index_if_missing(
        "ix_search_rank_deboost_stat_group_hour",
        "search_rank_deboost_action_stats",
        ["tenant_id", "account_pool_id", "hour_bucket"],
    )


def _create_group_proxy_bindings() -> None:
    if _has_table("account_group_proxy_bindings"):
        return
    op.create_table(
        "account_group_proxy_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("account_pool_id", sa.Integer(), sa.ForeignKey("account_pools.id"), nullable=False),
        sa.Column("proxy_airport_node_id", sa.Integer(), sa.ForeignKey("proxy_airport_nodes.id"), nullable=False),
        sa.Column("binding_scope", sa.String(length=24), nullable=False, server_default="group"),
        sa.Column("observed_exit_ip", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("observed_exit_country", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("observed_exit_asn", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("observed_exit_isp", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("exit_ip_stability_score", sa.Float(), nullable=False, server_default="100.0"),
        sa.Column("health_score", sa.Float(), nullable=False, server_default="100.0"),
        sa.Column("last_failover_at", sa.DateTime(), nullable=True),
        sa.Column("binding_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="active"),
        sa.Column("change_reason", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("bound_by", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("bound_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("unbound_at", sa.DateTime(), nullable=True),
        sa.Column("last_health_check_at", sa.DateTime(), nullable=True),
    )
    _create_active_pool_unique_index()
    _create_index_if_missing(
        "ix_account_group_proxy_binding_node",
        "account_group_proxy_bindings",
        ["tenant_id", "proxy_airport_node_id", "status"],
    )
    _create_index_if_missing(
        "ix_account_group_proxy_binding_pool",
        "account_group_proxy_bindings",
        ["tenant_id", "account_pool_id", "status"],
    )


def _create_active_pool_unique_index() -> None:
    if not _has_table("account_group_proxy_bindings"):
        return
    if "uq_account_group_proxy_binding_active_pool" in _index_names("account_group_proxy_bindings"):
        return
    op.create_index(
        "uq_account_group_proxy_binding_active_pool",
        "account_group_proxy_bindings",
        ["tenant_id", "account_pool_id", "status"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND unbound_at IS NULL"),
        sqlite_where=sa.text("status = 'active' AND unbound_at IS NULL"),
    )


def _add_binding_scope_column() -> None:
    if not _has_table("account_proxy_bindings"):
        return
    if _has_column("account_proxy_bindings", "binding_scope"):
        return
    op.add_column(
        "account_proxy_bindings",
        sa.Column("binding_scope", sa.String(length=24), nullable=False, server_default="authorization_slot"),
    )
    op.execute(
        "UPDATE account_proxy_bindings SET binding_scope = 'authorization_slot' "
        "WHERE binding_scope IS NULL OR binding_scope = ''"
    )


def _add_sample_purpose_column() -> None:
    if not _has_table("bot_protocol_samples"):
        return
    if _has_column("bot_protocol_samples", "sample_purpose"):
        return
    op.add_column(
        "bot_protocol_samples",
        sa.Column("sample_purpose", sa.String(length=40), nullable=False, server_default="search_join"),
    )
    op.execute(
        "UPDATE bot_protocol_samples SET sample_purpose = 'search_join' "
        "WHERE sample_purpose IS NULL OR sample_purpose = ''"
    )


def _create_index_if_missing(name: str, table_name: str, columns: list[str]) -> None:
    if not _has_table(table_name):
        return
    if name in _index_names(table_name):
        return
    op.create_index(name, table_name, columns)


def _index_names(table_name: str) -> set[str]:
    if not _has_table(table_name):
        return set()
    return {row["name"] for row in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _has_column(table_name, column_name):
        op.drop_column(table_name, column_name)


def _drop_table_if_exists(table_name: str) -> None:
    if _has_table(table_name):
        op.drop_table(table_name)


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return column_name in {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}
