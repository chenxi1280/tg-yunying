"""add search join group observation tables

Revision ID: 0075_search_join_group
Revises: 0074_account_center_contract
Create Date: 2026-07-03
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0075_search_join_group"
down_revision = "0074_account_center_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_bot_protocol_samples()
    if not _has_table("search_join_rank_observations"):
        op.create_table(
            "search_join_rank_observations",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("task_id", sa.String(length=36), nullable=False),
            sa.Column("bot_username", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("keyword_hash", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("target_group_id", sa.Integer(), nullable=True),
            sa.Column("observed_position", sa.Integer(), nullable=True),
            sa.Column("total_results", sa.Integer(), nullable=True),
            sa.Column("observed_region", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("observation_source", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("paid_keyword_ad_status", sa.String(length=40), nullable=False, server_default="unknown"),
            sa.Column("jisou_ecosystem_status", sa.String(length=40), nullable=False, server_default="unknown"),
            sa.Column("target_relevance_score", sa.Integer(), nullable=True),
            sa.Column("target_content_health", sa.String(length=40), nullable=False, server_default="unknown"),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        )
    if not _has_table("search_join_linked_task_dispatches"):
        op.create_table(
            "search_join_linked_task_dispatches",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("search_join_action_id", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("source_task_id", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("linked_task_id", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("account_id", sa.Integer(), nullable=True),
            sa.Column("target_group_id", sa.Integer(), nullable=True),
            sa.Column("link_type", sa.String(length=40), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="linked_task_ready_pending"),
            sa.Column("block_reason", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("can_send_checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("activation_not_before", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ready_pool_item_id", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("detail", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    _create_indexes()


def downgrade() -> None:
    _drop_index_if_exists("ix_bot_protocol_samples_captured", "bot_protocol_samples")
    _drop_index_if_exists("ix_bot_protocol_samples_active", "bot_protocol_samples")
    _drop_index_if_exists("ix_search_join_linked_status", "search_join_linked_task_dispatches")
    _drop_index_if_exists("ix_search_join_linked_source", "search_join_linked_task_dispatches")
    _drop_index_if_exists("ix_search_join_rank_keyword", "search_join_rank_observations")
    _drop_index_if_exists("ix_search_join_rank_task_time", "search_join_rank_observations")
    if _has_table("search_join_linked_task_dispatches"):
        op.drop_table("search_join_linked_task_dispatches")
    if _has_table("search_join_rank_observations"):
        op.drop_table("search_join_rank_observations")
    if _has_table("bot_protocol_samples"):
        op.drop_table("bot_protocol_samples")


def _create_bot_protocol_samples() -> None:
    if _has_table("bot_protocol_samples"):
        return
    op.create_table(
        "bot_protocol_samples",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("bot_username", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("sample_type", sa.String(length=60), nullable=False, server_default="search_results"),
        sa.Column("sample_hash", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("schema_version", sa.String(length=40), nullable=False, server_default="v1"),
        sa.Column("structure_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pii_scrubbed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )


def _create_indexes() -> None:
    _create_index_if_missing("ix_bot_protocol_samples_active", "bot_protocol_samples", ["tenant_id", "bot_username", "sample_type", "is_active"])
    _create_index_if_missing("ix_bot_protocol_samples_captured", "bot_protocol_samples", ["tenant_id", "bot_username", "captured_at"])
    _create_index_if_missing("ix_search_join_rank_task_time", "search_join_rank_observations", ["tenant_id", "task_id", "observed_at"])
    _create_index_if_missing("ix_search_join_rank_keyword", "search_join_rank_observations", ["tenant_id", "keyword_hash", "observed_at"])
    _create_index_if_missing("ix_search_join_linked_source", "search_join_linked_task_dispatches", ["tenant_id", "source_task_id", "created_at"])
    _create_index_if_missing("ix_search_join_linked_status", "search_join_linked_task_dispatches", ["tenant_id", "status", "activation_not_before"])


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _index_names(table_name: str) -> set[str]:
    return {row["name"] for row in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _create_index_if_missing(name: str, table_name: str, columns: list[str]) -> None:
    if _has_table(table_name) and name not in _index_names(table_name):
        op.create_index(name, table_name, columns)


def _drop_index_if_exists(name: str, table_name: str) -> None:
    if _has_table(table_name) and name in _index_names(table_name):
        op.drop_index(name, table_name=table_name)
