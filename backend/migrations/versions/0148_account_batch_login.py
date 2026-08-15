"""Add durable account batch login contracts.

Revision ID: 0148_account_batch_login
Revises: 0147_login_challenge_binding
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0148_account_batch_login"
down_revision = "0147_login_challenge_binding"
branch_labels = None
depends_on = None


def _timestamps() -> tuple[sa.Column, ...]:
    return (
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def _create_batches() -> None:
    op.create_table(
        "tg_account_login_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("pool_id", sa.Integer(), sa.ForeignKey("account_pools.id"), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("recipient_user_id", sa.Integer(), sa.ForeignKey("app_users.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(80), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(40), nullable=False, server_default="queued"),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("execution_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("resolution_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unresolved_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("warning_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_claimed_at", sa.DateTime(), nullable=True),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column("trace_id", sa.String(80), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "recipient_user_id", "idempotency_key", name="uq_login_batch_idempotency"),
    )
    op.create_index(
        "ix_login_batch_fair_claim", "tg_account_login_batches",
        ["status", "last_claimed_at", "created_at"],
    )


def _create_items() -> None:
    op.create_table(
        "tg_account_login_batch_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.Integer(), sa.ForeignKey("tg_account_login_batches.id"), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("phone_masked", sa.String(60), nullable=False),
        sa.Column("phone_fingerprint", sa.String(64), nullable=False),
        sa.Column("phone_fingerprint_version", sa.Integer(), nullable=False),
        sa.Column("phone_ciphertext", sa.Text(), nullable=False),
        sa.Column("code_url_ciphertext", sa.Text(), nullable=True),
        sa.Column("credential_expires_at", sa.DateTime(), nullable=True),
        sa.Column("code_source_host", sa.String(120), nullable=False),
        sa.Column("code_source_uuid_fingerprint", sa.String(64), nullable=False),
        sa.Column("code_source_uuid_hint", sa.String(40), nullable=False),
        sa.Column("replace_binding", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expected_binding_version", sa.Integer(), nullable=True),
        sa.Column("route_hint", sa.String(40), nullable=False),
        sa.Column("route", sa.String(40), nullable=False, server_default=""),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=True),
        sa.Column("status", sa.String(40), nullable=False, server_default="pending"),
        sa.Column("phase", sa.String(40), nullable=False, server_default="prepare"),
        sa.Column("failure_type", sa.String(80), nullable=False, server_default=""),
        sa.Column("failure_detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("warning_detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("current_attempt_id", sa.Integer(), nullable=True),
        sa.Column("execution_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("batch_id", "line_no", name="uq_login_batch_item_line"),
        sa.UniqueConstraint("batch_id", "phone_fingerprint", name="uq_login_batch_item_phone"),
        sa.UniqueConstraint("batch_id", "code_source_uuid_fingerprint", name="uq_login_batch_item_uuid"),
    )
    op.create_index(
        "ix_login_batch_item_due", "tg_account_login_batch_items",
        ["status", "next_retry_at", "batch_id", "line_no"],
    )


def _create_attempts() -> None:
    op.create_table(
        "tg_account_login_batch_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("tg_account_login_batch_items.id"), nullable=False),
        sa.Column("batch_id", sa.Integer(), sa.ForeignKey("tg_account_login_batches.id"), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("execution_generation", sa.Integer(), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("phase", sa.String(40), nullable=False, server_default="prepare"),
        sa.Column("lease_token", sa.String(80), nullable=False, server_default=""),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
        sa.Column("code_wait_until_at", sa.DateTime(), nullable=True),
        sa.Column("flow_id", sa.Integer(), sa.ForeignKey("tg_login_flows.id"), nullable=True),
        sa.Column("flow_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("baseline_code_hmac", sa.String(64), nullable=False, server_default=""),
        sa.Column("baseline_login_time_hmac", sa.String(64), nullable=False, server_default=""),
        sa.Column("send_request_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("send_request_key", sa.String(80), nullable=False, server_default=""),
        sa.Column("send_call_state", sa.String(20), nullable=False, server_default="none"),
        sa.Column("code_verify_request_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("code_verify_request_key", sa.String(80), nullable=False, server_default=""),
        sa.Column("code_verify_call_state", sa.String(20), nullable=False, server_default="none"),
        sa.Column("twofa_verify_request_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("twofa_verify_request_key", sa.String(80), nullable=False, server_default=""),
        sa.Column("twofa_verify_call_state", sa.String(20), nullable=False, server_default="none"),
        sa.Column("reconcile_status", sa.String(40), nullable=False, server_default="none"),
        sa.Column("reconcile_until_at", sa.DateTime(), nullable=True),
        sa.Column("last_reconciled_at", sa.DateTime(), nullable=True),
        sa.Column("authoritative_evidence_ref", sa.String(160), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("item_id", "execution_generation", name="uq_login_attempt_generation"),
    )
    op.create_index(
        "ix_login_attempt_reconcile", "tg_account_login_batch_attempts",
        ["reconcile_status", "reconcile_until_at"],
    )


def _create_notifications() -> None:
    op.create_table(
        "tg_account_login_batch_notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.Integer(), sa.ForeignKey("tg_account_login_batches.id"), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("execution_generation", sa.Integer(), nullable=False),
        sa.Column("resolution_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(20), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("recipient_user_id", sa.Integer(), sa.ForeignKey("app_users.id"), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=False),
        sa.Column("delivery_status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("delivery_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        *_timestamps(),
        sa.UniqueConstraint(
            "batch_id", "execution_generation", "resolution_version", "channel", "recipient_user_id",
            name="uq_login_batch_notification_delivery",
        ),
    )
    op.create_index(
        "ix_login_batch_notification_outbox", "tg_account_login_batch_notifications",
        ["channel", "delivery_status", "next_retry_at"],
    )


def _create_aliases_and_buckets() -> None:
    op.create_table(
        "tg_account_phone_fingerprint_aliases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "key_version", "fingerprint", name="uq_account_phone_alias_fingerprint"),
    )
    op.create_index(
        "ix_account_phone_alias_account", "tg_account_phone_fingerprint_aliases", ["tenant_id", "account_id"]
    )
    op.create_table(
        "tg_account_login_rate_buckets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scope_type", sa.String(30), nullable=False),
        sa.Column("scope_id", sa.String(120), nullable=False),
        sa.Column("next_available_at", sa.DateTime(), nullable=True),
        sa.Column("active_leases", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_concurrency", sa.Integer(), nullable=False),
        sa.Column("lease_tokens_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("scope_type", "scope_id", name="uq_account_login_rate_bucket_scope"),
    )


def _add_account_binding() -> None:
    columns = (
        sa.Column("code_source_host", sa.String(120), nullable=False, server_default=""),
        sa.Column("code_source_uuid_ciphertext", sa.Text(), nullable=True),
        sa.Column("code_source_uuid_fingerprint", sa.String(64), nullable=False, server_default=""),
        sa.Column("code_source_uuid_hint", sa.String(40), nullable=False, server_default=""),
        sa.Column("code_source_binding_status", sa.String(40), nullable=False, server_default="unbound"),
        sa.Column("code_source_binding_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("code_source_bound_at", sa.DateTime(), nullable=True),
        sa.Column("code_source_bound_by", sa.String(100), nullable=False, server_default=""),
    )
    for column in columns:
        op.add_column("tg_accounts", column)
    op.create_index(
        "ux_tg_accounts_tenant_code_source_active",
        "tg_accounts",
        ["tenant_id", "code_source_host", "code_source_uuid_fingerprint"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND code_source_uuid_fingerprint <> ''"),
    )
    op.add_column("tg_login_flows", sa.Column("batch_login_attempt_id", sa.Integer(), nullable=True))
    op.add_column(
        "tg_login_flows",
        sa.Column("batch_login_generation", sa.Integer(), nullable=False, server_default="0"),
    )


def upgrade() -> None:
    _create_batches()
    _create_items()
    _create_attempts()
    _create_notifications()
    _create_aliases_and_buckets()
    _add_account_binding()
    op.create_foreign_key(
        "fk_login_batch_item_current_attempt",
        "tg_account_login_batch_items", "tg_account_login_batch_attempts",
        ["current_attempt_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_login_flow_batch_attempt",
        "tg_login_flows", "tg_account_login_batch_attempts",
        ["batch_login_attempt_id"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_login_flow_batch_attempt", "tg_login_flows", type_="foreignkey")
    op.drop_constraint("fk_login_batch_item_current_attempt", "tg_account_login_batch_items", type_="foreignkey")
    op.drop_column("tg_login_flows", "batch_login_generation")
    op.drop_column("tg_login_flows", "batch_login_attempt_id")
    op.drop_index("ux_tg_accounts_tenant_code_source_active", table_name="tg_accounts")
    for name in (
        "code_source_bound_by", "code_source_bound_at", "code_source_binding_version",
        "code_source_binding_status", "code_source_uuid_hint", "code_source_uuid_fingerprint",
        "code_source_uuid_ciphertext", "code_source_host",
    ):
        op.drop_column("tg_accounts", name)
    op.drop_table("tg_account_login_rate_buckets")
    op.drop_index("ix_account_phone_alias_account", table_name="tg_account_phone_fingerprint_aliases")
    op.drop_table("tg_account_phone_fingerprint_aliases")
    op.drop_index("ix_login_batch_notification_outbox", table_name="tg_account_login_batch_notifications")
    op.drop_table("tg_account_login_batch_notifications")
    op.drop_index("ix_login_attempt_reconcile", table_name="tg_account_login_batch_attempts")
    op.drop_table("tg_account_login_batch_attempts")
    op.drop_index("ix_login_batch_item_due", table_name="tg_account_login_batch_items")
    op.drop_table("tg_account_login_batch_items")
    op.drop_index("ix_login_batch_fair_claim", table_name="tg_account_login_batches")
    op.drop_table("tg_account_login_batches")
