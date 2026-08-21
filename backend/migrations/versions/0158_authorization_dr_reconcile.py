"""Add guarded authorization DR unknown reconciliation state.

Revision ID: 0158_dr_reconcile
Revises: 0157_authorization_dr_core
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0158_dr_reconcile"
down_revision = "0157_authorization_dr_core"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _columns(name: str) -> set[str]:
    if not _has_table(name):
        return set()
    return {str(item["name"]) for item in sa.inspect(op.get_bind()).get_columns(name)}


def _add_column(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def _create_reconcile_cases() -> None:
    if _has_table("tg_authorization_dr_reconcile_cases"):
        return
    op.create_table(
        "tg_authorization_dr_reconcile_cases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("operation_id", sa.String(36), sa.ForeignKey("tg_authorization_dr_operations.id"), nullable=False),
        sa.Column("reconcile_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(40), nullable=False, server_default="open"),
        sa.Column("classification", sa.String(48), nullable=False),
        sa.Column("recommended_transition", sa.String(48), nullable=False),
        sa.Column("blocker_code", sa.String(100), nullable=False),
        sa.Column("expected_operation_version", sa.Integer(), nullable=False),
        sa.Column("expected_item_version", sa.Integer(), nullable=False),
        sa.Column("expected_source_fact_version", sa.Integer(), nullable=False),
        sa.Column("expected_owner_epoch", sa.Integer(), nullable=False),
        sa.Column("expected_node_id", sa.String(80), nullable=False),
        sa.Column("expected_runtime_image_sha", sa.String(64), nullable=False),
        sa.Column("evidence_fingerprint", sa.String(64), nullable=False),
        sa.Column("evidence_manifest", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("persisted_artifact_state", sa.String(32), nullable=False, server_default="none"),
        sa.Column("requested_by", sa.String(100), nullable=False),
        sa.Column("applied_by", sa.String(100), nullable=False, server_default=""),
        sa.Column("approval_ref", sa.String(160), nullable=False, server_default=""),
        sa.Column("apply_idempotency_key", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("operation_id", name="uq_dr_reconcile_operation"),
        sa.UniqueConstraint("tenant_id", "apply_idempotency_key", name="uq_dr_reconcile_apply_idempotency"),
    )


def upgrade() -> None:
    _add_column("authorization_dr_execution_nodes", sa.Column("runtime_image_sha", sa.String(64), nullable=False, server_default=""))
    _add_column("tg_authorization_dr_batches", sa.Column("execution_finished_at", sa.DateTime(), nullable=True))
    _add_column("tg_authorization_dr_operations", sa.Column("reconcile_case_id", sa.String(36), nullable=True))
    _add_column("tg_authorization_dr_operations", sa.Column("reconcile_status", sa.String(32), nullable=False, server_default="none"))
    _add_column("tg_authorization_dr_operations", sa.Column("reconciled_at", sa.DateTime(), nullable=True))
    _create_reconcile_cases()


def downgrade() -> None:
    if _has_table("tg_authorization_dr_reconcile_cases"):
        op.drop_table("tg_authorization_dr_reconcile_cases")
    for name in ("reconciled_at", "reconcile_status", "reconcile_case_id"):
        if name in _columns("tg_authorization_dr_operations"):
            op.drop_column("tg_authorization_dr_operations", name)
    if "execution_finished_at" in _columns("tg_authorization_dr_batches"):
        op.drop_column("tg_authorization_dr_batches", "execution_finished_at")
    if "runtime_image_sha" in _columns("authorization_dr_execution_nodes"):
        op.drop_column("authorization_dr_execution_nodes", "runtime_image_sha")
