from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from app.models import (
    AccountStatus,
    TgAccount,
    TgAccountAuthorization,
    TgAccountOnlineState,
    TgAuthorizationDrOperation,
    TgAuthorizationLocalActivateCase,
)
from app.services._common import _now, audit, gateway
from app.services.developer_apps import credentials_for_authorization

from .contracts import (
    AuthorizationDrError,
    PRIMARY_REGULAR_EGRESS_ID,
    PRIMARY_REGULAR_EGRESS_VERSION,
)
from .primary_fence import verified_code_source


def preview_local_activate_verification(
    session,
    tenant_id: int,
    account_id: int,
    case_id: str,
    *,
    idempotency_key: str,
) -> dict:
    if not idempotency_key.strip():
        raise AuthorizationDrError("idempotency_key_required", "Local activate verification key is required")
    case, account, primary = _verification_inputs(session, tenant_id, account_id, case_id)
    payload = _preview_payload(case, account, primary, idempotency_key)
    return {**payload, "fingerprint": _fingerprint(payload)}


def apply_local_activate_verification(
    session,
    tenant_id: int,
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
    existing = _operation_by_key(session, tenant_id, idempotency_key)
    if existing:
        return _existing_result(session, existing, expected_fingerprint)
    preview = preview_local_activate_verification(
        session,
        tenant_id,
        account_id,
        case_id,
        idempotency_key=idempotency_key,
    )
    if preview["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "Verification preview changed")
    case, account, primary = _verification_inputs(session, tenant_id, account_id, case_id)
    operation = _new_operation(account, primary, preview, requested_by, approved_by, approval_ref)
    session.add(operation)
    session.flush()
    case.verification_operation_id = operation.id
    session.commit()
    return _execute_verification(session, operation, case.id)


def _execute_verification(session, operation, case_id: str) -> dict:
    primary = verified_code_source(session, operation)
    operation.status = "send_remote_started"
    operation.remote_call_state = "started"
    operation.remote_effect_started_at = _now()
    operation.operation_version += 1
    session.commit()
    marker = f"LOCAL-ACTIVATE-VERIFY account={operation.account_id} operation={operation.id}"
    try:
        result = gateway.send_message(
            operation.account_id,
            0,
            marker,
            [],
            primary.session_ciphertext,
            "me",
            credentials_for_authorization(session, primary),
        )
    except Exception as exc:
        return _mark_unknown(session, operation.id, case_id, type(exc).__name__)
    if not result.ok or not result.remote_message_id:
        code = str(result.failure_type or "local_activate_send_failed")
        if result.remote_mutation_started is False:
            return _mark_failed(session, operation.id, case_id, code)
        return _mark_unknown(session, operation.id, case_id, code)
    return _mark_succeeded(session, operation.id, case_id, str(result.remote_message_id))


def _mark_succeeded(session, operation_id: str, case_id: str, remote_message_id: str) -> dict:
    operation = session.get(TgAuthorizationDrOperation, operation_id)
    primary = verified_code_source(session, operation)
    account = session.get(TgAccount, operation.account_id)
    case = session.get(TgAuthorizationLocalActivateCase, case_id)
    if not account or not case or case.target_authorization_id != primary.id:
        raise AuthorizationDrError("authorization_version_conflict", "Verification target changed")
    operation.status = "succeeded"
    operation.remote_call_state = "succeeded"
    operation.finished_at = _now()
    operation.operation_version += 1
    case.status = "applied"
    case.verification_remote_message_id = remote_message_id
    case.verification_blocker_code = ""
    case.verified_at = _now()
    _release_verified_primary(session, account)
    _audit_success(session, operation, case, remote_message_id)
    session.commit()
    return _result(operation, case)


def _mark_failed(session, operation_id: str, case_id: str, code: str) -> dict:
    operation = session.get(TgAuthorizationDrOperation, operation_id)
    case = session.get(TgAuthorizationLocalActivateCase, case_id)
    operation.status = "failed"
    operation.remote_call_state = "failed"
    operation.blocker_code = code[:100]
    operation.finished_at = _now()
    operation.operation_version += 1
    case.status = "verification_failed"
    case.verification_blocker_code = code[:100]
    session.commit()
    return _result(operation, case)


def _mark_unknown(session, operation_id: str, case_id: str, code: str) -> dict:
    session.rollback()
    operation = session.get(TgAuthorizationDrOperation, operation_id)
    case = session.get(TgAuthorizationLocalActivateCase, case_id)
    operation.status = "reconcile_unknown"
    operation.remote_call_state = "unknown"
    operation.blocker_code = code[:100]
    operation.operation_version += 1
    case.status = "verification_unknown"
    case.verification_blocker_code = code[:100]
    session.commit()
    return _result(operation, case)


def _verification_inputs(session, tenant_id: int, account_id: int, case_id: str):
    case = session.get(TgAuthorizationLocalActivateCase, case_id)
    account = session.get(TgAccount, account_id)
    primary = session.get(TgAccountAuthorization, case.target_authorization_id) if case else None
    valid = (
        case
        and account
        and primary
        and case.tenant_id == tenant_id
        and case.account_id == account_id
        and case.status == "applied_pending_verification"
        and account.current_authorization_id == primary.id
        and account.status == AccountStatus.NEED_RELOGIN.value
        and primary.logical_slot in {"primary", "standby_1"}
        and primary.is_current
        and primary.health_status == "healthy"
    )
    if not valid:
        raise AuthorizationDrError("local_activate_verification_unavailable", "Pending activation is unavailable")
    return case, account, primary


def _preview_payload(case, account, primary, key: str) -> dict:
    return {
        "tenant_id": account.tenant_id,
        "account_id": account.id,
        "case_id": case.id,
        "idempotency_key": key.strip(),
        "current_authorization_id": primary.id,
        "primary_fact_version": primary.fact_version,
        "authorization_generation": account.authorization_generation,
        "authorization_fact_generation": account.authorization_fact_generation,
        "connection_generation": account.connection_generation,
        "telegram_user_id_digest": primary.telegram_user_id_digest,
        "auth_key_fingerprint_digest": primary.auth_key_fingerprint_digest,
    }


def _new_operation(account, primary, preview, requested_by, approved_by, approval_ref):
    return TgAuthorizationDrOperation(
        tenant_id=account.tenant_id,
        account_id=account.id,
        operation_type="local_activate_send_verify",
        logical_slot="primary",
        source_authorization_id=primary.id,
        code_source_authorization_id=primary.id,
        source_generation=primary.slot_generation,
        target_generation=primary.slot_generation,
        expected_current_authorization_id=primary.id,
        expected_authorization_generation=account.authorization_generation,
        expected_authorization_fact_generation=account.authorization_fact_generation,
        expected_connection_generation=account.connection_generation,
        expected_code_source_fact_version=primary.fact_version,
        expected_code_source_user_id_digest=primary.telegram_user_id_digest,
        expected_code_source_auth_key_digest=primary.auth_key_fingerprint_digest,
        developer_app_id=primary.developer_app_id,
        developer_app_api_id_snapshot=primary.developer_app_api_id_snapshot,
        developer_app_credentials_version=primary.developer_app.credentials_version,
        assignment_version=1,
        egress_id=PRIMARY_REGULAR_EGRESS_ID,
        egress_version=PRIMARY_REGULAR_EGRESS_VERSION,
        idempotency_key=preview["idempotency_key"],
        request_fingerprint=preview["fingerprint"],
        status="approved",
        requested_by=requested_by.strip(),
        approved_by=approved_by.strip(),
        approval_ref=approval_ref.strip(),
    )


def _release_verified_primary(session, account) -> None:
    timestamp = _now()
    account.status = AccountStatus.ACTIVE.value
    account.last_active_at = timestamp
    account.health_score = max(account.health_score, 90)
    account.business_runtime_status = "degraded"
    state = session.scalar(select(TgAccountOnlineState).where(
        TgAccountOnlineState.tenant_id == account.tenant_id,
        TgAccountOnlineState.account_id == account.id,
    ))
    if not state:
        return
    state.online_status = "warming"
    state.failure_type = ""
    state.failure_detail = ""
    state.recovery_status = "local_activate_verified"
    state.next_probe_at = timestamp
    state.updated_at = timestamp


def _audit_success(session, operation, case, remote_message_id: str) -> None:
    audit(
        session,
        tenant_id=operation.tenant_id,
        actor=operation.approved_by,
        action="完成 local_activate 新 current 发送读回",
        target_type="tg_authorization_local_activate_case",
        target_id=case.id,
        detail=f"approval_ref={operation.approval_ref}; primary_saved_message_id={remote_message_id}",
    )


def _operation_by_key(session, tenant_id: int, key: str):
    return session.scalar(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.tenant_id == tenant_id,
        TgAuthorizationDrOperation.idempotency_key == key.strip(),
    ))


def _existing_result(session, operation, fingerprint: str) -> dict:
    if operation.request_fingerprint != fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "Verification idempotency key changed")
    case = session.scalar(select(TgAuthorizationLocalActivateCase).where(
        TgAuthorizationLocalActivateCase.verification_operation_id == operation.id,
    ))
    if not case:
        raise AuthorizationDrError("local_activate_case_not_found", "Verification case is unavailable")
    return _result(operation, case)


def _result(operation, case) -> dict:
    return {
        "operation_id": operation.id,
        "case_id": case.id,
        "account_id": operation.account_id,
        "status": operation.status,
        "blocker_code": operation.blocker_code,
        "primary_saved_message_id": case.verification_remote_message_id,
    }


def _require_approval(requested_by: str, approved_by: str, approval_ref: str) -> None:
    if not requested_by.strip() or not approved_by.strip() or not approval_ref.strip():
        raise AuthorizationDrError("approval_ref_required", "Verification approval is required")
    if requested_by.strip() == approved_by.strip():
        raise AuthorizationDrError("approval_actor_conflict", "Approver must differ from requester")


def _fingerprint(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


__all__ = ["apply_local_activate_verification", "preview_local_activate_verification"]
