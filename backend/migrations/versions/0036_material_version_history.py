"""material version history

Revision ID: 0036_material_version_history
Revises: 0035_source_media_assets
Create Date: 2026-05-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0036_material_version_history"
down_revision = "0035_source_media_assets"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def upgrade() -> None:
    if not _table_exists("material_asset_versions"):
        op.create_table(
            "material_asset_versions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("material_id", sa.Integer(), sa.ForeignKey("materials.id"), nullable=False),
            sa.Column("asset_version_id", sa.Integer(), nullable=False),
            sa.Column("source_kind", sa.String(40), nullable=False, server_default=""),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("asset_fingerprint", sa.String(128), nullable=False, server_default=""),
            sa.Column("file_name", sa.String(255), nullable=False, server_default=""),
            sa.Column("mime_type", sa.String(120), nullable=False, server_default=""),
            sa.Column("file_size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("width", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("height", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("caption", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_by", sa.String(100), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("material_id", "asset_version_id", name="uq_material_asset_versions_version"),
        )
        op.create_index("ix_material_asset_versions_material", "material_asset_versions", ["tenant_id", "material_id"])
    if not _table_exists("material_tg_ref_versions"):
        op.create_table(
            "material_tg_ref_versions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("material_id", sa.Integer(), sa.ForeignKey("materials.id"), nullable=False),
            sa.Column("asset_version_id", sa.Integer(), nullable=False),
            sa.Column("tg_ref_version_id", sa.Integer(), nullable=False),
            sa.Column("cache_status", sa.String(40), nullable=False, server_default=""),
            sa.Column("tg_cache_account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=True),
            sa.Column("tg_cache_peer_id", sa.String(160), nullable=False, server_default=""),
            sa.Column("tg_cache_message_id", sa.String(160), nullable=False, server_default=""),
            sa.Column("gateway_type", sa.String(40), nullable=False, server_default=""),
            sa.Column("failure_reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_by", sa.String(100), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("material_id", "tg_ref_version_id", name="uq_material_tg_ref_versions_version"),
        )
        op.create_index("ix_material_tg_ref_versions_material", "material_tg_ref_versions", ["tenant_id", "material_id"])


def downgrade() -> None:
    if _table_exists("material_tg_ref_versions"):
        op.drop_index("ix_material_tg_ref_versions_material", table_name="material_tg_ref_versions")
        op.drop_table("material_tg_ref_versions")
    if _table_exists("material_asset_versions"):
        op.drop_index("ix_material_asset_versions_material", table_name="material_asset_versions")
        op.drop_table("material_asset_versions")
