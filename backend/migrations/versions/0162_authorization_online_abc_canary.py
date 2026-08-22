"""Add guarded complete-online-ABC ten-account canary manifests.

Revision ID: 0162_online_abc_canary
Revises: 0161_provider_failover
"""

from alembic import op
import sqlalchemy as sa


revision = "0162_online_abc_canary"
down_revision = "0161_provider_failover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tg_authorization_online_abc_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("target_set_fingerprint", sa.String(64), nullable=False),
        sa.Column("target_count", sa.Integer(), nullable=False),
        sa.Column("deployed_release_sha", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="previewed"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("requested_by", sa.String(100), nullable=False),
        sa.Column("approved_by", sa.String(100), nullable=False, server_default=""),
        sa.Column("approval_ref", sa.String(160), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("observation_started_at", sa.DateTime(), nullable=True),
        sa.Column("observation_closes_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_online_abc_batch_idempotency"),
    )
    op.create_table(
        "tg_authorization_online_abc_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("tg_authorization_online_abc_batches.id"), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("primary_authorization_id", sa.Integer(), sa.ForeignKey("tg_account_authorizations.id"), nullable=False),
        sa.Column("primary_fact_version", sa.Integer(), nullable=False),
        sa.Column("authorization_generation", sa.Integer(), nullable=False),
        sa.Column("authorization_fact_generation", sa.Integer(), nullable=False),
        sa.Column("connection_generation", sa.Integer(), nullable=False),
        sa.Column("primary_session_digest", sa.String(64), nullable=False),
        sa.Column("app_b_id", sa.Integer(), sa.ForeignKey("telegram_developer_apps.id"), nullable=False),
        sa.Column("app_b_credentials_version", sa.Integer(), nullable=False),
        sa.Column("app_b_assignment_purpose", sa.String(32), nullable=False),
        sa.Column("app_b_assignment_version", sa.Integer(), nullable=False),
        sa.Column("proxy_id", sa.Integer(), sa.ForeignKey("account_proxies.id"), nullable=False),
        sa.Column("source_c_authorization_id", sa.Integer(), sa.ForeignKey("tg_account_authorizations.id"), nullable=False),
        sa.Column("source_c_fact_version", sa.Integer(), nullable=False),
        sa.Column("source_c_slot_generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("outcome", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("primary_probe_outcome", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("blocker_code", sa.String(100), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("batch_id", "account_id", name="uq_online_abc_batch_account"),
        sa.UniqueConstraint("batch_id", "ordinal", name="uq_online_abc_batch_ordinal"),
    )
    op.create_table(
        "tg_authorization_online_abc_slot_results",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("tg_authorization_online_abc_batches.id"), nullable=False),
        sa.Column("item_id", sa.String(36), sa.ForeignKey("tg_authorization_online_abc_items.id"), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("logical_slot", sa.String(24), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("operation_id", sa.String(36), sa.ForeignKey("tg_authorization_dr_operations.id"), nullable=True),
        sa.Column("blocker_code", sa.String(100), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("item_id", "logical_slot", name="uq_online_abc_item_slot"),
    )


def downgrade() -> None:
    op.drop_table("tg_authorization_online_abc_slot_results")
    op.drop_table("tg_authorization_online_abc_items")
    op.drop_table("tg_authorization_online_abc_batches")
