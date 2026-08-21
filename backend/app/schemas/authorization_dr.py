from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .api import ApiModel


class DrStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DrMigrationPreviewRequest(DrStrictModel):
    account_ids: list[int] = Field(min_length=1)


class DrMigrationApprovalRequest(DrStrictModel):
    expected_version: int = Field(ge=1)
    approval_ref: str = Field(min_length=1, max_length=160)


class DrBatchItemOut(ApiModel):
    id: str
    account_id: int
    ordinal: int
    expected_source_authorization_id: int
    expected_source_fact_version: int
    expected_source_generation: int
    target_generation: int
    status: str
    outcome: str
    blocker_code: str
    operation_id: str | None


class DrBatchOut(ApiModel):
    id: str
    tenant_id: int
    operation_type: str
    target_set_fingerprint: str
    target_count: int
    status: str
    version: int
    requested_by: str
    approval_ref: str
    approved_by: str
    approved_at: datetime | None
    created_at: datetime
    execution_finished_at: datetime | None
    finished_at: datetime | None
    status_counts: dict[str, int]
    items: list[DrBatchItemOut]


class DrOperationOut(ApiModel):
    id: str
    tenant_id: int
    account_id: int
    operation_type: str
    logical_slot: str
    source_authorization_id: int | None
    candidate_authorization_id: int | None
    source_generation: int
    target_generation: int
    developer_app_id: int
    developer_app_api_id_snapshot: int
    assignment_version: int
    egress_id: str
    egress_version: int
    status: str
    blocker_code: str
    operation_version: int
    execution_generation: int
    owner_node_id: str
    owner_epoch: int
    remote_effect_started_at: datetime | None
    remote_call_state: str
    reconcile_case_id: str | None
    reconcile_status: str
    reconciled_at: datetime | None
    requested_by: str
    approved_by: str
    approval_ref: str
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class DrNodeOut(ApiModel):
    id: str
    region_code: str
    purpose: str
    capability_version: str
    runtime_image_sha: str
    standby_egress_id: str
    status: str
    active_client_count: int
    last_heartbeat_at: datetime | None
    version: int


class DrClaimRequest(DrStrictModel):
    purpose: str = Field(pattern="^migrate_standby_2$")


class DrClaimOut(ApiModel):
    operation_id: str
    account_id: int
    owner_node_id: str
    owner_epoch: int
    lease_token: str
    lease_expires_at: datetime
    target_generation: int
    developer_app_id: int
    developer_app_api_id: int
    developer_app_credentials_version: int
    egress_id: str
    egress_version: int


class DrLoginMaterialOut(DrStrictModel):
    phone: str
    password_2fa: str
    api_id: int
    api_hash: str
    app_name: str
    credentials_version: int


class DrArtifactClaimOut(ApiModel):
    operation_id: str
    account_id: int
    owner_node_id: str
    owner_epoch: int
    lease_token: str
    lease_expires_at: datetime
    target_generation: int
    developer_app_id: int
    egress_id: str
    egress_version: int
    classification: str
    expected_ciphertext_digest: str
    expected_inventory_sequence: int


class DrArtifactProbeMaterialOut(DrStrictModel):
    api_id: int
    api_hash: str
    app_name: str
    credentials_version: int


class DrLoginCodeOut(DrStrictModel):
    code: str


class DrOwnerRequest(DrStrictModel):
    owner_epoch: int = Field(ge=1)
    lease_token: str = Field(min_length=1, max_length=80)


class DrLoginFailureRequest(DrOwnerRequest):
    blocker_code: str = Field(pattern="^(phone_number_banned|two_fa_invalid)$")


class DrStageFactRequest(DrOwnerRequest):
    stage: str = Field(pattern="^(remote_login_confirmed|local_copy_verified|snapshot_copy_verified|inventory_persisted)$")
    manifest_digest: str = Field(pattern="^[0-9a-f]{64}$")
    bundle_generation: int = Field(default=0, ge=0)
    ciphertext_digest: str = ""
    inventory_sequence: int = Field(default=0, ge=0)


class DrCopyReceiptRequest(DrStrictModel):
    copy_kind: str = Field(pattern="^(local_persistent|remote_ssh_snapshot|object_snapshot)$")
    object_ref_digest: str = Field(min_length=64, max_length=64)
    ciphertext_digest: str = Field(min_length=64, max_length=64)
    immutable_version: str = Field(min_length=1, max_length=120)
    write_receipt_digest: str = Field(min_length=64, max_length=64)
    readback_receipt_digest: str = Field(min_length=64, max_length=64)
    write_verified_at: datetime
    readback_verified_at: datetime
    decrypt_verified_at: datetime


class DrWakeBundleRequest(DrOwnerRequest):
    bundle_generation: int = Field(ge=1)
    ciphertext_digest: str = Field(min_length=64, max_length=64)
    wrapped_dek_ciphertext: str = Field(min_length=1)
    kms_key_ref_digest: str = Field(min_length=64, max_length=64)
    kms_key_version: str = Field(min_length=1, max_length=80)
    auth_key_fingerprint_digest: str = Field(min_length=64, max_length=64)
    telegram_user_id_digest: str = Field(min_length=64, max_length=64)
    authorization_fingerprint_digest: str = Field(min_length=64, max_length=64)
    remote_authorization_hash: str = Field(min_length=1)
    inventory_sequence: int = Field(ge=1)
    inventory_manifest_digest: str = Field(min_length=64, max_length=64)
    copies: list[DrCopyReceiptRequest] = Field(min_length=2, max_length=2)


class DrRestoreProbeRequest(DrOwnerRequest):
    probe_generation: int = Field(ge=1)
    source_copy_kind: str = Field(pattern="^(remote_ssh_snapshot|object_snapshot)$")
    status: str
    session_parse_status: str
    authorization_status: str
    identity_match_status: str
    auth_key_match_status: str
    source_client_disconnected: bool
    probe_client_disconnected: bool
    zeroize_receipt_digest: str = Field(min_length=64, max_length=64)


class DrNodeHeartbeatRequest(DrStrictModel):
    region_code: str = Field(pattern="^my$")
    purpose: str = Field(pattern="^standby_session_dr$")
    capability_version: str = Field(min_length=1, max_length=80)
    runtime_image_sha: str = Field(pattern="^[0-9a-f]{40}([0-9a-f]{24})?$")
    standby_egress_id: str = Field(min_length=1, max_length=80)
    active_client_count: int = Field(ge=0, le=1)
    node_version: int = Field(ge=1)


class DrReconcileEvidence(DrStrictModel):
    kind: str = Field(pattern="^(historical_typed_login_failure|remote_orphan_without_bundle|confirmed_no_remote_effect|remote_unproven|artifact_forward_recovery)$")
    blocker_code: str = ""
    event_digest: str = ""
    source_ref: str = ""
    runtime_image_sha: str = ""
    node_id: str = ""
    owner_epoch: int = 0
    bundle_generation: int = 0
    ciphertext_digest: str = ""
    inventory_sequence: int = 0
    remote_set_before_digest: str = ""
    remote_set_after_digest: str = ""
    new_device_count: int = -1


class DrReconcilePreviewRequest(DrStrictModel):
    expected_operation_version: int = Field(ge=1)
    evidence: DrReconcileEvidence


class DrReconcileApplyRequest(DrStrictModel):
    expected_operation_version: int = Field(ge=1)
    evidence_fingerprint: str = Field(pattern="^[0-9a-f]{64}$")
    approval_ref: str = Field(min_length=1, max_length=160)


class DrReconcileOut(ApiModel):
    id: str
    tenant_id: int
    account_id: int
    operation_id: str
    reconcile_generation: int
    status: str
    classification: str
    recommended_transition: str
    blocker_code: str
    expected_operation_version: int
    expected_item_version: int
    expected_source_fact_version: int
    expected_owner_epoch: int
    expected_node_id: str
    expected_runtime_image_sha: str
    evidence_fingerprint: str
    evidence_manifest: dict
    persisted_artifact_state: str
    requested_by: str
    applied_by: str
    approval_ref: str
    created_at: datetime
    applied_at: datetime | None


class DrLocalActivatePreviewRequest(DrStrictModel):
    reason: str = Field(min_length=1, max_length=255)


class DrLocalActivateApplyRequest(DrStrictModel):
    fingerprint: str = Field(pattern="^[0-9a-f]{64}$")
    approval_ref: str = Field(min_length=1, max_length=160)


class DrLocalActivateOut(ApiModel):
    id: str
    tenant_id: int
    account_id: int
    target_authorization_id: int
    expected_current_authorization_id: int | None
    expected_authorization_generation: int
    expected_fact_generation: int
    expected_connection_generation: int
    expected_target_fact_version: int
    fingerprint: str
    reason: str
    status: str
    requested_by: str
    applied_by: str
    approval_ref: str
    created_at: datetime
    applied_at: datetime | None


__all__ = [name for name in globals() if name.startswith("Dr")]
