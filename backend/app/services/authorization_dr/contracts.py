from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


PRIMARY_REGULAR_EGRESS_ID = "primary_regular:direct"
PRIMARY_REGULAR_EGRESS_VERSION = 1


class AuthorizationDrError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CopyReceipt:
    copy_kind: str
    object_ref_digest: str
    ciphertext_digest: str
    immutable_version: str
    write_receipt_digest: str
    readback_receipt_digest: str
    write_verified_at: datetime
    readback_verified_at: datetime
    decrypt_verified_at: datetime


@dataclass(frozen=True)
class WakeBundleReceipt:
    bundle_generation: int
    ciphertext_digest: str
    wrapped_dek_ciphertext: str
    kms_key_ref_digest: str
    kms_key_version: str
    auth_key_fingerprint_digest: str
    telegram_user_id_digest: str
    authorization_fingerprint_digest: str
    remote_authorization_hash_ciphertext: str
    inventory_sequence: int
    inventory_manifest_digest: str
    copies: tuple[CopyReceipt, ...]


@dataclass(frozen=True)
class RestoreProbeReceipt:
    probe_generation: int
    source_copy_kind: str
    status: str
    session_parse_status: str
    authorization_status: str
    identity_match_status: str
    auth_key_match_status: str
    source_client_disconnected: bool
    probe_client_disconnected: bool
    zeroize_receipt_digest: str


@dataclass(frozen=True)
class OperationClaim:
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


__all__ = [
    "AuthorizationDrError",
    "CopyReceipt",
    "OperationClaim",
    "PRIMARY_REGULAR_EGRESS_ID",
    "PRIMARY_REGULAR_EGRESS_VERSION",
    "RestoreProbeReceipt",
    "WakeBundleReceipt",
]
