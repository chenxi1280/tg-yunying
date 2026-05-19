"""account security batches

Revision ID: 0041_account_security_batches
Revises: 0040_unlimited_account_quota
Create Date: 2026-05-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0041_account_security_batches"
down_revision = "0040_unlimited_account_quota"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _tables() -> set[str]:
    return set(sa.inspect(_bind()).get_table_names())


def _has_table(name: str) -> bool:
    return name in _tables()


def upgrade() -> None:
    if not _has_table("tg_account_security_batches"):
        op.create_table(
            "tg_account_security_batches",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("action_types", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="draft"),
            sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_by", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("confirmed_by", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("confirm_text", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("password_strategy", sa.String(length=60), nullable=False, server_default=""),
            sa.Column("password_secret_ref", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("profile_strategy", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("username_strategy", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("avatar_strategy", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("overwrite_existing_profile", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("reason", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("trace_id", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_account_security_batches_tenant_status", "tg_account_security_batches", ["tenant_id", "status"])

    if not _has_table("tg_account_security_snapshots"):
        op.create_table(
            "tg_account_security_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False, unique=True),
            sa.Column("trusted_session_status", sa.String(length=40), nullable=False, server_default="unknown"),
            sa.Column("two_fa_status", sa.String(length=40), nullable=False, server_default="unknown"),
            sa.Column("two_fa_password_ciphertext", sa.Text(), nullable=False, server_default=""),
            sa.Column("two_fa_password_hint", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("two_fa_password_stored_at", sa.DateTime(), nullable=True),
            sa.Column("external_authorization_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_device_scan_at", sa.DateTime(), nullable=True),
            sa.Column("last_2fa_check_at", sa.DateTime(), nullable=True),
            sa.Column("profile_status", sa.String(length=40), nullable=False, server_default="unknown"),
            sa.Column("profile_last_updated_at", sa.DateTime(), nullable=True),
            sa.Column("trusted_device_label", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("last_hardened_at", sa.DateTime(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
            sa.Column("trace_id", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_account_security_snapshots_tenant", "tg_account_security_snapshots", ["tenant_id"])

    if not _has_table("tg_account_authorization_snapshots"):
        op.create_table(
            "tg_account_authorization_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
            sa.Column("batch_id", sa.Integer(), sa.ForeignKey("tg_account_security_batches.id"), nullable=True),
            sa.Column("authorization_hash_ciphertext", sa.Text(), nullable=False, server_default=""),
            sa.Column("is_platform_trusted", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("is_current_session", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("device_model", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("platform", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("system_version", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("api_id", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("app_name", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("app_version", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("ip_masked", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("country", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("region", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("date_created", sa.DateTime(), nullable=True),
            sa.Column("date_active", sa.DateTime(), nullable=True),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="active"),
            sa.Column("scanned_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_account_authorization_snapshots_account", "tg_account_authorization_snapshots", ["tenant_id", "account_id"])

    if not _has_table("tg_account_security_batch_items"):
        op.create_table(
            "tg_account_security_batch_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("batch_id", sa.Integer(), sa.ForeignKey("tg_account_security_batches.id"), nullable=False),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="pending"),
            sa.Column("precheck_status", sa.String(length=40), nullable=False, server_default="pending"),
            sa.Column("cleanup_status", sa.String(length=40), nullable=False, server_default="not_requested"),
            sa.Column("two_fa_status", sa.String(length=40), nullable=False, server_default="not_requested"),
            sa.Column("profile_status", sa.String(length=40), nullable=False, server_default="not_requested"),
            sa.Column("username_status", sa.String(length=40), nullable=False, server_default="not_requested"),
            sa.Column("avatar_status", sa.String(length=40), nullable=False, server_default="not_requested"),
            sa.Column("external_devices_before", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("external_devices_after", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("generated_display_name", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("generated_first_name", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("generated_last_name", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("generated_bio", sa.Text(), nullable=False, server_default=""),
            sa.Column("generated_username", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("username_candidates", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("avatar_source", sa.String(length=300), nullable=False, server_default=""),
            sa.Column("skipped_reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("failure_type", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("failure_detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("next_retry_at", sa.DateTime(), nullable=True),
            sa.Column("trace_id", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_account_security_batch_items_batch_status", "tg_account_security_batch_items", ["batch_id", "status"])
        op.create_index("ix_account_security_batch_items_account", "tg_account_security_batch_items", ["tenant_id", "account_id"])

    if not _has_table("tg_account_profile_batch_rules"):
        op.create_table(
            "tg_account_profile_batch_rules",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("batch_id", sa.Integer(), sa.ForeignKey("tg_account_security_batches.id"), nullable=False),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("generation_mode", sa.String(length=40), nullable=False, server_default="ai_random"),
            sa.Column("ai_provider_id", sa.Integer(), nullable=True),
            sa.Column("ai_prompt_version", sa.String(length=40), nullable=False, server_default="account_profile_v1"),
            sa.Column("language_style", sa.String(length=40), nullable=False, server_default="中文"),
            sa.Column("persona_style", sa.String(length=80), nullable=False, server_default="自然用户"),
            sa.Column("gender_bias", sa.String(length=40), nullable=False, server_default="不限"),
            sa.Column("age_style", sa.String(length=40), nullable=False, server_default="不限"),
            sa.Column("forbidden_words", sa.Text(), nullable=False, server_default=""),
            sa.Column("uniqueness_seed", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("name_base", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("name_start_index", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("name_padding", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("username_prefix", sa.String(length=60), nullable=False, server_default=""),
            sa.Column("username_start_index", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("username_padding", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("username_max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("bio_template", sa.Text(), nullable=False, server_default=""),
            sa.Column("avatar_assignment_mode", sa.String(length=40), nullable=False, server_default="none"),
            sa.Column("overwrite_existing", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )


def downgrade() -> None:
    for name in (
        "tg_account_profile_batch_rules",
        "tg_account_security_batch_items",
        "tg_account_authorization_snapshots",
        "tg_account_security_snapshots",
        "tg_account_security_batches",
    ):
        if _has_table(name):
            op.drop_table(name)
