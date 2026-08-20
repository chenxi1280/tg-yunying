"""Add Malaysia standby authorization DR contract and durable migration state.

Revision ID: 0157_authorization_dr_core
Revises: 0156_ai_content_runtime
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0157_authorization_dr_core"
down_revision = "0156_ai_content_runtime"
branch_labels = None
depends_on = None
REQUIRED_TABLES = frozenset({
    "authorization_dr_runtime_contracts",
    "authorization_dr_execution_nodes",
    "telegram_egress_assignments",
    "developer_app_slot_assignments",
    "tg_authorization_dr_batches",
    "tg_authorization_dr_batch_items",
    "tg_authorization_dr_operations",
    "tg_authorization_wake_bundles",
    "tg_authorization_wake_bundle_copies",
    "tg_authorization_restore_probe_facts",
    "tg_authorization_wake_inventory_entries",
    "tg_authorization_slot_decisions",
    "tg_account_device_cleanup_targets",
})
REQUIRED_COLUMNS = {
    "tg_account_authorizations": frozenset({
        "logical_slot", "slot_generation", "is_slot_current", "provision_region_code",
        "credential_storage_scope", "dr_state", "remote_authorization_state",
        "protected_from_cleanup", "wake_bundle_id", "telegram_login_at",
        "migration_recovery_gate_status", "rollback_window_closed_at",
        "auth_key_fingerprint_digest", "telegram_user_id_digest",
    }),
    "tg_accounts": frozenset({
        "current_authorization_id", "authorization_generation", "authorization_fact_generation",
        "connection_generation", "authorization_contract_version", "business_runtime_status",
        "sv_redundancy_status", "authorization_recovery_status", "account_lifecycle_status",
    }),
    "tg_account_security_batches": frozenset({
        "requested_count", "eligible_count", "skipped_reason_counts", "idempotency_key",
    }),
    "tg_account_security_batch_items": frozenset({
        "executor_authorization_id", "executor_fact_version", "executor_telegram_login_at",
        "protected_manifest_digest", "target_set_digest", "remote_effect_started_at",
        "final_readback_digest",
    }),
}


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _columns(name: str) -> set[str]:
    if not _has_table(name):
        return set()
    return {str(item["name"]) for item in sa.inspect(op.get_bind()).get_columns(name)}


def _schema_complete() -> bool:
    if any(not _has_table(table) for table in REQUIRED_TABLES):
        return False
    return all(columns <= _columns(table) for table, columns in REQUIRED_COLUMNS.items())


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def _create_runtime_tables() -> None:
    op.create_table(
        "authorization_dr_runtime_contracts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mode", sa.String(24), nullable=False, server_default="off"),
        sa.Column("contract_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("cluster_incarnation", sa.String(80), nullable=False, server_default=""),
        sa.Column("mutation_hold_reason", sa.String(80), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by", sa.String(100), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "telegram_egress_assignments",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("purpose", sa.String(40), nullable=False, unique=True),
        sa.Column("region_code", sa.String(24), nullable=False),
        sa.Column("secret_ref_digest", sa.String(64), nullable=False, server_default=""),
        sa.Column("observed_ip_hmac", sa.String(64), nullable=False, server_default=""),
        sa.Column("status", sa.String(24), nullable=False, server_default="unproven"),
        sa.Column("connectivity_status", sa.String(24), nullable=False, server_default="unproven"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "authorization_dr_execution_nodes",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("region_code", sa.String(24), nullable=False),
        sa.Column("purpose", sa.String(40), nullable=False),
        sa.Column("capability_version", sa.String(80), nullable=False),
        sa.Column("standby_egress_id", sa.String(80), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="offline"),
        sa.Column("active_client_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_table(
        "developer_app_slot_assignments",
        sa.Column("slot_purpose", sa.String(32), primary_key=True),
        sa.Column("developer_app_id", sa.Integer(), sa.ForeignKey("telegram_developer_apps.id"), nullable=False),
        sa.Column("assignment_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("credentials_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("assigned_by", sa.String(100), nullable=False),
        sa.Column("assigned_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("developer_app_id", name="uq_dr_slot_assignment_app"),
    )


def _create_batch_tables() -> None:
    op.create_table(
        "tg_authorization_dr_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("operation_type", sa.String(40), nullable=False, server_default="migrate_standby_2"),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("target_set_fingerprint", sa.String(64), nullable=False),
        sa.Column("target_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="previewed"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("requested_by", sa.String(100), nullable=False),
        sa.Column("approval_ref", sa.String(160), nullable=False, server_default=""),
        sa.Column("approved_by", sa.String(100), nullable=False, server_default=""),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("last_claimed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_dr_batch_idempotency"),
    )
    op.create_index("ix_dr_batch_claim", "tg_authorization_dr_batches", ["status", "last_claimed_at", "created_at"])
    op.create_table(
        "tg_authorization_dr_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("batch_item_id", sa.String(36), nullable=True),
        sa.Column("operation_type", sa.String(48), nullable=False),
        sa.Column("logical_slot", sa.String(24), nullable=False),
        sa.Column("source_authorization_id", sa.Integer(), sa.ForeignKey("tg_account_authorizations.id"), nullable=True),
        sa.Column("candidate_authorization_id", sa.Integer(), sa.ForeignKey("tg_account_authorizations.id"), nullable=True),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("target_generation", sa.Integer(), nullable=False),
        sa.Column("developer_app_id", sa.Integer(), sa.ForeignKey("telegram_developer_apps.id"), nullable=False),
        sa.Column("developer_app_api_id_snapshot", sa.Integer(), nullable=False),
        sa.Column("developer_app_credentials_version", sa.Integer(), nullable=False),
        sa.Column("assignment_version", sa.Integer(), nullable=False),
        sa.Column("egress_id", sa.String(80), nullable=False),
        sa.Column("egress_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(40), nullable=False, server_default="approved"),
        sa.Column("blocker_code", sa.String(100), nullable=False, server_default=""),
        sa.Column("operation_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("execution_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("owner_node_id", sa.String(80), nullable=False, server_default=""),
        sa.Column("owner_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_token", sa.String(80), nullable=False, server_default=""),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("remote_effect_started_at", sa.DateTime(), nullable=True),
        sa.Column("remote_call_state", sa.String(20), nullable=False, server_default="none"),
        sa.Column("requested_by", sa.String(100), nullable=False),
        sa.Column("approved_by", sa.String(100), nullable=False),
        sa.Column("approval_ref", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_dr_operation_idempotency"),
    )
    op.create_index("ix_dr_operation_claim", "tg_authorization_dr_operations", ["status", "lease_expires_at", "created_at"])
    op.create_table(
        "tg_authorization_dr_batch_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("tg_authorization_dr_batches.id"), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("expected_source_authorization_id", sa.Integer(), sa.ForeignKey("tg_account_authorizations.id"), nullable=False),
        sa.Column("expected_source_fact_version", sa.Integer(), nullable=False),
        sa.Column("expected_source_generation", sa.Integer(), nullable=False),
        sa.Column("target_generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("outcome", sa.String(48), nullable=False, server_default="pending"),
        sa.Column("blocker_code", sa.String(100), nullable=False, server_default=""),
        sa.Column("operation_id", sa.String(36), sa.ForeignKey("tg_authorization_dr_operations.id"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("batch_id", "account_id", name="uq_dr_batch_account"),
    )
    op.create_foreign_key(
        "fk_dr_operation_batch_item", "tg_authorization_dr_operations", "tg_authorization_dr_batch_items",
        ["batch_item_id"], ["id"],
    )
    op.create_index("ix_dr_batch_item_claim", "tg_authorization_dr_batch_items", ["status", "batch_id", "ordinal"])


def _create_bundle_tables() -> None:
    op.create_table(
        "tg_authorization_wake_bundles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("authorization_id", sa.Integer(), sa.ForeignKey("tg_account_authorizations.id"), nullable=False),
        sa.Column("operation_id", sa.String(36), sa.ForeignKey("tg_authorization_dr_operations.id"), nullable=False),
        sa.Column("bundle_generation", sa.Integer(), nullable=False),
        sa.Column("ciphertext_digest", sa.String(64), nullable=False),
        sa.Column("wrapped_dek_ciphertext", sa.Text(), nullable=False),
        sa.Column("kms_key_ref_digest", sa.String(64), nullable=False),
        sa.Column("kms_key_version", sa.String(80), nullable=False),
        sa.Column("kms_decrypt_status", sa.String(24), nullable=False, server_default="unproven"),
        sa.Column("auth_key_fingerprint_digest", sa.String(64), nullable=False),
        sa.Column("telegram_user_id_digest", sa.String(64), nullable=False),
        sa.Column("recoverable_copy_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("receipt_status", sa.String(32), nullable=False, server_default="prepared"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("protected_from_cleanup", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("authorization_id", "bundle_generation", name="uq_dr_bundle_generation"),
    )
    op.create_index("ix_dr_bundle_active", "tg_authorization_wake_bundles", ["authorization_id", "is_active"])
    op.create_table(
        "tg_authorization_wake_bundle_copies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("bundle_id", sa.String(36), sa.ForeignKey("tg_authorization_wake_bundles.id"), nullable=False),
        sa.Column("copy_kind", sa.String(24), nullable=False),
        sa.Column("object_ref_digest", sa.String(64), nullable=False),
        sa.Column("ciphertext_digest", sa.String(64), nullable=False),
        sa.Column("immutable_version", sa.String(120), nullable=False),
        sa.Column("write_receipt_digest", sa.String(64), nullable=False),
        sa.Column("readback_receipt_digest", sa.String(64), nullable=False),
        sa.Column("write_verified_at", sa.DateTime(), nullable=False),
        sa.Column("readback_verified_at", sa.DateTime(), nullable=False),
        sa.Column("decrypt_verified_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("bundle_id", "copy_kind", name="uq_dr_bundle_copy_kind"),
    )
    op.create_table(
        "tg_authorization_restore_probe_facts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("bundle_id", sa.String(36), sa.ForeignKey("tg_authorization_wake_bundles.id"), nullable=False),
        sa.Column("operation_id", sa.String(36), sa.ForeignKey("tg_authorization_dr_operations.id"), nullable=False),
        sa.Column("probe_generation", sa.Integer(), nullable=False),
        sa.Column("source_copy_kind", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("session_parse_status", sa.String(24), nullable=False),
        sa.Column("authorization_status", sa.String(24), nullable=False),
        sa.Column("identity_match_status", sa.String(24), nullable=False),
        sa.Column("auth_key_match_status", sa.String(24), nullable=False),
        sa.Column("source_client_disconnected", sa.Boolean(), nullable=False),
        sa.Column("probe_client_disconnected", sa.Boolean(), nullable=False),
        sa.Column("zeroize_receipt_digest", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("bundle_id", "probe_generation", name="uq_dr_restore_probe_generation"),
    )
    op.create_table(
        "tg_authorization_wake_inventory_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("node_id", sa.String(80), nullable=False),
        sa.Column("inventory_sequence", sa.BigInteger(), nullable=False),
        sa.Column("operation_id", sa.String(36), sa.ForeignKey("tg_authorization_dr_operations.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("authorization_id", sa.Integer(), sa.ForeignKey("tg_account_authorizations.id"), nullable=False),
        sa.Column("bundle_id", sa.String(36), sa.ForeignKey("tg_authorization_wake_bundles.id"), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("decision_payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("observed_by_central_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("node_id", "inventory_sequence", name="uq_dr_inventory_sequence"),
    )
    op.create_table(
        "tg_authorization_slot_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("logical_slot", sa.String(24), nullable=False),
        sa.Column("decision_generation", sa.Integer(), nullable=False),
        sa.Column("expected_old_authorization_id", sa.Integer(), sa.ForeignKey("tg_account_authorizations.id"), nullable=False),
        sa.Column("new_authorization_id", sa.Integer(), sa.ForeignKey("tg_account_authorizations.id"), nullable=False),
        sa.Column("expected_old_slot_generation", sa.Integer(), nullable=False),
        sa.Column("new_slot_generation", sa.Integer(), nullable=False),
        sa.Column("expected_account_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="prepared"),
        sa.Column("recovery_gate_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("inventory_sequence", sa.BigInteger(), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("prepared_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("observed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("account_id", "logical_slot", "decision_generation", name="uq_dr_slot_decision_generation"),
    )


def _add_authorization_columns() -> None:
    columns = (
        sa.Column("logical_slot", sa.String(24), nullable=False, server_default="primary"),
        sa.Column("slot_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_slot_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("provision_region_code", sa.String(24), nullable=False, server_default="sv"),
        sa.Column("credential_storage_scope", sa.String(32), nullable=False, server_default="central_business"),
        sa.Column("dr_state", sa.String(40), nullable=False, server_default="not_configured"),
        sa.Column("remote_authorization_state", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("protected_from_cleanup", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("wake_bundle_id", sa.String(36), nullable=True),
        sa.Column("telegram_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("migration_recovery_gate_status", sa.String(40), nullable=False, server_default="not_required"),
        sa.Column("rollback_window_closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auth_key_fingerprint_digest", sa.String(64), nullable=False, server_default=""),
        sa.Column("telegram_user_id_digest", sa.String(64), nullable=False, server_default=""),
    )
    for column in columns:
        op.add_column("tg_account_authorizations", column)
    op.execute("UPDATE tg_account_authorizations SET logical_slot = role, is_slot_current = false")
    op.execute(
        "UPDATE tg_account_authorizations SET is_slot_current = true "
        "WHERE id IN (SELECT max(id) FROM tg_account_authorizations GROUP BY account_id, role)"
    )
    op.create_foreign_key(
        "fk_tg_authorization_wake_bundle", "tg_account_authorizations", "tg_authorization_wake_bundles",
        ["wake_bundle_id"], ["id"],
    )
    op.create_index(
        "ux_tg_authorization_current_slot", "tg_account_authorizations",
        ["account_id", "logical_slot"], unique=True,
        postgresql_where=sa.text("is_slot_current = true"),
        sqlite_where=sa.text("is_slot_current = 1"),
    )
    op.create_index(
        "ux_tg_authorization_auth_key_digest", "tg_account_authorizations",
        ["auth_key_fingerprint_digest"], unique=True,
        postgresql_where=sa.text("auth_key_fingerprint_digest <> ''"),
        sqlite_where=sa.text("auth_key_fingerprint_digest <> ''"),
    )


def _add_account_columns() -> None:
    columns = (
        sa.Column("current_authorization_id", sa.Integer(), nullable=True),
        sa.Column("authorization_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("authorization_fact_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("connection_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("authorization_contract_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("business_runtime_status", sa.String(40), nullable=False, server_default="unknown"),
        sa.Column("sv_redundancy_status", sa.String(40), nullable=False, server_default="unknown"),
        sa.Column("authorization_recovery_status", sa.String(40), nullable=False, server_default="not_configured"),
        sa.Column("account_lifecycle_status", sa.String(40), nullable=False, server_default="business_active"),
    )
    for column in columns:
        op.add_column("tg_accounts", column)
    op.create_foreign_key(
        "fk_tg_account_current_authorization", "tg_accounts", "tg_account_authorizations",
        ["current_authorization_id"], ["id"], use_alter=True,
    )


def _add_device_cleanup_v2() -> None:
    batch_columns = (
        sa.Column("requested_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("eligible_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_reason_counts", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("idempotency_key", sa.String(100), nullable=False, server_default=""),
    )
    for column in batch_columns:
        op.add_column("tg_account_security_batches", column)
    op.create_index(
        "ux_security_batch_idempotency", "tg_account_security_batches", ["tenant_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key <> ''"),
        sqlite_where=sa.text("idempotency_key <> ''"),
    )
    item_columns = (
        sa.Column("executor_authorization_id", sa.Integer(), nullable=True),
        sa.Column("executor_fact_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("executor_telegram_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("protected_manifest_digest", sa.String(64), nullable=False, server_default=""),
        sa.Column("target_set_digest", sa.String(64), nullable=False, server_default=""),
        sa.Column("remote_effect_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final_readback_digest", sa.String(64), nullable=False, server_default=""),
    )
    for column in item_columns:
        op.add_column("tg_account_security_batch_items", column)
    op.create_foreign_key(
        "fk_security_item_executor_authorization", "tg_account_security_batch_items", "tg_account_authorizations",
        ["executor_authorization_id"], ["id"],
    )
    op.create_table(
        "tg_account_device_cleanup_targets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_item_id", sa.Integer(), sa.ForeignKey("tg_account_security_batch_items.id"), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), sa.ForeignKey("tg_account_authorization_snapshots.id"), nullable=False),
        sa.Column("target_hash_ciphertext", sa.Text(), nullable=False),
        sa.Column("target_hash_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(40), nullable=False, server_default="pending"),
        sa.Column("remote_effect_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("batch_item_id", "target_hash_digest", name="uq_device_cleanup_target_hash"),
    )


def upgrade() -> None:
    if _schema_complete():
        return
    _create_runtime_tables()
    _create_batch_tables()
    _create_bundle_tables()
    _add_authorization_columns()
    _add_account_columns()
    _add_device_cleanup_v2()


def downgrade() -> None:
    raise RuntimeError("0157 authorization DR migration is intentionally irreversible")
