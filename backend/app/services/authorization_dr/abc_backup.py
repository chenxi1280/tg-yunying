from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass

from sqlalchemy import select

from app.models import (
    AccountProxy,
    DeveloperAppSlotAssignment,
    TelegramDeveloperApp,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
)
from app.security import decrypt_session, encrypt_secret
from app.services._common import _now, audit, gateway
from app.services.account_authorizations import (
    start_standby_authorization_login,
    verify_standby_authorization_login,
)
from app.services.account_authorization_metadata import resolve_authorization_identity_hash
from app.services.account_two_fa import managed_two_fa_password
from app.services.developer_apps import credentials_for_authorization
from app.timezone import as_beijing_aware

from .contracts import AuthorizationDrError
from .login_code import bind_login_code
from .primary_fence import require_primary_code_source, verified_code_source


CODE_POLL_SECONDS = 2
TERMINAL_OPERATION_STATUSES = {"succeeded", "failed", "manual_required", "migration_rolled_back_forward"}


@dataclass(frozen=True)
class AbcBackupPreview:
    tenant_id: int
    account_id: int
    primary_authorization_id: int
    primary_fact_version: int
    authorization_generation: int
    authorization_fact_generation: int
    connection_generation: int
    app_b_id: int
    app_b_credentials_version: int
    app_b_assignment_purpose: str
    assignment_version: int
    proxy_id: int
    idempotency_key: str
    fingerprint: str = ""


def preview_abc_backup(
    session,
    tenant_id: int,
    account_id: int,
    *,
    idempotency_key: str,
) -> dict:
    preview = _preview_inputs(session, tenant_id, account_id, idempotency_key)
    payload = asdict(preview)
    payload["fingerprint"] = _fingerprint(payload)
    return payload


def apply_abc_backup(
    session,
    tenant_id: int,
    account_id: int,
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
        if existing.request_fingerprint != expected_fingerprint:
            raise AuthorizationDrError("migration_fingerprint_conflict", "ABC backup idempotency key changed")
        if existing.status == "succeeded":
            return _operation_result(existing)
        raise AuthorizationDrError("authorization_operation_active", f"ABC backup is {existing.status}")
    preview = preview_abc_backup(
        session,
        tenant_id,
        account_id,
        idempotency_key=idempotency_key,
    )
    if preview["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "ABC backup preview changed")
    operation = _get_or_create_operation(session, preview, requested_by, approved_by, approval_ref)
    if operation.status == "succeeded":
        return _operation_result(operation)
    _execute_b_login(session, operation)
    return _operation_result(operation)


def _preview_inputs(session, tenant_id: int, account_id: int, idempotency_key: str) -> AbcBackupPreview:
    if not idempotency_key.strip():
        raise AuthorizationDrError("idempotency_key_required", "ABC backup idempotency key is required")
    account = session.get(TgAccount, account_id)
    if not account or account.tenant_id != tenant_id or account.deleted_at is not None:
        raise AuthorizationDrError("account_not_found", "ABC backup account is unavailable")
    primary = require_primary_code_source(account)
    assignment, app = _sv_backup_assignment(session, primary)
    proxy = session.get(AccountProxy, account.proxy_id) if account.proxy_id else None
    if not proxy or proxy.status not in {"healthy", "available", "normal", "active"}:
        raise AuthorizationDrError("proxy_unavailable", "A SV proxy is unavailable for B login")
    _require_no_healthy_b(session, account_id, primary.developer_app_id)
    _require_no_active_operation(session, account_id)
    return AbcBackupPreview(
        tenant_id=tenant_id,
        account_id=account_id,
        primary_authorization_id=primary.id,
        primary_fact_version=primary.fact_version,
        authorization_generation=account.authorization_generation,
        authorization_fact_generation=account.authorization_fact_generation,
        connection_generation=account.connection_generation,
        app_b_id=app.id,
        app_b_credentials_version=app.credentials_version,
        app_b_assignment_purpose=assignment.slot_purpose,
        assignment_version=assignment.assignment_version,
        proxy_id=proxy.id,
        idempotency_key=idempotency_key.strip(),
    )


def _get_or_create_operation(session, preview: dict, requested_by: str, approved_by: str, approval_ref: str):
    existing = session.scalar(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.tenant_id == preview["tenant_id"],
        TgAuthorizationDrOperation.idempotency_key == preview["idempotency_key"],
    ))
    if existing:
        if existing.request_fingerprint != preview["fingerprint"]:
            raise AuthorizationDrError("migration_fingerprint_conflict", "ABC backup idempotency key changed")
        return existing
    account = session.get(TgAccount, preview["account_id"])
    primary = require_primary_code_source(account)
    operation = _new_b_operation(preview, account, primary, requested_by, approved_by, approval_ref)
    session.add(operation)
    session.flush()
    audit(
        session,
        tenant_id=account.tenant_id,
        actor=approved_by,
        action="批准 A 保护的 SV standby_1 备份",
        target_type="tg_authorization_dr_operation",
        target_id=operation.id,
        detail=f"account_id={account.id}; approval_ref={approval_ref}",
    )
    session.commit()
    return operation


def _new_b_operation(preview, account, primary, requested_by, approved_by, approval_ref):
    return TgAuthorizationDrOperation(
        tenant_id=account.tenant_id,
        account_id=account.id,
        operation_type="provision_standby_1",
        logical_slot="standby_1",
        source_authorization_id=primary.id,
        code_source_authorization_id=primary.id,
        source_generation=primary.slot_generation,
        target_generation=1,
        expected_current_authorization_id=primary.id,
        expected_authorization_generation=preview["authorization_generation"],
        expected_authorization_fact_generation=preview["authorization_fact_generation"],
        expected_connection_generation=preview["connection_generation"],
        expected_code_source_fact_version=primary.fact_version,
        expected_code_source_user_id_digest=primary.telegram_user_id_digest,
        expected_code_source_auth_key_digest=primary.auth_key_fingerprint_digest,
        developer_app_id=preview["app_b_id"],
        developer_app_api_id_snapshot=0,
        developer_app_credentials_version=preview["app_b_credentials_version"],
        assignment_version=preview["assignment_version"],
        egress_id=f"sv-proxy:{preview['proxy_id']}",
        egress_version=1,
        idempotency_key=preview["idempotency_key"],
        request_fingerprint=preview["fingerprint"],
        status="approved",
        requested_by=requested_by,
        approved_by=approved_by,
        approval_ref=approval_ref,
    )


def _execute_b_login(session, operation) -> None:
    source = verified_code_source(session, operation)
    operation.remote_call_state = "started"
    operation.remote_effect_started_at = _now()
    operation.status = "login_remote_started"
    operation.operation_version += 1
    session.commit()
    try:
        flow = start_standby_authorization_login(
            session,
            operation.account_id,
            method="code",
            role="standby_1",
            developer_app_id=operation.developer_app_id,
            proxy_id=_proxy_id(operation),
            actor=operation.approved_by,
            persist_code_preview=False,
        )
    except Exception as exc:
        _mark_unknown(session, operation, exc)
        raise
    operation.login_flow_id = flow.id
    operation.login_challenge_sent_at = as_beijing_aware(flow.challenge_sent_at)
    operation.status = "waiting_login_code"
    operation.operation_version += 1
    session.commit()
    code = _poll_bound_code(session, operation)
    _finish_b_login(session, operation, source, flow, code)


def _poll_bound_code(session, operation) -> str:
    while True:
        operation = session.get(TgAuthorizationDrOperation, operation.id)
        source = verified_code_source(session, operation)
        try:
            snapshots = gateway.poll_verification_codes(
                operation.account_id,
                session_ciphertext=source.session_ciphertext,
                credentials=credentials_for_authorization(session, source),
            )
            bound = bind_login_code(
                snapshots,
                challenge_sent_at=operation.login_challenge_sent_at,
                expected_message_id=operation.login_code_message_id,
            )
        except Exception:
            _mark_failed(session, operation, "verification_code_poll_failed")
            raise
        if bound:
            operation.login_code_message_id = bound.message_id
            operation.login_code_received_at = as_beijing_aware(bound.received_at)
            operation.operation_version += 1
            session.commit()
            return bound.code
        flow = _login_flow(session, operation)
        if not flow.code_expires_at or _now() > flow.code_expires_at:
            _mark_failed(session, operation, "verification_code_unreadable")
            raise AuthorizationDrError("verification_code_unreadable", "Bound A login code expired")
        time.sleep(CODE_POLL_SECONDS)


def _finish_b_login(session, operation, source, flow, code: str) -> None:
    try:
        asset = verify_standby_authorization_login(
            session,
            operation.account_id,
            flow.id,
            flow.flow_version,
            code=code,
            password_2fa=managed_two_fa_password(session, source.account),
            actor=operation.approved_by,
            rotate_two_fa=False,
        )
    except Exception as exc:
        _mark_unknown(session, operation, exc)
        raise
    operation.candidate_authorization_id = asset.id
    operation.status = "qualifying_candidate"
    operation.operation_version += 1
    session.commit()
    try:
        verified_code_source(session, operation)
        identity = gateway.authorization_identity(
            decrypt_session(asset.session_ciphertext),
            credentials_for_authorization(session, asset),
        )
        identity, _hash_source = resolve_authorization_identity_hash(
            session,
            operation.account_id,
            identity,
            exclude_authorization_id=asset.id,
        )
        _retain_conflicting_b(session, asset, source)
        _qualify_b(asset, source, identity)
    except Exception as exc:
        _mark_manual(session, operation, exc)
        raise
    operation.remote_call_state = "succeeded"
    operation.status = "succeeded"
    operation.finished_at = _now()
    operation.operation_version += 1
    session.commit()


def _qualify_b(asset, source, identity) -> None:
    if identity.telegram_user_id_digest != source.telegram_user_id_digest:
        raise AuthorizationDrError("authorization_identity_mismatch", "B belongs to a different account")
    if identity.auth_key_fingerprint_digest == source.auth_key_fingerprint_digest:
        raise AuthorizationDrError("authorization_identity_mismatch", "B duplicates A AuthKey")
    if not identity.authorization_hash or identity.authorization_hash == "0":
        raise AuthorizationDrError("authorization_hash_missing", "B remote authorization hash is missing")
    asset.telegram_user_id_digest = identity.telegram_user_id_digest
    asset.auth_key_fingerprint_digest = identity.auth_key_fingerprint_digest
    asset.telegram_authorization_hash_ciphertext = encrypt_secret(identity.authorization_hash)
    asset.logical_slot = "standby_1"
    asset.role = "standby_1"
    asset.provision_region_code = "sv"
    asset.is_slot_current = True
    asset.is_current = False
    asset.protected_from_cleanup = True
    asset.remote_authorization_state = "active"
    asset.dr_state = "dormant_ready"
    asset.fact_version += 1


def _login_flow(session, operation):
    from app.models import TgLoginFlow

    flow = session.get(TgLoginFlow, operation.login_flow_id)
    if not flow:
        raise AuthorizationDrError("login_flow_missing", "B login flow is missing")
    return flow


def _mark_unknown(session, operation, exc: Exception) -> None:
    session.rollback()
    operation = session.get(TgAuthorizationDrOperation, operation.id)
    operation.status = "reconcile_unknown"
    operation.remote_call_state = "unknown"
    operation.blocker_code = type(exc).__name__[:100]
    operation.operation_version += 1
    session.commit()


def _mark_failed(session, operation, code: str) -> None:
    operation.status = "failed"
    operation.blocker_code = code
    operation.finished_at = _now()
    operation.operation_version += 1
    session.commit()


def _mark_manual(session, operation, exc: Exception) -> None:
    session.rollback()
    operation = session.get(TgAuthorizationDrOperation, operation.id)
    operation.status = "manual_required"
    operation.remote_call_state = "succeeded"
    operation.blocker_code = type(exc).__name__[:100]
    operation.finished_at = _now()
    operation.operation_version += 1
    session.commit()


def _proxy_id(operation) -> int:
    return int(operation.egress_id.split(":", 1)[1])


def _sv_backup_assignment(session, primary):
    c_assignment = session.get(DeveloperAppSlotAssignment, "standby_2_my")
    excluded = {primary.developer_app_id, c_assignment.developer_app_id if c_assignment else None}
    for purpose in ("standby_1_sv", "primary_sv"):
        assignment = session.get(DeveloperAppSlotAssignment, purpose)
        app = session.get(TelegramDeveloperApp, assignment.developer_app_id) if assignment else None
        if assignment and assignment.status == "active" and app and app.is_active and app.id not in excluded:
            return assignment, app
    raise AuthorizationDrError(
        "developer_app_slot_assignment_conflict",
        "No active SV Developer App is distinct from current A and App C",
    )


def _require_no_healthy_b(session, account_id: int, primary_app_id: int) -> None:
    row = session.scalar(select(TgAccountAuthorization.id).where(
        TgAccountAuthorization.account_id == account_id,
        TgAccountAuthorization.logical_slot == "standby_1",
        TgAccountAuthorization.is_slot_current.is_(True),
        TgAccountAuthorization.status.in_({"active", "standby"}),
        TgAccountAuthorization.health_status == "healthy",
        TgAccountAuthorization.developer_app_id != primary_app_id,
        TgAccountAuthorization.disabled_at.is_(None),
    ).limit(1))
    if row:
        raise AuthorizationDrError("sv_redundancy_already_ready", "Account already has healthy B")


def _retain_conflicting_b(session, asset, source) -> None:
    rows = list(session.scalars(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == asset.account_id,
        TgAccountAuthorization.id != asset.id,
        TgAccountAuthorization.logical_slot == "standby_1",
        TgAccountAuthorization.disabled_at.is_(None),
    )))
    for row in rows:
        if row.developer_app_id != source.developer_app_id:
            continue
        row.role = "standby_repair"
        row.logical_slot = "standby_repair"
        row.status = "needs_repair"
        row.health_status = "unknown"
        row.derived_status = "needs_repair"
        row.protected_from_cleanup = True
        row.failure_reason = "Retained after dynamic SV standby replacement"
        row.fact_version += 1


def _require_no_active_operation(session, account_id: int) -> None:
    row = session.scalar(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.account_id == account_id,
        TgAuthorizationDrOperation.status.not_in(TERMINAL_OPERATION_STATUSES),
    ).limit(1))
    if row:
        raise AuthorizationDrError("authorization_operation_active", "Account has an active DR operation")


def _require_approval(requested_by: str, approved_by: str, approval_ref: str) -> None:
    if not requested_by.strip() or not approved_by.strip() or not approval_ref.strip():
        raise AuthorizationDrError("approval_ref_required", "Requester, approver and approval ref are required")
    if requested_by.strip() == approved_by.strip():
        raise AuthorizationDrError("approval_actor_conflict", "Approver must differ from requester")


def _operation_by_key(session, tenant_id: int, idempotency_key: str):
    return session.scalar(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.tenant_id == tenant_id,
        TgAuthorizationDrOperation.idempotency_key == idempotency_key.strip(),
    ))


def _operation_result(operation) -> dict:
    return {
        "operation_id": operation.id,
        "account_id": operation.account_id,
        "status": operation.status,
        "candidate_authorization_id": operation.candidate_authorization_id,
        "blocker_code": operation.blocker_code,
    }


def _fingerprint(payload: dict) -> str:
    body = {key: value for key, value in payload.items() if key != "fingerprint"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


__all__ = ["apply_abc_backup", "preview_abc_backup"]
