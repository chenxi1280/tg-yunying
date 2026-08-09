"""add licensed avatar material source metadata

Revision ID: 0144_avatar_material_sources
Revises: 0143_account_profile_name_claims
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0144_avatar_material_sources"
down_revision = "0143_account_profile_name_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "avatar_material_sources" in inspector.get_table_names():
        return
    op.create_table(
        "avatar_material_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("material_id", sa.Integer(), sa.ForeignKey("materials.id"), nullable=False),
        sa.Column("source_page_id", sa.String(length=40), nullable=False),
        sa.Column("source_page_url", sa.String(length=1024), nullable=False),
        sa.Column("source_file_url", sa.String(length=2048), nullable=False),
        sa.Column("license_code", sa.String(length=40), nullable=False),
        sa.Column("license_url", sa.String(length=1024), nullable=False),
        sa.Column("attribution_text", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("perceptual_hash", sa.String(length=16), nullable=False),
        sa.Column("contains_person", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("imported_by", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("imported_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("material_id", name="uq_avatar_material_sources_material"),
        sa.UniqueConstraint("tenant_id", "source_page_id", name="uq_avatar_material_sources_source_page"),
        sa.UniqueConstraint("tenant_id", "content_sha256", name="uq_avatar_material_sources_content"),
    )


def downgrade() -> None:
    op.drop_table("avatar_material_sources")
