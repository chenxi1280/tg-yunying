from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from app.models import (
    AccountProxy,
    AccountStatus,
    TelegramDeveloperApp,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationLocalActivateCase,
)
from app.security import decrypt_session, encrypt_secret
from app.services._common import _now, audit, gateway
from app.services.account_authorizations import apply_primary_authorization_switch
from app.services.developer_apps import credentials_for_authorization, credentials_for_developer_app

from .contracts import AuthorizationDrError


def preview_local_activate(session, tenant_id: int, account_id: int, target_id: int, *, actor: str, reason: str):
    account, target = _inputs(session, tenant_id, account_id, target_id)
    identity = _probe_target(session, account, target)
    payload = _fingerprint_payload(account, target, identity, reason)
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    existing = session.scalar(select(TgAuthorizationLocalActivateCase).where(
        TgAuthorizationLocalActivateCase.fingerprint == fingerprint,
    ))
    if existing:
        return existing
    case = _new_case(account, target, identity, fingerprint, actor, reason)
    session.add(case)
    session.commit()
    return case


def apply_local_activate(
    session,
    tenant_id: int,
    account_id: int,
    target_id: int,
    *,
    fingerprint: str,
    actor: str,
    approval_ref: str,
    idempotency_key: str,
):
    case = session.scalar(select(TgAuthorizationLocalActivateCase).where(
        TgAuthorizationLocalActivateCase.fingerprint == fingerprint,
    ).with_for_update())
    if not case or case.tenant_id != tenant_id or case.account_id != account_id or case.target_authorization_id != target_id:
        raise AuthorizationDrError("local_activate_case_not_found", "Local activate preview does not exist")
    if case.status == "applied":
        return _idempotent_case(case, idempotency_key)
    _require_approval(case, actor, approval_ref, idempotency_key)
    _require_apply_key_available(session, case, idempotency_key)
    identity = _probe_target(session, *(_inputs(session, tenant_id, account_id, target_id)))
    account, target = _locked_inputs(session, tenant_id, account_id, target_id)
    _require_frozen(case, account, target, identity)
    _apply_probed_identity(target, identity)
    gateway.invalidate_session_cache(
        account.session_ciphertext,
        _current_credentials(session, account),
    )
    apply_primary_authorization_switch(session, account, target, actor=actor, reason=case.reason)
    _finish_case(case, actor, approval_ref, idempotency_key)
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="应用 SV 本地授权切换",
        target_type="tg_authorization_local_activate_case",
        target_id=case.id,
        detail=f"account_id={account_id}; authorization_id={target_id}; approval_ref={approval_ref}",
    )
    session.commit()
    return case


def local_activate_out(case) -> dict:
    fields = (
        "id", "tenant_id", "account_id", "target_authorization_id", "expected_current_authorization_id",
        "expected_authorization_generation", "expected_fact_generation", "expected_connection_generation",
        "expected_target_fact_version", "fingerprint", "reason", "status", "requested_by", "applied_by",
        "approval_ref", "created_at", "applied_at",
    )
    return {field: getattr(case, field) for field in fields}


def _inputs(session, tenant_id: int, account_id: int, target_id: int):
    account = session.get(TgAccount, account_id)
    target = session.get(TgAccountAuthorization, target_id)
    if not account or account.tenant_id != tenant_id or account.deleted_at is not None:
        raise AuthorizationDrError("account_not_found", "Local activate account does not exist")
    if not target or target.account_id != account.id or target.disabled_at is not None:
        raise AuthorizationDrError("authorization_not_found", "Local activate target does not exist")
    if account.status not in {AccountStatus.SESSION_EXPIRED.value, AccountStatus.NEED_RELOGIN.value}:
        raise AuthorizationDrError("local_activate_primary_failure_unproven", "Primary authorization failure is not confirmed")
    return account, target


def _locked_inputs(session, tenant_id: int, account_id: int, target_id: int):
    account = session.scalar(select(TgAccount).where(TgAccount.id == account_id).with_for_update())
    target = session.scalar(select(TgAccountAuthorization).where(TgAccountAuthorization.id == target_id).with_for_update())
    if not account or account.tenant_id != tenant_id or not target or target.account_id != account_id:
        raise AuthorizationDrError("authorization_version_conflict", "Local activate inputs changed")
    if account.status not in {AccountStatus.SESSION_EXPIRED.value, AccountStatus.NEED_RELOGIN.value}:
        raise AuthorizationDrError("local_activate_primary_failure_unproven", "Primary authorization recovered")
    return account, target


def _probe_target(session, account, target):
    _require_switchable_target(target)
    raw_session = decrypt_session(target.session_ciphertext)
    if not raw_session:
        raise AuthorizationDrError("local_activate_standby_probe_failed", "SV standby_1 material is unavailable")
    identity = gateway.authorization_identity(raw_session, credentials_for_authorization(session, target))
    if target.telegram_user_id_digest and identity.telegram_user_id_digest != target.telegram_user_id_digest:
        raise AuthorizationDrError("authorization_identity_mismatch", "SV standby_1 Telegram identity changed")
    if target.auth_key_fingerprint_digest and identity.auth_key_fingerprint_digest != target.auth_key_fingerprint_digest:
        raise AuthorizationDrError("authorization_identity_mismatch", "SV standby_1 AuthKey changed")
    return identity


def _require_switchable_target(target) -> None:
    valid = (
        target.logical_slot == "standby_1"
        and target.is_slot_current
        and target.provision_region_code == "sv"
        and target.status in {"active", "standby"}
        and target.health_status == "healthy"
        and target.session_ciphertext
        and target.developer_app_id
    )
    if not valid:
        raise AuthorizationDrError("local_activate_standby_probe_failed", "SV standby_1 is not switchable")


def _current_credentials(session, account):
    app = session.get(TelegramDeveloperApp, account.developer_app_id) if account.developer_app_id else None
    proxy = session.get(AccountProxy, account.proxy_id) if account.proxy_id else None
    if not app:
        raise AuthorizationDrError("local_activate_primary_failure_unproven", "Current Developer App is unavailable")
    return credentials_for_developer_app(app, proxy)


def _fingerprint_payload(account, target, identity, reason: str) -> dict:
    return {
        "account": [account.id, account.current_authorization_id, account.authorization_generation,
                    account.authorization_fact_generation, account.connection_generation],
        "target": [target.id, target.fact_version, target.telegram_user_id_digest,
                   identity.auth_key_fingerprint_digest],
        "reason": reason.strip(),
    }


def _new_case(account, target, identity, fingerprint: str, actor: str, reason: str):
    return TgAuthorizationLocalActivateCase(
        tenant_id=account.tenant_id,
        account_id=account.id,
        target_authorization_id=target.id,
        expected_current_authorization_id=account.current_authorization_id,
        expected_authorization_generation=account.authorization_generation,
        expected_fact_generation=account.authorization_fact_generation,
        expected_connection_generation=account.connection_generation,
        expected_target_fact_version=target.fact_version,
        telegram_user_id_digest=identity.telegram_user_id_digest,
        auth_key_fingerprint_digest=identity.auth_key_fingerprint_digest,
        fingerprint=fingerprint,
        reason=reason.strip(),
        requested_by=actor,
    )


def _require_frozen(case, account, target, identity) -> None:
    _require_switchable_target(target)
    observed = (
        account.current_authorization_id,
        account.authorization_generation,
        account.authorization_fact_generation,
        account.connection_generation,
        target.fact_version,
        identity.telegram_user_id_digest,
        identity.auth_key_fingerprint_digest,
    )
    expected = (
        case.expected_current_authorization_id,
        case.expected_authorization_generation,
        case.expected_fact_generation,
        case.expected_connection_generation,
        case.expected_target_fact_version,
        case.telegram_user_id_digest,
        case.auth_key_fingerprint_digest,
    )
    if observed != expected:
        raise AuthorizationDrError("authorization_version_conflict", "Local activate frozen facts changed")


def _apply_probed_identity(target, identity) -> None:
    target.telegram_user_id_digest = identity.telegram_user_id_digest
    target.auth_key_fingerprint_digest = identity.auth_key_fingerprint_digest
    target.telegram_authorization_hash_ciphertext = encrypt_secret(identity.authorization_hash)


def _require_approval(case, actor: str, approval_ref: str, idempotency_key: str) -> None:
    if case.requested_by == actor:
        raise AuthorizationDrError("approval_actor_conflict", "Local activate applier must differ from requester")
    if not approval_ref.strip() or not idempotency_key.strip() or case.status != "decision_ready":
        raise AuthorizationDrError("reconcile_approval_required", "Local activate approval is incomplete")


def _finish_case(case, actor: str, approval_ref: str, idempotency_key: str) -> None:
    case.status = "applied"
    case.applied_by = actor
    case.approval_ref = approval_ref.strip()
    case.apply_idempotency_key = idempotency_key.strip()
    case.applied_at = _now()


def _require_apply_key_available(session, case, idempotency_key: str) -> None:
    existing = session.scalar(select(TgAuthorizationLocalActivateCase.id).where(
        TgAuthorizationLocalActivateCase.tenant_id == case.tenant_id,
        TgAuthorizationLocalActivateCase.apply_idempotency_key == idempotency_key.strip(),
        TgAuthorizationLocalActivateCase.id != case.id,
    ).limit(1))
    if existing:
        raise AuthorizationDrError("reconcile_idempotency_conflict", "Local activate apply key was already used")


def _idempotent_case(case, idempotency_key: str):
    if case.apply_idempotency_key != idempotency_key.strip():
        raise AuthorizationDrError("reconcile_idempotency_conflict", "Local activate apply key changed")
    return case


__all__ = ["apply_local_activate", "local_activate_out", "preview_local_activate"]
