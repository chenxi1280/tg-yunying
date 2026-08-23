from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from app.models import (
    AuditLog,
    AuthorizationDrRuntimeContract,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationLocalActivateCase,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgAuthorizationOnlineAbcSlotResult,
)
from app.services._common import audit

from .abc_backup import preview_abc_backup
from .contracts import AuthorizationDrError
from .online_abc import UNKNOWN_OPERATION_STATUSES
from .online_abc_operations import online_abc_item_operations, online_abc_operation_keys


SOURCE_BLOCKER = "c_orphan_revoked_retry_ready"
REBASE_BLOCKER = "post_local_activate_rebase_ready"
AUDIT_ACTION = "重绑 local_activate 后 ABC frozen item"


def preview_post_activate_rebase(
    session, batch_id: str, account_id: int, case_id: str, *, idempotency_key: str,
) -> dict:
    batch, item, case = _inputs(session, batch_id, account_id, case_id)
    body = _preview_body(session, batch, item, case, idempotency_key)
    return {**body, "fingerprint": _fingerprint(body)}


def apply_post_activate_rebase(
    session,
    batch_id: str,
    account_id: int,
    case_id: str,
    *,
    idempotency_key: str,
    expected_fingerprint: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
) -> dict:
    _require_approval(requested_by, approved_by, approval_ref)
    batch, item = _locked_batch_item(session, batch_id, account_id)
    existing = _existing_audit(session, item.id, idempotency_key)
    if existing:
        _require_existing_fingerprint(existing, expected_fingerprint)
        return _result(item, case_id, expected_fingerprint, already_applied=True)
    _require_batch_approval(batch, requested_by, approved_by, approval_ref)
    preview = preview_post_activate_rebase(
        session, batch_id, account_id, case_id, idempotency_key=idempotency_key,
    )
    if preview["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "Post-activate rebase preview changed")
    slot = _b_slot(session, item.id)
    _apply_item_projection(item, slot, preview)
    audit(
        session,
        tenant_id=batch.tenant_id,
        actor=approved_by.strip(),
        action=AUDIT_ACTION,
        target_type="tg_authorization_online_abc_items",
        target_id=item.id,
        detail=(
            f"idempotency_key={idempotency_key.strip()}; fingerprint={expected_fingerprint}; "
            f"case_id={case_id}; approval_ref={approval_ref.strip()}"
        ),
    )
    session.commit()
    return _result(item, case_id, expected_fingerprint, already_applied=False)


def require_post_activate_rebase_resume(session, item, operations: dict) -> None:
    _require_runtime_and_unknown(session)
    audit_row = _latest_rebase_audit(session, item.id)
    case_id = _audit_value(audit_row, "case_id") if audit_row else ""
    case = session.get(TgAuthorizationLocalActivateCase, case_id) if case_id else None
    batch = session.get(TgAuthorizationOnlineAbcBatch, item.batch_id)
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    _require_rebased_state(session, batch, item, case, account, primary, operations)
    backup = preview_abc_backup(
        session,
        item.tenant_id,
        item.account_id,
        idempotency_key=online_abc_operation_keys(batch, item)["b"],
    )
    if _backup_projection(backup) != _item_backup_projection(item):
        raise AuthorizationDrError("online_abc_primary_drift", "Post-activate B route changed")


def _preview_body(session, batch, item, case, idempotency_key: str) -> dict:
    key = idempotency_key.strip()
    if not key:
        raise AuthorizationDrError("idempotency_key_required", "Post-activate rebase key is required")
    operations = online_abc_item_operations(session, batch, item)
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, account.current_authorization_id)
    _require_source_state(session, batch, item, case, account, primary, operations)
    backup = preview_abc_backup(
        session,
        batch.tenant_id,
        item.account_id,
        idempotency_key=online_abc_operation_keys(batch, item)["b"],
    )
    old_primary = session.get(TgAccountAuthorization, case.expected_current_authorization_id)
    return {
        "batch_id": batch.id,
        "batch_version": batch.version,
        "item_id": item.id,
        "item_version": item.version,
        "account_id": item.account_id,
        "case_id": case.id,
        "verification_operation_id": case.verification_operation_id,
        "verification_remote_message_id": case.verification_remote_message_id,
        "old_primary_authorization_id": old_primary.id,
        "new_primary_authorization_id": primary.id,
        "primary_fact_version": primary.fact_version,
        "authorization_generation": account.authorization_generation,
        "authorization_fact_generation": account.authorization_fact_generation,
        "connection_generation": account.connection_generation,
        "primary_session_digest": _digest(primary.session_ciphertext or ""),
        "primary_user_id_digest": primary.telegram_user_id_digest,
        "primary_auth_key_digest": primary.auth_key_fingerprint_digest,
        "source_c_authorization_id": item.source_c_authorization_id,
        "source_c_fact_version": item.source_c_fact_version,
        "source_c_slot_generation": item.source_c_slot_generation,
        **_backup_projection(backup),
        "idempotency_key": key,
    }


def _require_source_state(session, batch, item, case, account, primary, operations) -> None:
    valid = (
        batch.status == "stopped"
        and item.status == "stopped"
        and item.outcome == "runner_blocked"
        and item.blocker_code == SOURCE_BLOCKER
        and case.status == "applied"
        and case.account_id == item.account_id
        and case.target_authorization_id == account.current_authorization_id
        and bool(case.verification_operation_id and case.verification_remote_message_id)
        and operations["b"] is None
        and operations["e4"] is None
        and _compensated_c(operations["c"])
    )
    if not valid:
        raise AuthorizationDrError("online_abc_post_activate_unavailable", "Post-activate checkpoint is unavailable")
    _require_runtime_and_unknown(session)
    _require_primary_pair(session, item, case, account, primary)
    _require_source_c(session, item)


def _require_rebased_state(session, batch, item, case, account, primary, operations) -> None:
    valid = (
        batch
        and batch.status == "stopped"
        and item.status == "stopped"
        and item.outcome == "runner_blocked"
        and item.blocker_code == REBASE_BLOCKER
        and item.standby_1_plan == "provision"
        and case
        and case.status == "applied"
        and case.account_id == item.account_id
        and case.target_authorization_id == item.primary_authorization_id
        and operations["b"] is None
        and operations["e4"] is None
        and _compensated_c(operations["c"])
    )
    if not valid:
        raise AuthorizationDrError("online_abc_resume_blocker_forbidden", "Post-activate rebase changed")
    _require_primary_pair(session, item, case, account, primary)
    _require_primary_baseline(item, account, primary)
    _require_source_c(session, item)
    slot = _b_slot(session, item.id)
    if slot.outcome != "pending" or slot.operation_id is not None or slot.blocker_code:
        raise AuthorizationDrError("online_abc_resume_remote_effect_started", "Post-activate B slot changed")


def _require_primary_pair(session, item, case, account, primary) -> None:
    old_primary = session.get(TgAccountAuthorization, case.expected_current_authorization_id)
    verification = session.get(TgAuthorizationDrOperation, case.verification_operation_id)
    valid = (
        account
        and primary
        and account.current_authorization_id == primary.id
        and primary.id == case.target_authorization_id
        and primary.is_current
        and primary.is_slot_current
        and primary.logical_slot == "standby_1"
        and primary.provision_region_code == "sv"
        and primary.status == "active"
        and primary.health_status == "healthy"
        and primary.last_authoritative_error_code == ""
        and primary.disabled_at is None
        and primary.protected_from_cleanup
        and primary.session_ciphertext == account.session_ciphertext
        and primary.developer_app_id == account.developer_app_id
        and account.status == "在线"
        and primary.telegram_user_id_digest
        and primary.auth_key_fingerprint_digest
        and case.telegram_user_id_digest == primary.telegram_user_id_digest
        and case.auth_key_fingerprint_digest == primary.auth_key_fingerprint_digest
        and case.verified_at is not None
        and old_primary
        and old_primary.id != primary.id
        and not old_primary.is_current
        and old_primary.protected_from_cleanup
        and old_primary.health_status == "invalid"
        and old_primary.last_authoritative_error_code == "authorization_key_duplicated"
        and verification
        and verification.id == case.verification_operation_id
        and verification.source_authorization_id == primary.id
        and verification.code_source_authorization_id == primary.id
        and verification.expected_current_authorization_id == primary.id
        and verification.expected_authorization_generation == account.authorization_generation
        and verification.expected_authorization_fact_generation == account.authorization_fact_generation
        and verification.expected_connection_generation == account.connection_generation
        and verification.expected_code_source_fact_version == primary.fact_version
        and verification.expected_code_source_user_id_digest == primary.telegram_user_id_digest
        and verification.expected_code_source_auth_key_digest == primary.auth_key_fingerprint_digest
        and verification.status == "succeeded"
        and verification.remote_call_state == "succeeded"
    )
    if not valid:
        raise AuthorizationDrError("online_abc_primary_drift", "Activated A or retained old A changed")


def _require_primary_baseline(item, account, primary) -> None:
    valid = (
        item.primary_authorization_id == primary.id
        and item.primary_fact_version == primary.fact_version
        and item.authorization_generation == account.authorization_generation
        and item.authorization_fact_generation == account.authorization_fact_generation
        and item.connection_generation == account.connection_generation
        and item.primary_session_digest == _digest(primary.session_ciphertext or "")
    )
    if not valid:
        raise AuthorizationDrError("online_abc_primary_drift", "Rebased A baseline changed")


def _require_source_c(session, item) -> None:
    source = session.get(TgAccountAuthorization, item.source_c_authorization_id)
    valid = (
        source
        and source.account_id == item.account_id
        and source.fact_version == item.source_c_fact_version
        and source.slot_generation == item.source_c_slot_generation
        and source.logical_slot == "standby_2"
        and source.is_slot_current
        and source.protected_from_cleanup
    )
    if not valid:
        raise AuthorizationDrError("migration_source_drift", "Frozen C source changed")


def _require_runtime_and_unknown(session) -> None:
    contract = session.get(AuthorizationDrRuntimeContract, 1)
    if not contract or contract.mode != "off" or contract.claim_scope_operation_id:
        raise AuthorizationDrError("runtime_capability_unproven", "DR runtime must be off")
    unknown = session.scalar(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.status.in_(UNKNOWN_OPERATION_STATUSES),
    ).limit(1))
    if unknown:
        raise AuthorizationDrError("global_reconcile_unknown", "Global reconcile unknown must be zero")


def _apply_item_projection(item, slot, preview: dict) -> None:
    item.primary_authorization_id = preview["new_primary_authorization_id"]
    item.primary_fact_version = preview["primary_fact_version"]
    item.authorization_generation = preview["authorization_generation"]
    item.authorization_fact_generation = preview["authorization_fact_generation"]
    item.connection_generation = preview["connection_generation"]
    item.primary_session_digest = preview["primary_session_digest"]
    item.app_b_id = preview["app_b_id"]
    item.app_b_credentials_version = preview["app_b_credentials_version"]
    item.app_b_assignment_purpose = preview["app_b_assignment_purpose"]
    item.app_b_assignment_version = preview["app_b_assignment_version"]
    item.proxy_id = preview["proxy_id"]
    item.standby_1_plan = "provision"
    item.blocker_code = REBASE_BLOCKER
    item.version += 1
    slot.outcome = "pending"
    slot.operation_id = None
    slot.blocker_code = ""
    slot.version += 1


def _inputs(session, batch_id: str, account_id: int, case_id: str):
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ))
    case = session.get(TgAuthorizationLocalActivateCase, case_id)
    if not batch or not item or not case:
        raise AuthorizationDrError("online_abc_post_activate_unavailable", "Post-activate inputs are unavailable")
    return batch, item, case


def _locked_batch_item(session, batch_id: str, account_id: int):
    batch = session.scalar(select(TgAuthorizationOnlineAbcBatch).where(
        TgAuthorizationOnlineAbcBatch.id == batch_id,
    ).with_for_update())
    item = session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ).with_for_update())
    if not batch or not item:
        raise AuthorizationDrError("online_abc_post_activate_unavailable", "Post-activate item is unavailable")
    return batch, item


def _b_slot(session, item_id: str):
    slot = session.scalar(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.item_id == item_id,
        TgAuthorizationOnlineAbcSlotResult.logical_slot == "standby_1",
    ).with_for_update())
    if not slot:
        raise AuthorizationDrError("online_abc_post_activate_unavailable", "B slot result is unavailable")
    return slot


def _compensated_c(operation) -> bool:
    return bool(
        operation
        and operation.status == "migration_rolled_back_forward"
        and operation.remote_call_state == "compensated"
        and operation.reconcile_status == "applied"
        and operation.blocker_code == "orphan_remote_authorization_revoked"
    )


def _backup_projection(backup: dict) -> dict:
    return {
        "app_b_id": backup["app_b_id"],
        "app_b_credentials_version": backup["app_b_credentials_version"],
        "app_b_assignment_purpose": backup["app_b_assignment_purpose"],
        "app_b_assignment_version": backup["assignment_version"],
        "proxy_id": backup["proxy_id"],
    }


def _item_backup_projection(item) -> dict:
    return {
        "app_b_id": item.app_b_id,
        "app_b_credentials_version": item.app_b_credentials_version,
        "app_b_assignment_purpose": item.app_b_assignment_purpose,
        "app_b_assignment_version": item.app_b_assignment_version,
        "proxy_id": item.proxy_id,
    }


def _require_batch_approval(batch, requested_by: str, approved_by: str, approval_ref: str) -> None:
    expected = (batch.requested_by, batch.approved_by, batch.approval_ref)
    actual = (requested_by.strip(), approved_by.strip(), approval_ref.strip())
    if expected != actual:
        raise AuthorizationDrError("online_abc_runner_approval_mismatch", "Rebase approval differs from batch")


def _require_approval(requested_by: str, approved_by: str, approval_ref: str) -> None:
    values = (requested_by.strip(), approved_by.strip(), approval_ref.strip())
    if not all(values):
        raise AuthorizationDrError("approval_ref_required", "Rebase approval is incomplete")
    if values[0] == values[1]:
        raise AuthorizationDrError("approval_actor_conflict", "Approver must differ from requester")


def _existing_audit(session, item_id: str, key: str):
    token = f"idempotency_key={key.strip()};"
    rows = session.scalars(select(AuditLog).where(
        AuditLog.action == AUDIT_ACTION,
        AuditLog.target_type == "tg_authorization_online_abc_items",
        AuditLog.target_id == item_id,
    ).order_by(AuditLog.id.desc()))
    return next((row for row in rows if token in row.detail), None)


def _latest_rebase_audit(session, item_id: str):
    return _latest_action_audit(session, item_id, AUDIT_ACTION)


def _latest_action_audit(session, item_id: str, action: str):
    return session.scalar(select(AuditLog).where(
        AuditLog.action == action,
        AuditLog.target_type == "tg_authorization_online_abc_items",
        AuditLog.target_id == item_id,
    ).order_by(AuditLog.id.desc()).limit(1))


def _require_existing_fingerprint(row, fingerprint: str) -> None:
    if f"fingerprint={fingerprint};" not in row.detail:
        raise AuthorizationDrError("migration_fingerprint_conflict", "Rebase idempotency key changed")


def _audit_value(row, key: str) -> str:
    prefix = f"{key}="
    for part in row.detail.split("; "):
        if part.startswith(prefix):
            return part.removeprefix(prefix).strip()
    return ""


def _result(item, case_id: str, fingerprint: str, *, already_applied: bool) -> dict:
    return {
        "batch_id": item.batch_id,
        "item_id": item.id,
        "account_id": item.account_id,
        "case_id": case_id,
        "primary_authorization_id": item.primary_authorization_id,
        "standby_1_plan": item.standby_1_plan,
        "blocker_code": item.blocker_code,
        "fingerprint": fingerprint,
        "already_applied": already_applied,
    }


def _fingerprint(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


__all__ = [
    "REBASE_BLOCKER",
    "apply_post_activate_rebase",
    "preview_post_activate_rebase",
    "require_post_activate_rebase_resume",
]
