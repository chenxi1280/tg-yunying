"""material imports and cache config

Revision ID: 0047_material_cache_imports
Revises: 0046_repair_admin_tables
Create Date: 2026-05-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0047_material_cache_imports"
down_revision = "0046_repair_admin_tables"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def upgrade() -> None:
    if not _table_exists("material_cache_configs"):
        op.create_table(
            "material_cache_configs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("material_cache_input", sa.String(length=300), nullable=False, server_default=""),
            sa.Column("material_cache_peer_id", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("source_media_cache_input", sa.String(length=300), nullable=False, server_default=""),
            sa.Column("source_media_cache_peer_id", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("material_cache_last_error", sa.Text(), nullable=False, server_default=""),
            sa.Column("source_media_cache_last_error", sa.Text(), nullable=False, server_default=""),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("tenant_id", name="uq_material_cache_configs_tenant"),
        )
    if not _table_exists("material_import_jobs"):
        op.create_table(
            "material_import_jobs",
            sa.Column("id", sa.String(length=40), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("source_filename", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("import_type", sa.String(length=40), nullable=False, server_default="zip"),
            sa.Column("target_group_name", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="completed"),
            sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("duplicate_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("oversize_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("item_details", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )


def downgrade() -> None:
    if _table_exists("material_import_jobs"):
        op.drop_table("material_import_jobs")
    if _table_exists("material_cache_configs"):
        op.drop_table("material_cache_configs")
