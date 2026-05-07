"""continuous campaigns and keyword rules

Revision ID: 0013_continuous_campaigns
Revises: 0012_group_listener_auto_reply
Create Date: 2026-05-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0013_continuous_campaigns"
down_revision = "0012_group_listener_auto_reply"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def _column_exists(table: str, column: str) -> bool:
    return column in {item["name"] for item in sa.inspect(_bind()).get_columns(table)}


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    if _table_exists(table) and not _column_exists(table, column.name):
        op.add_column(table, column)


def upgrade() -> None:
    for column in [
        sa.Column("source_group_ids", sa.Text(), nullable=False, server_default=""),
        sa.Column("execution_mode", sa.String(length=40), nullable=False, server_default="manual_draft"),
        sa.Column("run_interval_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("ends_at", sa.DateTime(), nullable=True),
        sa.Column("max_ai_tokens", sa.Integer(), nullable=True, server_default="100000"),
        sa.Column("used_ai_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("participation_min_ratio", sa.Float(), nullable=False, server_default="0.6"),
        sa.Column("participation_max_ratio", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("max_messages_per_account", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("max_drafts_per_batch", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("filtered_count", sa.Integer(), nullable=False, server_default="0"),
    ]:
        _add_column_if_missing("campaigns", column)

    if not _table_exists("content_keyword_rules"):
        op.create_table(
            "content_keyword_rules",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("keyword", sa.String(length=160), nullable=False),
            sa.Column("match_type", sa.String(length=40), nullable=False, server_default="contains"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("note", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("tenant_id", "keyword"),
        )
    op.create_index("ix_content_keyword_rules_tenant_active", "content_keyword_rules", ["tenant_id", "is_active"], if_not_exists=True)

    if not _table_exists("campaign_processed_messages"):
        op.create_table(
            "campaign_processed_messages",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id"), nullable=False),
            sa.Column("source_group_id", sa.Integer(), sa.ForeignKey("tg_groups.id"), nullable=False),
            sa.Column("source_remote_message_id", sa.String(length=160), nullable=False),
            sa.Column("action", sa.String(length=40), nullable=False, server_default="queued"),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("campaign_id", "source_group_id", "source_remote_message_id"),
        )
    op.create_index("ix_campaign_processed_messages_campaign", "campaign_processed_messages", ["campaign_id"], if_not_exists=True)


def downgrade() -> None:
    if _table_exists("campaign_processed_messages"):
        op.drop_index("ix_campaign_processed_messages_campaign", table_name="campaign_processed_messages", if_exists=True)
        op.drop_table("campaign_processed_messages")
    if _table_exists("content_keyword_rules"):
        op.drop_index("ix_content_keyword_rules_tenant_active", table_name="content_keyword_rules", if_exists=True)
        op.drop_table("content_keyword_rules")
    for name in [
        "filtered_count",
        "max_drafts_per_batch",
        "max_messages_per_account",
        "participation_max_ratio",
        "participation_min_ratio",
        "last_error",
        "last_run_at",
        "used_ai_tokens",
        "max_ai_tokens",
        "ends_at",
        "run_interval_seconds",
        "execution_mode",
        "source_group_ids",
    ]:
        if _column_exists("campaigns", name):
            op.drop_column("campaigns", name)
