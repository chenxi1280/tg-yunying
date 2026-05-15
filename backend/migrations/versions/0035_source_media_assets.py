"""source media assets

Revision ID: 0035_source_media_assets
Revises: 0034_material_media_fields
Create Date: 2026-05-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0035_source_media_assets"
down_revision = "0034_material_media_fields"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def upgrade() -> None:
    if _table_exists("source_media_assets"):
        return
    op.create_table(
        "source_media_assets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("source_group_id", sa.Integer(), sa.ForeignKey("tg_groups.id"), nullable=True),
        sa.Column("listener_account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=True),
        sa.Column("source_peer_id", sa.String(160), nullable=False, server_default=""),
        sa.Column("source_message_id", sa.String(160), nullable=False, server_default=""),
        sa.Column("source_media_group_id", sa.String(160), nullable=False, server_default=""),
        sa.Column("media_group_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("media_group_total", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("album_caption_policy", sa.String(40), nullable=False, server_default="per_item"),
        sa.Column("media_type", sa.String(40), nullable=False, server_default="photo"),
        sa.Column("caption", sa.Text(), nullable=False, server_default=""),
        sa.Column("media_fingerprint", sa.String(128), nullable=False, server_default=""),
        sa.Column("cache_status", sa.String(40), nullable=False, server_default="pending_cache"),
        sa.Column("cache_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("cache_peer_id", sa.String(160), nullable=False, server_default=""),
        sa.Column("cache_message_id", sa.String(160), nullable=False, server_default=""),
        sa.Column("failure_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_cached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "source_group_id",
            "source_message_id",
            "source_media_group_id",
            "media_group_index",
            name="uq_source_media_assets_dedupe",
        ),
    )
    op.create_index("ix_source_media_assets_status", "source_media_assets", ["tenant_id", "cache_status"])


def downgrade() -> None:
    if _table_exists("source_media_assets"):
        op.drop_index("ix_source_media_assets_status", table_name="source_media_assets")
        op.drop_table("source_media_assets")
