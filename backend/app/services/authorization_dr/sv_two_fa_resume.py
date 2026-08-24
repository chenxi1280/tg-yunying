from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import func, select
from telethon.errors import PasswordHashInvalidError

from app.models import (
    AccountStatus,
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
    TgAccountSecuritySnapshot,
    TgAuthorizationDrOperation,
    TgAuthorizationDrReconcileCase,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgAuthorizationOnlineAbcSlotResult,
    TgLoginFlow,
)
from app.integrations.telegram.authorization_fingerprint import authorization_fingerprint_digest
from app.security import decrypt_secret
from app.services._common import gateway
from app.services.account_authorization_metadata import resolve_authorization_identity_hash
from app.services.developer_apps import credentials_for_authorization, credentials_for_developer_app

from .contracts import AuthorizationDrError
from .online_abc_operations import online_abc_item_operations
from .primary_fence import verified_code_source
from .sv_login_recovery import (
    _egress_matches_flow,
    _operation_proxy,
    _remote_set_digest,
    _standby_target_slot,
)
from .sv_two_fa_resume_commit import (
    INVALID_CLASSIFICATION,
    RECOVERY_CLASSIFICATION,
    close_invalid,
    close_success,
    idempotent_result,
    persist_candidate,
    result,
)


UNAUTHORIZED_MESSAGE = "session is not authorized"
UNKNOWN_STATUSES = {"provision_reconcile_unknown", "reconcile_unknown"}
RESUMABLE_BLOCKERS = {"PasswordHashInvalidError", "ValueError"}
ACTIVE_STATUSES = {
    "pending", "waiting_login", "login_remote_started", "bundle_copies_verified",
    "ready_for_slot_commit", "slot_commit_prepared", "running", "approved",
}


@dataclass(frozen=True)
class ResumeContext:
    operation: TgAuthorizationDrOperation
    account: TgAccount
    primary: TgAccountAuthorization
    flow: TgLoginFlow
    app: TelegramDeveloperApp
    proxy: object | None
    security: TgAccountSecuritySnapshot
    tenant: Tenant
    batch: TgAuthorizationOnlineAbcBatch
    item: TgAuthorizationOnlineAbcItem
    slot: TgAuthorizationOnlineAbcSlotResult
    runtime_image_sha: str
    requested_by: str


def preview_sv_two_fa_resume(
    session, operation_id: str, *, tenant_id: int, runtime_image_sha: str, requested_by: str,
) -> dict:
    context = _load_context(
        session, operation_id, tenant_id=tenant_id, runtime_sha=runtime_image_sha, requested_by=requested_by,
    )
    payload = _evidence_payload(context)
    return {**payload, "evidence_fingerprint": _fingerprint(payload)}


def apply_sv_two_fa_resume(
    session, operation_id: str, *, tenant_id: int, runtime_image_sha: str,
    requested_by: str, actor: str, approval_ref: str, idempotency_key: str,
    expected_fingerprint: str,
) -> dict:
    _require_approval(
        requested_by=requested_by, actor=actor, approval_ref=approval_ref, key=idempotency_key,
    )
    existing = idempotent_result(
        session, operation_id, tenant_id=tenant_id, requested_by=requested_by, actor=actor,
        key=idempotency_key, fingerprint=expected_fingerprint,
    )
    if existing:
        return existing
    context = _lock_context(
        session, operation_id, tenant_id=tenant_id, runtime_sha=runtime_image_sha, requested_by=requested_by,
    )
    _require_batch_approval(context, actor)
    payload = _evidence_payload(context)
    fingerprint = _fingerprint(payload)
    if fingerprint != expected_fingerprint:
        raise AuthorizationDrError("reconcile_evidence_conflict", "SV 2FA resume evidence changed")
    try:
        remote = _recover_remote_session(session, context)
    except PasswordHashInvalidError:
        close_invalid(
            session, context, payload=payload, fingerprint=fingerprint, actor=actor,
            approval_ref=approval_ref, key=idempotency_key,
        )
        session.commit()
        return result(context.operation, fingerprint, INVALID_CLASSIFICATION)
    asset = persist_candidate(session, context, remote, actor=actor)
    close_success(
        session, context, asset, payload=payload, remote=remote, fingerprint=fingerprint,
        actor=actor, approval_ref=approval_ref, key=idempotency_key,
    )
    session.commit()
    return result(context.operation, fingerprint, RECOVERY_CLASSIFICATION)


def readback_sv_two_fa_resume(session, operation_id: str, tenant_id: int) -> dict:
    operation = session.get(TgAuthorizationDrOperation, operation_id)
    if not operation or operation.tenant_id != tenant_id:
        raise AuthorizationDrError("migration_operation_not_found", "SV 2FA operation does not exist")
    case = session.scalar(select(TgAuthorizationDrReconcileCase).where(
        TgAuthorizationDrReconcileCase.operation_id == operation.id,
    ))
    return {
        "operation_id": operation.id,
        "account_id": operation.account_id,
        "operation_status": operation.status,
        "operation_version": operation.operation_version,
        "candidate_authorization_id": operation.candidate_authorization_id,
        "remote_call_state": operation.remote_call_state,
        "reconcile_status": operation.reconcile_status,
        "case_status": case.status if case else "missing",
        "classification": case.classification if case else "",
        "evidence_fingerprint": case.evidence_fingerprint if case else "",
    }


def _load_context(session, operation_id, *, tenant_id, runtime_sha, requested_by) -> ResumeContext:
    operation = session.get(TgAuthorizationDrOperation, operation_id)
    if not operation or operation.tenant_id != tenant_id:
        raise AuthorizationDrError("migration_operation_not_found", "SV 2FA operation does not exist")
    account = session.get(TgAccount, operation.account_id)
    primary = verified_code_source(session, operation, allow_unpersisted_identity=True)
    flow = session.get(TgLoginFlow, operation.login_flow_id)
    app = session.get(TelegramDeveloperApp, operation.developer_app_id)
    proxy = _operation_proxy(session, operation)
    security = session.scalar(select(TgAccountSecuritySnapshot).where(
        TgAccountSecuritySnapshot.account_id == operation.account_id,
    ))
    tenant = session.get(Tenant, tenant_id)
    slot = session.scalar(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.operation_id == operation.id,
    ))
    item = session.get(TgAuthorizationOnlineAbcItem, slot.item_id) if slot else None
    batch = session.get(TgAuthorizationOnlineAbcBatch, item.batch_id) if item else None
    context = ResumeContext(
        operation, account, primary, flow, app, proxy, security, tenant, batch, item, slot,
        runtime_sha.strip(), requested_by.strip(),
    )
    _require_context(session, context)
    return context


def _lock_context(session, operation_id, *, tenant_id, runtime_sha, requested_by) -> ResumeContext:
    session.expire_all()
    operation = session.scalar(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.id == operation_id,
    ).with_for_update())
    if not operation or operation.tenant_id != tenant_id:
        raise AuthorizationDrError("migration_operation_not_found", "SV 2FA operation does not exist")
    for model, row_id in _lock_targets(session, operation):
        session.scalar(select(model).where(model.id == row_id).with_for_update())
    return _load_context(
        session, operation_id, tenant_id=tenant_id, runtime_sha=runtime_sha, requested_by=requested_by,
    )


def _lock_targets(session, operation) -> list[tuple[object, object]]:
    slot = session.scalar(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.operation_id == operation.id,
    ))
    item = session.get(TgAuthorizationOnlineAbcItem, slot.item_id) if slot else None
    batch = session.get(TgAuthorizationOnlineAbcBatch, item.batch_id) if item else None
    security = session.scalar(select(TgAccountSecuritySnapshot).where(
        TgAccountSecuritySnapshot.account_id == operation.account_id,
    ))
    rows = [
        (TgAccount, operation.account_id),
        (TgAccountAuthorization, operation.code_source_authorization_id),
        (TgLoginFlow, operation.login_flow_id),
        (Tenant, operation.tenant_id),
    ]
    rows.extend((model, row.id) for model, row in [
        (TgAccountSecuritySnapshot, security), (TgAuthorizationOnlineAbcSlotResult, slot),
        (TgAuthorizationOnlineAbcItem, item), (TgAuthorizationOnlineAbcBatch, batch),
    ] if row)
    return rows


def _require_context(session, context: ResumeContext) -> None:
    operation, flow = context.operation, context.flow
    contract = session.get(AuthorizationDrRuntimeContract, 1)
    active_clients = session.scalar(select(func.sum(AuthorizationDrExecutionNode.active_client_count))) or 0
    valid = operation.operation_type == "provision_standby_1"
    valid = valid and operation.status == "reconcile_unknown"
    valid = valid and operation.blocker_code in RESUMABLE_BLOCKERS
    valid = valid and operation.remote_call_state == "unknown" and not operation.candidate_authorization_id
    valid = valid and not operation.owner_node_id and not operation.lease_token
    valid = valid and operation.lease_expires_at is None and _primary_safe(context)
    valid = valid and flow and flow.status in {AccountStatus.WAITING_CODE.value, AccountStatus.WAITING_2FA.value}
    valid = valid and flow.temporary_session_ciphertext and flow.phone_code_hash_ciphertext
    valid = valid and context.app and context.app.is_active and _egress_matches_flow(operation, flow, context.proxy)
    valid = valid and contract and contract.mode == "off" and not contract.claim_scope_operation_id
    valid = valid and active_clients == 0 and context.runtime_image_sha and context.requested_by
    valid = valid and _operation_boundary_frozen(session, operation.id)
    valid = valid and _online_state_frozen(session, context) and _security_state_frozen(context)
    if not valid or _healthy_target_exists(session, context):
        raise AuthorizationDrError("reconcile_transition_blocked", "SV 2FA resume state is not frozen")


def _primary_safe(context: ResumeContext) -> bool:
    primary, account = context.primary, context.account
    health_allowed = primary.health_status == "healthy"
    if context.operation.blocker_code == "ValueError":
        health_allowed = primary.health_status == "legacy" and _legacy_waiting_two_fa(context)
    return bool(
        account.status == AccountStatus.ACTIVE.value
        and primary.status == "active" and health_allowed
        and primary.is_slot_current and primary.protected_from_cleanup
        and not primary.last_authoritative_error_code and primary.disabled_at is None
    )


def _legacy_waiting_two_fa(context: ResumeContext) -> bool:
    security = context.security
    return bool(
        context.flow and context.flow.status == AccountStatus.WAITING_2FA.value
        and context.operation.login_challenge_sent_at and context.operation.login_code_message_id
        and context.operation.login_code_received_at
        and security and security.trusted_session_status == "confirmed"
        and security.two_fa_status == "enabled" and not security.two_fa_password_ciphertext
        and not security.two_fa_password_stored_at
        and context.tenant and context.tenant.fixed_two_fa_password_ciphertext
        and context.tenant.fixed_two_fa_password_set_at
    )


def _operation_boundary_frozen(session, operation_id: str) -> bool:
    unknown_ids = list(session.scalars(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.status.in_(UNKNOWN_STATUSES),
    ).limit(2)))
    other_active = session.scalar(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.id != operation_id,
        TgAuthorizationDrOperation.status.in_(ACTIVE_STATUSES),
    ).limit(1))
    return unknown_ids == [operation_id] and other_active is None


def _online_state_frozen(session, context: ResumeContext) -> bool:
    if not context.batch or not context.item:
        return False
    operations = online_abc_item_operations(session, context.batch, context.item)
    return bool(
        context.batch and context.batch.status == "stopped"
        and context.item and context.item.status == "stopped" and context.item.outcome == "reconcile_unknown"
        and _item_primary_frozen(context)
        and context.slot and context.slot.logical_slot == "standby_1"
        and context.slot.outcome == "reconcile_unknown"
        and context.slot.operation_id == context.operation.id
        and operations == {"b": context.operation, "c": None, "e4": None}
    )


def _item_primary_frozen(context: ResumeContext) -> bool:
    item, account, primary = context.item, context.account, context.primary
    return bool(
        item.account_id == account.id
        and item.primary_authorization_id == primary.id
        and item.primary_fact_version == primary.fact_version
        and item.primary_session_digest == _digest(primary.session_ciphertext or "")
        and item.authorization_generation == account.authorization_generation
        and item.authorization_fact_generation == account.authorization_fact_generation
        and item.connection_generation == account.connection_generation
        and item.app_b_id == context.operation.developer_app_id
    )


def _security_state_frozen(context: ResumeContext) -> bool:
    if context.operation.blocker_code == "ValueError":
        return _legacy_waiting_two_fa(context)
    security, tenant = context.security, context.tenant
    return bool(
        security and security.trusted_session_status == "confirmed"
        and security.two_fa_status == "enabled" and security.two_fa_password_ciphertext
        and security.two_fa_password_stored_at and tenant and tenant.fixed_two_fa_password_ciphertext
        and tenant.fixed_two_fa_password_set_at
        and tenant.fixed_two_fa_password_set_at > security.two_fa_password_stored_at
        and _managed_and_fixed_passwords_differ(context)
    )


def _managed_and_fixed_passwords_differ(context: ResumeContext) -> bool:
    managed = decrypt_secret(context.security.two_fa_password_ciphertext)
    fixed = decrypt_secret(context.tenant.fixed_two_fa_password_ciphertext)
    return bool(managed and fixed and managed != fixed)


def _healthy_target_exists(session, context: ResumeContext) -> bool:
    target_slot = _standby_target_slot(context.primary)
    return bool(session.scalar(select(TgAccountAuthorization.id).where(
        TgAccountAuthorization.account_id == context.account.id,
        TgAccountAuthorization.id != context.primary.id,
        TgAccountAuthorization.logical_slot == target_slot,
        TgAccountAuthorization.is_slot_current.is_(True),
        TgAccountAuthorization.status.in_({"active", "standby"}),
        TgAccountAuthorization.health_status == "healthy",
        TgAccountAuthorization.disabled_at.is_(None),
    ).limit(1)))


def _evidence_payload(context: ResumeContext) -> dict:
    account, primary, flow = context.account, context.primary, context.flow
    managed_secret = context.security.two_fa_password_ciphertext or ""
    return {
        "operation_id": context.operation.id,
        "account_id": account.id,
        "operation_version": context.operation.operation_version,
        "operation_blocker_code": context.operation.blocker_code,
        "login_challenge_sent_at": str(context.operation.login_challenge_sent_at),
        "login_code_message_id": context.operation.login_code_message_id,
        "login_code_received_at": str(context.operation.login_code_received_at),
        "flow_id": flow.id,
        "flow_version": flow.flow_version,
        "flow_status": flow.status,
        "primary_authorization_id": primary.id,
        "primary_fact_version": primary.fact_version,
        "primary_session_digest": _digest(primary.session_ciphertext or ""),
        "expected_primary_user_digest": context.operation.expected_code_source_user_id_digest,
        "expected_primary_authkey_digest": context.operation.expected_code_source_auth_key_digest,
        "account_generations": [account.authorization_generation, account.authorization_fact_generation,
                                account.connection_generation],
        "developer_app_id": context.app.id,
        "proxy_id": context.proxy.id if context.proxy else None,
        "planned_proxy_id": context.item.proxy_id,
        "operation_egress_id": context.operation.egress_id,
        "operation_egress_version": context.operation.egress_version,
        "target_logical_slot": _standby_target_slot(primary),
        "temporary_session_digest": _digest(flow.temporary_session_ciphertext),
        "phone_code_hash_digest": _digest(flow.phone_code_hash_ciphertext),
        "managed_secret_ref_digest": _digest(managed_secret),
        "managed_secret_stored_at": str(context.security.two_fa_password_stored_at or ""),
        "fixed_secret_ref_digest": _digest(context.tenant.fixed_two_fa_password_ciphertext),
        "fixed_secret_set_at": str(context.tenant.fixed_two_fa_password_set_at),
        "batch_id": context.batch.id,
        "batch_version": context.batch.version,
        "batch_requested_by": context.batch.requested_by,
        "batch_approved_by": context.batch.approved_by,
        "item_id": context.item.id,
        "item_version": context.item.version,
        "slot_result_id": context.slot.id,
        "slot_version": context.slot.version,
        "runtime_image_sha": context.runtime_image_sha,
        "requested_by": context.requested_by,
    }


def _recover_remote_session(session, context: ResumeContext) -> dict:
    raw_session = decrypt_secret(context.flow.temporary_session_ciphertext)
    credentials = credentials_for_developer_app(context.app, context.proxy)
    try:
        identity = gateway.authorization_identity(raw_session, credentials)
    except RuntimeError as exc:
        if str(exc) != UNAUTHORIZED_MESSAGE:
            raise
        raw_session = _submit_fixed_password(context, credentials, raw_session)
        identity = gateway.authorization_identity(raw_session, credentials)
    identity, hash_source = resolve_authorization_identity_hash(session, context.account.id, identity)
    _require_remote_identity(context, identity)
    remote = gateway.list_authorizations(
        context.primary.session_ciphertext, credentials_for_authorization(session, context.primary),
    )
    candidate = _unique_recovered_device(remote, identity, context.app)
    return {
        "raw_session": raw_session,
        "identity": identity,
        "hash_source": hash_source,
        "remote_set_digest": _remote_set_digest(remote),
        "remote_device_count": len(remote),
        "candidate_hash_digest": _digest(candidate.authorization_hash),
        "candidate_fingerprint_digest": authorization_fingerprint_digest(candidate),
        "fixed_password": decrypt_secret(context.tenant.fixed_two_fa_password_ciphertext),
    }


def _unique_recovered_device(remote, identity, app):
    expected_hash = str(identity.authorization_hash).strip()
    expected_fingerprint = identity.authorization_fingerprint_digest
    matches = [
        row for row in remote
        if str(row.authorization_hash).strip() == expected_hash
        and row.api_id == app.api_id
        and not row.is_current
        and authorization_fingerprint_digest(row) == expected_fingerprint
    ]
    if len(matches) != 1:
        raise AuthorizationDrError("reconcile_evidence_conflict", "Recovered B device is not unique")
    return matches[0]


def _submit_fixed_password(context: ResumeContext, credentials, raw_session: str) -> str:
    fixed_password = decrypt_secret(context.tenant.fixed_two_fa_password_ciphertext)
    status, resumed_session = gateway.finish_login(
        None, fixed_password, flow_id=context.flow.id, account_id=context.account.id,
        phone=context.account.phone_number, credentials=credentials, temporary_session=raw_session,
        phone_code_hash=decrypt_secret(context.flow.phone_code_hash_ciphertext),
    )
    if status != AccountStatus.ACTIVE.value or not resumed_session:
        raise RuntimeError("SV password-only login did not return an authorized Session")
    return resumed_session


def _require_remote_identity(context: ResumeContext, identity) -> None:
    expected_user = context.operation.expected_code_source_user_id_digest
    expected_authkey = context.operation.expected_code_source_auth_key_digest
    if identity.telegram_user_id_digest != expected_user:
        raise AuthorizationDrError("authorization_identity_mismatch", "Recovered B belongs to another account")
    if identity.auth_key_fingerprint_digest == expected_authkey:
        raise AuthorizationDrError("authorization_identity_mismatch", "Recovered B duplicates A AuthKey")
    if not identity.authorization_hash or identity.authorization_hash == "0":
        raise AuthorizationDrError("authorization_hash_missing", "Recovered B authorization hash is missing")


def _require_batch_approval(context: ResumeContext, actor: str) -> None:
    valid = context.requested_by == context.batch.requested_by
    valid = valid and actor.strip() == context.batch.approved_by
    if not valid:
        raise AuthorizationDrError("reconcile_approval_required", "SV 2FA resume must reuse batch actors")


def _require_approval(*, requested_by, actor, approval_ref, key) -> None:
    identities = [requested_by.strip(), actor.strip()]
    if not all(identities) or identities[0] == identities[1] or not approval_ref.strip() or not key.strip():
        raise AuthorizationDrError("reconcile_approval_required", "SV 2FA resume approval is incomplete")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


__all__ = ["apply_sv_two_fa_resume", "preview_sv_two_fa_resume", "readback_sv_two_fa_resume"]
