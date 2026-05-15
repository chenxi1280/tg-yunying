"""material media fields

Revision ID: 0034_material_media_fields
Revises: 0033_admin_permissions
Create Date: 2026-05-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0034_material_media_fields"
down_revision = "0033_admin_permissions"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def _column_names(table: str) -> set[str]:
    if not _table_exists(table):
        return set()
    return {item["name"] for item in sa.inspect(_bind()).get_columns(table)}


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    if column.name not in _column_names(table):
        op.add_column(table, column)


def upgrade() -> None:
    if _table_exists("materials"):
        for column in [
            sa.Column("source_kind", sa.String(40), nullable=False, server_default="url"),
            sa.Column("asset_fingerprint", sa.String(128), nullable=False, server_default=""),
            sa.Column("asset_version_id", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("delivery_mode", sa.String(40), nullable=False, server_default="download_reupload"),
            sa.Column("emoji_asset_kind", sa.String(40), nullable=False, server_default=""),
            sa.Column("gateway_type", sa.String(40), nullable=False, server_default="telethon"),
            sa.Column("cache_ready_status", sa.String(40), nullable=False, server_default="not_cached"),
            sa.Column("last_cache_flood_wait_until", sa.DateTime(), nullable=True),
            sa.Column("tg_cache_account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=True),
            sa.Column("tg_cache_peer_id", sa.String(160), nullable=False, server_default=""),
            sa.Column("tg_cache_message_id", sa.String(160), nullable=False, server_default=""),
            sa.Column("tg_ref_version_id", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("file_name", sa.String(255), nullable=False, server_default=""),
            sa.Column("mime_type", sa.String(120), nullable=False, server_default=""),
            sa.Column("file_size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("width", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("height", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("caption", sa.Text(), nullable=False, server_default=""),
            sa.Column("last_cache_error", sa.Text(), nullable=False, server_default=""),
        ]:
            _add_column_if_missing("materials", column)
        op.execute(
            "UPDATE materials SET cache_ready_status = 'ready' "
            "WHERE material_type NOT IN ('图片', '表情包', '文件', '组合消息')"
        )
    if _table_exists("message_tasks"):
        for column in [
            sa.Column("media_sent", sa.Boolean(), nullable=True),
            sa.Column("media_failure_reason", sa.String(80), nullable=False, server_default=""),
            sa.Column("material_asset_fingerprint", sa.String(128), nullable=False, server_default=""),
            sa.Column("material_cache_ready_status", sa.String(40), nullable=False, server_default=""),
        ]:
            _add_column_if_missing("message_tasks", column)


def downgrade() -> None:
    for table, names in {
        "message_tasks": [
            "material_cache_ready_status",
            "material_asset_fingerprint",
            "media_failure_reason",
            "media_sent",
        ],
        "materials": [
            "last_cache_error",
            "caption",
            "height",
            "width",
            "file_size",
            "mime_type",
            "file_name",
            "tg_ref_version_id",
            "tg_cache_message_id",
            "tg_cache_peer_id",
            "tg_cache_account_id",
            "last_cache_flood_wait_until",
            "cache_ready_status",
            "gateway_type",
            "emoji_asset_kind",
            "delivery_mode",
            "asset_version_id",
            "asset_fingerprint",
            "source_kind",
        ],
    }.items():
        if not _table_exists(table):
            continue
        columns = _column_names(table)
        for name in names:
            if name in columns:
                op.drop_column(table, name)
