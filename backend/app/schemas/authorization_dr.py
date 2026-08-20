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


class DrLoginCodeOut(DrStrictModel):
    code: str


class DrOwnerRequest(DrStrictModel):
    owner_epoch: int = Field(ge=1)
    lease_token: str = Field(min_length=1, max_length=80)


class DrLoginFailureRequest(DrOwnerRequest):
    blocker_code: str = Field(pattern="^phone_number_banned$")


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
    standby_egress_id: str = Field(min_length=1, max_length=80)
    active_client_count: int = Field(ge=0, le=1)
    node_version: int = Field(ge=1)


__all__ = [name for name in globals() if name.startswith("Dr")]
