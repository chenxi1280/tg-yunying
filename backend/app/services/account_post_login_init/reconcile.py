from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.config import get_settings
from app.models import (
    Tenant,
    TgAccount,
    TgAccountFullInitialization,
    TgAccountSecuritySnapshot,
    TgPostLoginAbcRequest,
)
from app.security import encrypt_secret
from app.services._common import _now, audit, gateway
from app.services.developer_apps import credentials_for_account

from .contracts import FullInitializationClaim
from .parent import sync_parent_bindings
from .two_fa import (
    complete_two_fa_success,
    continue_after_two_fa_reset,
    record_two_fa_reset_waiting,
    snapshot_proves_fixed,
)


CANDIDATE_ALLOWED_FAILURES = {
    "two_fa_current_password_unavailable",
    "two_fa_remote_confirmed_no_effect",
    "two_fa_remote_effect_unproven",
    "two_fa_manual_required",
}
RESET_ALLOWED_FAILURES = {
    "two_fa_current_password_unavailable",
    "two_fa_manual_required",
}
SAFE_TWO_FA_RECHECK_FAILURES = {"two_fa_source_resolution_failed"}
RECHECKABLE_STAGE_STATUSES = {"failed", "manual_required"}


def request_post_login_reconciliation(
    session,
    tenant_id: int,
    initialization_id: int,
    *,
    expected_version: int,
    actor: str,
    reason: str,
):
    owner = _locked_owner(
        session, tenant_id, initialization_id, expected_version=expected_version,
    )
    stage = _recheck_stage(owner)
    if stage is None:
        raise ValueError("post-login initialization has no safe recheck action")
    _reopen(owner, stage)
    _audit_action(
        session, owner, actor=actor, action="请求批量登录后置结果对账", reason=reason,
    )
    sync_parent_bindings(session, owner)
    session.commit()
    return owner


def submit_two_fa_candidate(
    session,
    tenant_id: int,
    initialization_id: int,
    *,
    expected_version: int,
    actor: str,
    reason: str,
    candidate_password: str,
):
    owner = _locked_owner(
        session, tenant_id, initialization_id, expected_version=expected_version,
    )
    allowed = owner.status == "manual_required" and owner.failure_type in CANDIDATE_ALLOWED_FAILURES
    if not allowed:
        raise ValueError("current 2FA candidate is not allowed for this operation")
    if not candidate_password:
        raise ValueError("current 2FA candidate is required")
    owner.source_two_fa_kind = "operator_candidate"
    owner.source_two_fa_password_ciphertext = encrypt_secret(candidate_password)
    ttl = get_settings().account_post_login_init_secret_ttl_seconds
    owner.source_secret_expires_at = _now() + timedelta(seconds=ttl)
    owner.two_fa_status = "pending"
    owner.two_fa_call_state = "none"
    _reopen(owner, "two_fa")
    _audit_action(
        session, owner, actor=actor, action="提交批量登录当前2FA候选", reason=reason,
    )
    sync_parent_bindings(session, owner)
    session.commit()
    return owner


def request_two_fa_reset(
    session,
    tenant_id: int,
    initialization_id: int,
    *,
    expected_version: int,
    actor: str,
    reason: str,
):
    owner = _locked_owner(
        session, tenant_id, initialization_id, expected_version=expected_version,
    )
    allowed = owner.status == "manual_required" and owner.failure_type in RESET_ALLOWED_FAILURES
    if not allowed:
        raise ValueError("2FA reset is not allowed for this operation")
    owner.source_two_fa_kind = "telegram_reset_requested"
    owner.source_two_fa_password_ciphertext = ""
    owner.source_secret_expires_at = None
    owner.two_fa_status = "pending"
    owner.two_fa_call_state = "none"
    _reopen(owner, "two_fa")
    _audit_action(
        session, owner, actor=actor, action="请求批量登录2FA重置", reason=reason,
    )
    sync_parent_bindings(session, owner)
    session.commit()
    return owner


def confirm_two_fa_email(
    session,
    tenant_id: int,
    initialization_id: int,
    *,
    expected_version: int,
    actor: str,
    reason: str,
    confirmation_code: str,
):
    owner = _locked_owner(
        session, tenant_id, initialization_id, expected_version=expected_version,
    )
    if owner.status != "manual_required" or "email" not in owner.failure_type:
        raise ValueError("2FA email confirmation is not required")
    account = _account(session, owner)
    credentials = credentials_for_account(session, account)
    owner.two_fa_call_state = "started"
    owner.version += 1
    session.commit()
    try:
        result = gateway.confirm_two_fa_email(
            account.session_ciphertext,
            confirmation_code,
            credentials,
        )
        result = _confirmed_email_readback(account, credentials, result)
        _finish_email_result(session, owner, result)
        _audit_action(
            session, owner, actor=actor, action="确认批量登录2FA恢复邮箱", reason=reason,
        )
        sync_parent_bindings(session, owner)
        session.commit()
        return owner
    except Exception as exc:
        session.rollback()
        return _persist_email_unknown(
            session,
            initialization_id,
            actor=actor,
            reason=reason,
            exc=exc,
        )


def _persist_email_unknown(
    session,
    initialization_id: int,
    *,
    actor: str,
    reason: str,
    exc: Exception,
):
    owner = session.scalar(
        select(TgAccountFullInitialization).where(
            TgAccountFullInitialization.id == initialization_id,
        ).with_for_update()
    )
    if not owner:
        raise ValueError("post-login initialization not found")
    _mark_unknown(owner, type(exc).__name__)
    _audit_action(
        session, owner, actor=actor, action="确认批量登录2FA恢复邮箱待对账", reason=reason,
    )
    sync_parent_bindings(session, owner)
    session.commit()
    return owner


def assume_execution_owner(
    session,
    tenant_id: int,
    initialization_id: int,
    *,
    expected_version: int,
    actor: str,
    reason: str,
):
    owner = _locked_owner(
        session, tenant_id, initialization_id, expected_version=expected_version,
    )
    request = _abc_request(session, owner.id)
    if request and request.status in {"approved", "running"}:
        raise ValueError("materialized ABC execution cannot change owner")
    owner.execution_owner = actor
    owner.version += 1
    if request:
        _reset_approval(request, actor)
    _audit_action(
        session, owner, actor=actor, action="接管批量登录后置初始化", reason=reason,
    )
    session.commit()
    return owner


def execute_reconcile_stage(session_factory, claim: FullInitializationClaim) -> None:
    with session_factory() as session:
        owner = _load_claim(session, claim)
        if owner.two_fa_status == "reconcile_unknown":
            _reconcile_two_fa(session, owner)
        elif owner.profile_status == "reconcile_unknown":
            _reopen(owner, "profile")
            owner.profile_status = "readback_retry"
        elif owner.abc_status == "reconcile_unknown":
            _reopen(owner, "abc")
        else:
            _mark_manual(owner, "post_init_reconcile_target_unknown")
        sync_parent_bindings(session, owner)
        session.commit()


def _reconcile_two_fa(session, owner) -> None:
    try:
        account = _account(session, owner)
        result = gateway.get_two_fa_status(
            account.session_ciphertext,
            credentials_for_account(session, account),
        )
    except Exception as exc:
        _mark_unknown(owner, type(exc).__name__)
        return
    if not result.ok:
        _mark_unknown(owner, result.failure_type or result.detail or "remote_unknown")
        return
    if result.status == "reset_waiting" and result.next_retry_at:
        record_two_fa_reset_waiting(session, owner, result.next_retry_at)
        return
    if result.status == "missing" and owner.source_two_fa_kind == "telegram_reset_requested":
        continue_after_two_fa_reset(session, owner)
        return
    if result.status == "enabled" and snapshot_proves_fixed(session, account, owner):
        snapshot = _snapshot(session, account.id)
        complete_two_fa_success(session, owner, snapshot.two_fa_evidence_ref)
        return
    if result.status == "missing":
        _mark_manual(owner, "two_fa_remote_confirmed_no_effect")
        return
    if result.status == "email_confirmation_required":
        _mark_manual(owner, "two_fa_email_confirmation_required")
        return
    _mark_manual(owner, "two_fa_remote_effect_unproven")


def _finish_email_result(session, owner, result) -> None:
    if result.ok and result.status == "enabled":
        complete_two_fa_success(session, owner, f"full-init:{owner.id}:two-fa-email")
        return
    if result.ok and result.status == "missing":
        _mark_manual(owner, "two_fa_remote_confirmed_no_effect")
        return
    if result.ok and result.status == "email_confirmation_required":
        _mark_manual(owner, "two_fa_email_confirmation_required")
        return
    code = result.failure_type or "two_fa_remote_effect_unproven"
    _mark_manual(owner, code)
    owner.failure_detail = (result.detail or owner.failure_type)[:500]
    owner.two_fa_call_state = "failed"


def _confirmed_email_readback(account, credentials, result):
    if not result.ok:
        return result
    readback = gateway.get_two_fa_status(account.session_ciphertext, credentials)
    if not readback.ok:
        raise RuntimeError(readback.failure_type or "two_fa_email_readback_unknown")
    return readback


def _recheck_stage(owner) -> str | None:
    if owner.status == "reconcile_unknown":
        return "reconcile"
    if owner.status not in RECHECKABLE_STAGE_STATUSES:
        return None
    if owner.failure_type in SAFE_TWO_FA_RECHECK_FAILURES:
        return "two_fa"
    if owner.profile_status in RECHECKABLE_STAGE_STATUSES:
        return "profile"
    if owner.abc_status in RECHECKABLE_STAGE_STATUSES | {"reconcile_unknown"}:
        return "abc"
    return None


def _locked_owner(session, tenant_id, initialization_id, *, expected_version):
    owner = session.scalar(
        select(TgAccountFullInitialization).where(
            TgAccountFullInitialization.id == initialization_id,
        ).with_for_update()
    )
    if not owner or owner.tenant_id != tenant_id:
        raise ValueError("post-login initialization not found")
    if owner.version != expected_version:
        raise ValueError("post-login initialization version changed")
    return owner


def _load_claim(session, claim):
    owner = session.get(TgAccountFullInitialization, claim.initialization_id)
    if not owner or owner.lease_token != claim.lease_token or owner.stage != claim.stage:
        raise RuntimeError("post-login reconciliation claim is stale")
    return owner


def _account(session, owner):
    account = session.get(TgAccount, owner.account_id)
    tenant = session.get(Tenant, owner.tenant_id)
    valid = bool(
        account
        and tenant
        and account.tenant_id == owner.tenant_id
        and account.deleted_at is None
        and account.account_identity == "normal"
        and account.authorization_generation == owner.authorization_generation
        and tenant.fixed_two_fa_password_version == owner.fixed_two_fa_version
    )
    if not valid:
        raise ValueError("post-login initialization account is unavailable")
    return account


def _snapshot(session, account_id):
    return session.scalar(
        select(TgAccountSecuritySnapshot).where(
            TgAccountSecuritySnapshot.account_id == account_id,
        )
    )


def _abc_request(session, owner_id):
    return session.scalar(
        select(TgPostLoginAbcRequest).where(
            TgPostLoginAbcRequest.full_initialization_id == owner_id,
        )
    )


def _reopen(owner, stage: str) -> None:
    owner.status = "pending"
    owner.stage = stage
    owner.failure_type = ""
    owner.failure_detail = ""
    owner.finished_at = None
    owner.next_retry_at = _now()
    owner.lease_token = ""
    owner.lease_expires_at = None
    owner.version += 1


def _mark_manual(owner, code: str) -> None:
    owner.status = owner.stage = "manual_required"
    owner.two_fa_status = "manual_required"
    owner.two_fa_call_state = "confirmed"
    owner.failure_type = code
    owner.failure_detail = code
    owner.finished_at = _now()
    owner.lease_token = ""
    owner.lease_expires_at = None
    owner.version += 1


def _mark_unknown(owner, detail: str) -> None:
    owner.status = owner.stage = "reconcile_unknown"
    owner.two_fa_status = "reconcile_unknown"
    owner.two_fa_call_state = "unknown"
    owner.failure_type = "two_fa_remote_unknown"
    owner.failure_detail = detail[:500]
    owner.finished_at = _now()
    owner.lease_token = ""
    owner.lease_expires_at = None
    owner.version += 1


def _reset_approval(request, actor: str) -> None:
    request.requested_by = actor
    request.approved_by = ""
    request.approval_ref = ""
    request.deployed_release_sha = ""
    request.preview_fingerprint = ""
    request.request_version += 1


def _audit_action(session, owner, *, actor, action, reason) -> None:
    audit(
        session,
        tenant_id=owner.tenant_id,
        actor=actor,
        action=action,
        target_type="tg_account_full_initialization",
        target_id=str(owner.id),
        detail=reason[:255],
    )


__all__ = [
    "assume_execution_owner",
    "confirm_two_fa_email",
    "execute_reconcile_stage",
    "request_post_login_reconciliation",
    "request_two_fa_reset",
    "submit_two_fa_candidate",
]
