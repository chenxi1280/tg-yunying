"""ai config and scheduling

Revision ID: 0003_ai_config_scheduling
Revises: 0002_developer_app_pool
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0003_ai_config_scheduling"
down_revision = "0002_developer_app_pool"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _columns(table_name: str) -> set[str]:
    if not _table_exists(table_name):
        return set()
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def upgrade() -> None:
    if not _table_exists("ai_providers"):
        op.create_table(
            "ai_providers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("provider_name", sa.String(length=100), nullable=False),
            sa.Column("provider_type", sa.String(length=40), nullable=False, server_default="openai_compatible"),
            sa.Column("base_url", sa.String(length=300), nullable=False),
            sa.Column("model_name", sa.String(length=120), nullable=False),
            sa.Column("api_key_ciphertext", sa.Text(), nullable=False),
            sa.Column("api_key_header", sa.String(length=80), nullable=False, server_default="Authorization"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("health_status", sa.String(length=30), nullable=False, server_default="健康"),
            sa.Column("last_check_at", sa.DateTime(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
            sa.Column("notes", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
    if not _table_exists("prompt_templates"):
        op.create_table(
            "prompt_templates",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("template_type", sa.String(length=60), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
    if not _table_exists("tenant_ai_settings"):
        op.create_table(
            "tenant_ai_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, unique=True),
            sa.Column("default_provider_id", sa.Integer(), sa.ForeignKey("ai_providers.id"), nullable=True),
            sa.Column("ai_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("fallback_to_mock", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("temperature", sa.Float(), nullable=False, server_default="0.8"),
            sa.Column("max_tokens", sa.Integer(), nullable=False, server_default="1024"),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
    if not _table_exists("scheduling_settings"):
        op.create_table(
            "scheduling_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=True, unique=True),
            sa.Column("jitter_min_seconds", sa.Integer(), nullable=False, server_default="15"),
            sa.Column("jitter_max_seconds", sa.Integer(), nullable=False, server_default="180"),
            sa.Column("batch_interval_seconds", sa.Integer(), nullable=False, server_default="45"),
            sa.Column("respect_send_window", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
    for table, columns in {
        "campaigns": [
            sa.Column("ai_provider_id", sa.Integer(), sa.ForeignKey("ai_providers.id"), nullable=True),
            sa.Column("prompt_template_id", sa.Integer(), sa.ForeignKey("prompt_templates.id"), nullable=True),
            sa.Column("jitter_min_seconds", sa.Integer(), nullable=True),
            sa.Column("jitter_max_seconds", sa.Integer(), nullable=True),
            sa.Column("batch_interval_seconds", sa.Integer(), nullable=True),
            sa.Column("respect_send_window", sa.Boolean(), nullable=True),
            sa.Column("material_ids", sa.Text(), nullable=False, server_default=""),
        ],
        "ai_drafts": [
            sa.Column("provider_name", sa.String(length=100), nullable=False, server_default="Mock"),
            sa.Column("model_name", sa.String(length=120), nullable=False, server_default="mock"),
            sa.Column("prompt_template_name", sa.String(length=120), nullable=False, server_default="默认模板"),
            sa.Column("material_id", sa.Integer(), sa.ForeignKey("materials.id"), nullable=True),
            sa.Column("generation_source", sa.String(length=40), nullable=False, server_default="mock"),
            sa.Column("generation_error", sa.Text(), nullable=False, server_default=""),
        ],
        "message_tasks": [
            sa.Column("message_type", sa.String(length=40), nullable=False, server_default="文本"),
            sa.Column("material_id", sa.Integer(), sa.ForeignKey("materials.id"), nullable=True),
            sa.Column("planned_delay_seconds", sa.Integer(), nullable=False, server_default="0"),
        ],
    }.items():
        for column in columns:
            _add_column_if_missing(table, column)


def downgrade() -> None:
    for table, names in {
        "message_tasks": ["planned_delay_seconds", "material_id", "message_type"],
        "ai_drafts": ["generation_error", "generation_source", "material_id", "prompt_template_name", "model_name", "provider_name"],
        "campaigns": [
            "material_ids",
            "respect_send_window",
            "batch_interval_seconds",
            "jitter_max_seconds",
            "jitter_min_seconds",
            "prompt_template_id",
            "ai_provider_id",
        ],
    }.items():
        for name in names:
            op.drop_column(table, name)
    op.drop_table("scheduling_settings")
    op.drop_table("tenant_ai_settings")
    op.drop_table("prompt_templates")
    op.drop_table("ai_providers")
