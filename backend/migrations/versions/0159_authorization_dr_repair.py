"""Add authorization DR stage facts for guarded artifact recovery.

Revision ID: 0159_dr_repair
Revises: 0158_dr_reconcile
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0159_dr_repair"
down_revision = "0158_dr_reconcile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("tg_authorization_dr_stage_facts"):
        _create_stage_facts()
    if not inspector.has_table("tg_authorization_local_activate_cases"):
        _create_local_activate_cases()


def _create_stage_facts() -> None:
    op.create_table(
        "tg_authorization_dr_stage_facts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("operation_id", sa.String(36), sa.ForeignKey("tg_authorization_dr_operations.id"), nullable=False),
        sa.Column("node_id", sa.String(80), nullable=False),
        sa.Column("owner_epoch", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(48), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("evidence_manifest", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("operation_id", "stage", name="uq_dr_operation_stage"),
    )
    op.create_index("ix_dr_stage_operation", "tg_authorization_dr_stage_facts", ["operation_id", "created_at"])


def _create_local_activate_cases() -> None:
    op.create_table(
        "tg_authorization_local_activate_cases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("target_authorization_id", sa.Integer(), sa.ForeignKey("tg_account_authorizations.id"), nullable=False),
        sa.Column("expected_current_authorization_id", sa.Integer(), nullable=True),
        sa.Column("expected_authorization_generation", sa.Integer(), nullable=False),
        sa.Column("expected_fact_generation", sa.Integer(), nullable=False),
        sa.Column("expected_connection_generation", sa.Integer(), nullable=False),
        sa.Column("expected_target_fact_version", sa.Integer(), nullable=False),
        sa.Column("telegram_user_id_digest", sa.String(64), nullable=False),
        sa.Column("auth_key_fingerprint_digest", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="decision_ready"),
        sa.Column("requested_by", sa.String(100), nullable=False),
        sa.Column("applied_by", sa.String(100), nullable=False, server_default=""),
        sa.Column("approval_ref", sa.String(160), nullable=False, server_default=""),
        sa.Column("apply_idempotency_key", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("fingerprint", name="uq_authorization_local_activate_fingerprint"),
        sa.UniqueConstraint("tenant_id", "apply_idempotency_key", name="uq_local_activate_apply_idempotency"),
    )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("tg_authorization_local_activate_cases"):
        op.drop_table("tg_authorization_local_activate_cases")
    if sa.inspect(op.get_bind()).has_table("tg_authorization_dr_stage_facts"):
        op.drop_table("tg_authorization_dr_stage_facts")
