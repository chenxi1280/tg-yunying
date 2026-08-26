from __future__ import annotations

import hashlib


def allows_partial_identity_resume(snapshot) -> bool:
    if snapshot is None:
        return True
    return bool(
        snapshot.trusted_session_status == "unknown"
        and snapshot.two_fa_status == "unknown"
        and not snapshot.two_fa_password_ciphertext
        and not snapshot.two_fa_password_hint
        and snapshot.two_fa_password_stored_at is None
        and snapshot.external_authorization_count == 0
        and snapshot.last_device_scan_at is None
        and snapshot.last_2fa_check_at is None
        and not snapshot.trusted_device_label
        and not snapshot.last_error
    )


def security_evidence(snapshot) -> dict:
    if snapshot is None:
        return _absent_evidence()
    return {
        "security_snapshot_present": True,
        "security_snapshot_id": snapshot.id,
        "security_snapshot_tenant_id": snapshot.tenant_id,
        "security_snapshot_account_id": snapshot.account_id,
        "security_trusted_session_status": snapshot.trusted_session_status,
        "security_two_fa_status": snapshot.two_fa_status,
        "managed_secret_ref_digest": _digest(snapshot.two_fa_password_ciphertext or ""),
        "managed_secret_hint_digest": _digest(snapshot.two_fa_password_hint or ""),
        "managed_secret_stored_at": str(snapshot.two_fa_password_stored_at or ""),
        "security_external_authorization_count": snapshot.external_authorization_count,
        "security_last_device_scan_at": str(snapshot.last_device_scan_at or ""),
        "security_last_2fa_check_at": str(snapshot.last_2fa_check_at or ""),
        "security_trusted_device_label_digest": _digest(snapshot.trusted_device_label or ""),
        "security_last_error_digest": _digest(snapshot.last_error or ""),
        "security_trace_id_digest": _digest(snapshot.trace_id or ""),
        "security_profile_status": snapshot.profile_status,
        "security_profile_last_updated_at": str(snapshot.profile_last_updated_at or ""),
        "security_last_hardened_at": str(snapshot.last_hardened_at or ""),
        "security_created_at": str(snapshot.created_at or ""),
        "security_updated_at": str(snapshot.updated_at or ""),
    }


def _absent_evidence() -> dict:
    return {
        "security_snapshot_present": False,
        "security_snapshot_id": None,
        "security_snapshot_tenant_id": None,
        "security_snapshot_account_id": None,
        "security_trusted_session_status": "",
        "security_two_fa_status": "",
        "managed_secret_ref_digest": _digest(""),
        "managed_secret_hint_digest": _digest(""),
        "managed_secret_stored_at": "",
        "security_external_authorization_count": 0,
        "security_last_device_scan_at": "",
        "security_last_2fa_check_at": "",
        "security_trusted_device_label_digest": _digest(""),
        "security_last_error_digest": _digest(""),
        "security_trace_id_digest": _digest(""),
        "security_profile_status": "",
        "security_profile_last_updated_at": "",
        "security_last_hardened_at": "",
        "security_created_at": "",
        "security_updated_at": "",
    }


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


__all__ = ["allows_partial_identity_resume", "security_evidence"]
