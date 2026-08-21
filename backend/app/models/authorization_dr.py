from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def new_uuid() -> str:
    return str(uuid4())


class AuthorizationDrRuntimeContract(Base):
    __tablename__ = "authorization_dr_runtime_contracts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    mode: Mapped[str] = mapped_column(String(24), default="off")
    contract_epoch: Mapped[int] = mapped_column(Integer, default=1)
    cluster_incarnation: Mapped[str] = mapped_column(String(80), default="")
    mutation_hold_reason: Mapped[str] = mapped_column(String(80), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[str] = mapped_column(String(100), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class AuthorizationDrExecutionNode(Base):
    __tablename__ = "authorization_dr_execution_nodes"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    region_code: Mapped[str] = mapped_column(String(24))
    purpose: Mapped[str] = mapped_column(String(40))
    capability_version: Mapped[str] = mapped_column(String(80))
    runtime_image_sha: Mapped[str] = mapped_column(String(64), default="")
    standby_egress_id: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), default="offline")
    active_client_count: Mapped[int] = mapped_column(Integer, default=0)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class TelegramEgressAssignment(Base):
    __tablename__ = "telegram_egress_assignments"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(40), unique=True)
    region_code: Mapped[str] = mapped_column(String(24))
    secret_ref_digest: Mapped[str] = mapped_column(String(64), default="")
    observed_ip_hmac: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(24), default="unproven")
    connectivity_status: Mapped[str] = mapped_column(String(24), default="unproven")
    version: Mapped[int] = mapped_column(Integer, default=1)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DeveloperAppSlotAssignment(Base):
    __tablename__ = "developer_app_slot_assignments"
    __table_args__ = (UniqueConstraint("developer_app_id", name="uq_dr_slot_assignment_app"),)

    slot_purpose: Mapped[str] = mapped_column(String(32), primary_key=True)
    developer_app_id: Mapped[int] = mapped_column(ForeignKey("telegram_developer_apps.id"))
    assignment_version: Mapped[int] = mapped_column(Integer, default=1)
    credentials_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="active")
    assigned_by: Mapped[str] = mapped_column(String(100))
    assigned_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TgAuthorizationDrBatch(Base):
    __tablename__ = "tg_authorization_dr_batches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_dr_batch_idempotency"),
        Index("ix_dr_batch_claim", "status", "last_claimed_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    operation_type: Mapped[str] = mapped_column(String(40), default="migrate_standby_2")
    idempotency_key: Mapped[str] = mapped_column(String(100))
    target_set_fingerprint: Mapped[str] = mapped_column(String(64))
    target_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="previewed")
    version: Mapped[int] = mapped_column(Integer, default=1)
    requested_by: Mapped[str] = mapped_column(String(100))
    approval_ref: Mapped[str] = mapped_column(String(160), default="")
    approved_by: Mapped[str] = mapped_column(String(100), default="")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    execution_finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TgAuthorizationDrBatchItem(Base):
    __tablename__ = "tg_authorization_dr_batch_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "account_id", name="uq_dr_batch_account"),
        Index("ix_dr_batch_item_claim", "status", "batch_id", "ordinal"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    batch_id: Mapped[str] = mapped_column(ForeignKey("tg_authorization_dr_batches.id"))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    ordinal: Mapped[int] = mapped_column(Integer)
    expected_source_authorization_id: Mapped[int] = mapped_column(ForeignKey("tg_account_authorizations.id"))
    expected_source_fact_version: Mapped[int] = mapped_column(Integer)
    expected_source_generation: Mapped[int] = mapped_column(Integer)
    target_generation: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    outcome: Mapped[str] = mapped_column(String(48), default="pending")
    blocker_code: Mapped[str] = mapped_column(String(100), default="")
    operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("tg_authorization_dr_operations.id"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TgAuthorizationDrOperation(Base):
    __tablename__ = "tg_authorization_dr_operations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_dr_operation_idempotency"),
        Index("ix_dr_operation_claim", "status", "lease_expires_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    batch_item_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "tg_authorization_dr_batch_items.id",
            use_alter=True,
            name="fk_dr_operation_batch_item",
        ),
        nullable=True,
    )
    operation_type: Mapped[str] = mapped_column(String(48))
    logical_slot: Mapped[str] = mapped_column(String(24))
    source_authorization_id: Mapped[int | None] = mapped_column(ForeignKey("tg_account_authorizations.id"), nullable=True)
    candidate_authorization_id: Mapped[int | None] = mapped_column(ForeignKey("tg_account_authorizations.id"), nullable=True)
    source_generation: Mapped[int] = mapped_column(Integer)
    target_generation: Mapped[int] = mapped_column(Integer)
    developer_app_id: Mapped[int] = mapped_column(ForeignKey("telegram_developer_apps.id"))
    developer_app_api_id_snapshot: Mapped[int] = mapped_column(Integer)
    developer_app_credentials_version: Mapped[int] = mapped_column(Integer)
    assignment_version: Mapped[int] = mapped_column(Integer)
    egress_id: Mapped[str] = mapped_column(String(80))
    egress_version: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(40), default="approved")
    blocker_code: Mapped[str] = mapped_column(String(100), default="")
    operation_version: Mapped[int] = mapped_column(Integer, default=1)
    execution_generation: Mapped[int] = mapped_column(Integer, default=1)
    owner_node_id: Mapped[str] = mapped_column(String(80), default="")
    owner_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str] = mapped_column(String(80), default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    remote_effect_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    remote_call_state: Mapped[str] = mapped_column(String(20), default="none")
    reconcile_case_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reconcile_status: Mapped[str] = mapped_column(String(32), default="none")
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    requested_by: Mapped[str] = mapped_column(String(100))
    approved_by: Mapped[str] = mapped_column(String(100))
    approval_ref: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TgAuthorizationDrReconcileCase(Base):
    __tablename__ = "tg_authorization_dr_reconcile_cases"
    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_dr_reconcile_operation"),
        UniqueConstraint("tenant_id", "apply_idempotency_key", name="uq_dr_reconcile_apply_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    operation_id: Mapped[str] = mapped_column(ForeignKey("tg_authorization_dr_operations.id"))
    reconcile_generation: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(40), default="open")
    classification: Mapped[str] = mapped_column(String(48))
    recommended_transition: Mapped[str] = mapped_column(String(48))
    blocker_code: Mapped[str] = mapped_column(String(100))
    expected_operation_version: Mapped[int] = mapped_column(Integer)
    expected_item_version: Mapped[int] = mapped_column(Integer)
    expected_source_fact_version: Mapped[int] = mapped_column(Integer)
    expected_owner_epoch: Mapped[int] = mapped_column(Integer)
    expected_node_id: Mapped[str] = mapped_column(String(80))
    expected_runtime_image_sha: Mapped[str] = mapped_column(String(64))
    evidence_fingerprint: Mapped[str] = mapped_column(String(64))
    evidence_manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    persisted_artifact_state: Mapped[str] = mapped_column(String(32), default="none")
    requested_by: Mapped[str] = mapped_column(String(100))
    applied_by: Mapped[str] = mapped_column(String(100), default="")
    approval_ref: Mapped[str] = mapped_column(String(160), default="")
    apply_idempotency_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TgAuthorizationWakeBundle(Base):
    __tablename__ = "tg_authorization_wake_bundles"
    __table_args__ = (
        UniqueConstraint("authorization_id", "bundle_generation", name="uq_dr_bundle_generation"),
        Index("ix_dr_bundle_active", "authorization_id", "is_active"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    authorization_id: Mapped[int] = mapped_column(ForeignKey("tg_account_authorizations.id"))
    operation_id: Mapped[str] = mapped_column(ForeignKey("tg_authorization_dr_operations.id"))
    bundle_generation: Mapped[int] = mapped_column(Integer)
    ciphertext_digest: Mapped[str] = mapped_column(String(64))
    wrapped_dek_ciphertext: Mapped[str] = mapped_column(Text)
    kms_key_ref_digest: Mapped[str] = mapped_column(String(64))
    kms_key_version: Mapped[str] = mapped_column(String(80))
    kms_decrypt_status: Mapped[str] = mapped_column(String(24), default="unproven")
    auth_key_fingerprint_digest: Mapped[str] = mapped_column(String(64))
    telegram_user_id_digest: Mapped[str] = mapped_column(String(64))
    recoverable_copy_count: Mapped[int] = mapped_column(Integer, default=0)
    receipt_status: Mapped[str] = mapped_column(String(32), default="prepared")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    protected_from_cleanup: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TgAuthorizationWakeBundleCopy(Base):
    __tablename__ = "tg_authorization_wake_bundle_copies"
    __table_args__ = (UniqueConstraint("bundle_id", "copy_kind", name="uq_dr_bundle_copy_kind"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    bundle_id: Mapped[str] = mapped_column(ForeignKey("tg_authorization_wake_bundles.id"))
    copy_kind: Mapped[str] = mapped_column(String(24))
    object_ref_digest: Mapped[str] = mapped_column(String(64))
    ciphertext_digest: Mapped[str] = mapped_column(String(64))
    immutable_version: Mapped[str] = mapped_column(String(120))
    write_receipt_digest: Mapped[str] = mapped_column(String(64))
    readback_receipt_digest: Mapped[str] = mapped_column(String(64))
    write_verified_at: Mapped[datetime] = mapped_column(DateTime)
    readback_verified_at: Mapped[datetime] = mapped_column(DateTime)
    decrypt_verified_at: Mapped[datetime] = mapped_column(DateTime)


class TgAuthorizationRestoreProbeFact(Base):
    __tablename__ = "tg_authorization_restore_probe_facts"
    __table_args__ = (UniqueConstraint("bundle_id", "probe_generation", name="uq_dr_restore_probe_generation"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    bundle_id: Mapped[str] = mapped_column(ForeignKey("tg_authorization_wake_bundles.id"))
    operation_id: Mapped[str] = mapped_column(ForeignKey("tg_authorization_dr_operations.id"))
    probe_generation: Mapped[int] = mapped_column(Integer)
    source_copy_kind: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24))
    session_parse_status: Mapped[str] = mapped_column(String(24))
    authorization_status: Mapped[str] = mapped_column(String(24))
    identity_match_status: Mapped[str] = mapped_column(String(24))
    auth_key_match_status: Mapped[str] = mapped_column(String(24))
    source_client_disconnected: Mapped[bool] = mapped_column(Boolean)
    probe_client_disconnected: Mapped[bool] = mapped_column(Boolean)
    zeroize_receipt_digest: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TgAuthorizationWakeInventoryEntry(Base):
    __tablename__ = "tg_authorization_wake_inventory_entries"
    __table_args__ = (UniqueConstraint("node_id", "inventory_sequence", name="uq_dr_inventory_sequence"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    node_id: Mapped[str] = mapped_column(String(80))
    inventory_sequence: Mapped[int] = mapped_column(BigInteger)
    operation_id: Mapped[str] = mapped_column(ForeignKey("tg_authorization_dr_operations.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    authorization_id: Mapped[int] = mapped_column(ForeignKey("tg_account_authorizations.id"))
    bundle_id: Mapped[str] = mapped_column(ForeignKey("tg_authorization_wake_bundles.id"))
    event_type: Mapped[str] = mapped_column(String(40))
    manifest_digest: Mapped[str] = mapped_column(String(64))
    decision_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_by_central_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TgAuthorizationSlotDecision(Base):
    __tablename__ = "tg_authorization_slot_decisions"
    __table_args__ = (
        UniqueConstraint("account_id", "logical_slot", "decision_generation", name="uq_dr_slot_decision_generation"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    logical_slot: Mapped[str] = mapped_column(String(24))
    decision_generation: Mapped[int] = mapped_column(Integer)
    expected_old_authorization_id: Mapped[int] = mapped_column(ForeignKey("tg_account_authorizations.id"))
    new_authorization_id: Mapped[int] = mapped_column(ForeignKey("tg_account_authorizations.id"))
    expected_old_slot_generation: Mapped[int] = mapped_column(Integer)
    new_slot_generation: Mapped[int] = mapped_column(Integer)
    expected_account_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="prepared")
    recovery_gate_status: Mapped[str] = mapped_column(String(32), default="pending")
    inventory_sequence: Mapped[int] = mapped_column(BigInteger)
    manifest_digest: Mapped[str] = mapped_column(String(64))
    prepared_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


__all__ = [name for name in globals() if name.startswith(("Authorization", "Developer", "Telegram", "TgAuthorization"))]
