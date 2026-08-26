from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from app.models import (
    Tenant,
    TgAccount,
    TgAccountFullInitialization,
    TgAccountLoginBatchItem,
    TgAccountLoginPostInitializationBinding,
    TgAccountSecuritySnapshot,
)
from app.security import decrypt_secret
from app.services._common import _now, audit, gateway
from app.services.account_two_fa import MANAGED_TWO_FA_HINT, record_managed_two_fa_password
from app.services.code_source_client import CodeSourceClient
from app.services.developer_apps import credentials_for_account
from app.services.tenant_two_fa_settings import tenant_fixed_two_fa_password

from .contracts import FullInitializationClaim


@dataclass(frozen=True)
class TwoFaInputs:
    account_id: int
    session_ciphertext: str
    credentials: object
    fixed_password: str
    current_password: str | None


def execute_two_fa_stage(
    session_factory,
    claim: FullInitializationClaim,
    *,
    code_client: CodeSourceClient,
) -> None:
    try:
        status, inputs = _resolve_inputs(session_factory, claim, code_client)
    except Exception as exc:
        _finish_failure(
            session_factory,
            claim,
            code="two_fa_source_resolution_failed",
            exc=exc,
        )
        return
    if status == "already_proven":
        _finish_success(session_factory, claim, evidence_ref="existing_fixed_evidence")
        return
    if inputs is None:
        return
    _mark_call_started(session_factory, claim)
    try:
        result = gateway.set_two_fa_password(
            inputs.session_ciphertext,
            inputs.fixed_password,
            credentials=inputs.credentials,
            hint=MANAGED_TWO_FA_HINT,
            current_password=inputs.current_password,
        )
    except Exception as exc:
        _finish_unknown(session_factory, claim, exc)
        return
    if result.status in {"email_confirmation_required", "pending_email_confirmation"}:
        _finish_result_failure(session_factory, claim, result)
        return
    if result.ok and result.status == "unchanged":
        _finish_unproven(session_factory, claim, result.detail)
        return
    if not result.ok:
        if result.remote_mutation_started is not False:
            _finish_result_unknown(session_factory, claim, result)
        else:
            _finish_result_failure(session_factory, claim, result)
        return
    evidence_ref = f"full-init:{claim.initialization_id}:two-fa"
    try:
        _finish_success(session_factory, claim, evidence_ref=evidence_ref)
    except Exception as exc:
        _finish_unknown(session_factory, claim, exc)


def _resolve_inputs(session_factory, claim, code_client):
    with session_factory() as session:
        owner = _load_claim(session, claim)
        account = _validated_account(session, owner)
        credentials = credentials_for_account(session, account)
        remote = gateway.get_two_fa_status(account.session_ciphertext, credentials)
        if not remote.ok:
            raise RuntimeError(remote.failure_type or "two_fa_status_unknown")
        if remote.status == "enabled" and _snapshot_proves_fixed(session, account, owner):
            return "already_proven", None
        if remote.status == "email_confirmation_required":
            _mark_manual(owner, "two_fa_email_confirmation_required", remote.detail)
            session.commit()
            return "manual_required", None
        current = _current_password(
            session,
            owner,
            account=account,
            remote_status=remote.status,
            code_client=code_client,
        )
        if remote.status == "enabled" and current is None:
            _mark_manual(owner, "two_fa_current_password_unavailable", "当前 2FA 无可信来源")
            session.commit()
            return "manual_required", None
        fixed = tenant_fixed_two_fa_password(session, tenant_id=owner.tenant_id)
        if not fixed:
            raise RuntimeError("tenant fixed 2FA unavailable")
        return remote.status, TwoFaInputs(
            account.id,
            account.session_ciphertext or "",
            credentials,
            fixed,
            current,
        )


def _current_password(session, owner, *, account, remote_status: str, code_client):
    if remote_status == "missing":
        return None
    accepted = _accepted_source_password(owner)
    if accepted:
        return accepted
    snapshot = session.scalar(
        select(TgAccountSecuritySnapshot).where(
            TgAccountSecuritySnapshot.account_id == account.id
        )
    )
    if snapshot and snapshot.two_fa_password_source == "platform_fixed_confirmed":
        return decrypt_secret(snapshot.two_fa_password_ciphertext)
    return _code_source_candidate(session, owner, code_client)


def _accepted_source_password(owner: TgAccountFullInitialization) -> str | None:
    if owner.source_two_fa_kind not in {"telegram_accepted", "operator_candidate"}:
        return None
    if owner.source_secret_expires_at and owner.source_secret_expires_at <= _now():
        owner.source_two_fa_password_ciphertext = ""
        return None
    return decrypt_secret(owner.source_two_fa_password_ciphertext)


def _code_source_candidate(session, owner, code_client: CodeSourceClient) -> str | None:
    item = session.scalar(
        select(TgAccountLoginBatchItem)
        .join(
            TgAccountLoginPostInitializationBinding,
            TgAccountLoginPostInitializationBinding.login_item_id
            == TgAccountLoginBatchItem.id,
        )
        .where(
            TgAccountLoginPostInitializationBinding.full_initialization_id == owner.id,
            TgAccountLoginPostInitializationBinding.status == "attached",
            TgAccountLoginBatchItem.code_url_ciphertext.is_not(None),
        )
        .order_by(TgAccountLoginBatchItem.id.desc())
        .limit(1)
    )
    if not item or not item.credential_expires_at or item.credential_expires_at <= _now():
        return None
    url = decrypt_secret(item.code_url_ciphertext)
    if not url:
        return None
    password = code_client.fetch_login_materials(url).password_2fa
    return password or None


def _snapshot_proves_fixed(session, account, owner) -> bool:
    snapshot = session.scalar(
        select(TgAccountSecuritySnapshot).where(
            TgAccountSecuritySnapshot.account_id == account.id
        )
    )
    return bool(
        snapshot
        and snapshot.two_fa_password_source == "platform_fixed_confirmed"
        and snapshot.fixed_two_fa_version == owner.fixed_two_fa_version
        and snapshot.two_fa_authorization_generation == owner.authorization_generation
        and snapshot.two_fa_evidence_ref
    )


def snapshot_proves_fixed(session, account, owner) -> bool:
    return _snapshot_proves_fixed(session, account, owner)


def _mark_call_started(session_factory, claim) -> None:
    with session_factory() as session:
        owner = _load_claim(session, claim)
        owner.two_fa_call_state = "started"
        owner.two_fa_request_key = f"full-init:{owner.id}:two-fa:{owner.version}"
        owner.version += 1
        session.commit()


def _finish_success(session_factory, claim, *, evidence_ref: str) -> None:
    with session_factory() as session:
        owner = _load_claim(session, claim)
        evidence_ref = complete_two_fa_success(session, owner, evidence_ref)
        audit(
            session,
            tenant_id=owner.tenant_id,
            actor=owner.execution_owner,
            action="完成批量登录固定 2FA",
            target_type="tg_account_full_initialization",
            target_id=str(owner.id),
            detail=evidence_ref,
        )
        session.commit()


def complete_two_fa_success(session, owner, evidence_ref: str) -> str:
    account = _validated_account(session, owner)
    fixed = tenant_fixed_two_fa_password(session, tenant_id=owner.tenant_id)
    if not fixed:
        raise RuntimeError("tenant fixed 2FA unavailable after mutation")
    evidence_ref = _resolved_evidence_ref(session, account.id, evidence_ref)
    record_managed_two_fa_password(
        session,
        account,
        fixed,
        source="platform_fixed_confirmed",
        fixed_version=owner.fixed_two_fa_version,
        evidence_ref=evidence_ref,
        authorization_generation=owner.authorization_generation,
    )
    owner.two_fa_status = "succeeded"
    owner.two_fa_call_state = "confirmed"
    owner.two_fa_evidence_ref = evidence_ref
    owner.source_two_fa_password_ciphertext = ""
    owner.source_secret_expires_at = None
    owner.stage = "profile"
    owner.status = "pending"
    owner.finished_at = None
    owner.lease_token = ""
    owner.lease_expires_at = None
    owner.version += 1
    _clear_bound_credentials(session, owner.id)
    return evidence_ref


def _resolved_evidence_ref(session, account_id: int, evidence_ref: str) -> str:
    if evidence_ref != "existing_fixed_evidence":
        return evidence_ref
    snapshot = session.scalar(
        select(TgAccountSecuritySnapshot).where(
            TgAccountSecuritySnapshot.account_id == account_id
        )
    )
    return snapshot.two_fa_evidence_ref if snapshot else evidence_ref


def _finish_result_failure(session_factory, claim, result) -> None:
    detail = result.detail or result.failure_type or "Telegram rejected 2FA mutation"
    email_required = result.status in {
        "email_confirmation_required",
        "pending_email_confirmation",
    }
    manual = email_required
    manual = manual or result.failure_type == "two_fa_invalid"
    manual = manual or "password" in detail.lower() or "2fa" in detail.lower()
    with session_factory() as session:
        owner = _load_claim(session, claim)
        if manual:
            code = "two_fa_email_confirmation_required" if email_required else "two_fa_manual_required"
            _mark_manual(owner, code, detail)
            owner.two_fa_call_state = "confirmed"
        else:
            _mark_failed(owner, "two_fa_update_failed", detail)
            owner.two_fa_call_state = "failed"
        session.commit()


def _finish_unproven(session_factory, claim, detail: str) -> None:
    with session_factory() as session:
        owner = _load_claim(session, claim)
        _mark_manual(
            owner,
            "two_fa_remote_effect_unproven",
            detail or "Telegram did not confirm the fixed 2FA mutation",
        )
        owner.two_fa_call_state = "confirmed"
        session.commit()


def _finish_result_unknown(session_factory, claim, result) -> None:
    with session_factory() as session:
        owner = _load_claim(session, claim)
        owner.status = "reconcile_unknown"
        owner.stage = "reconcile_unknown"
        owner.two_fa_status = "reconcile_unknown"
        owner.two_fa_call_state = "unknown"
        owner.failure_type = "two_fa_remote_unknown"
        owner.failure_detail = (result.failure_type or result.detail or "remote_unknown")[:500]
        owner.finished_at = _now()
        owner.lease_token = ""
        owner.lease_expires_at = None
        owner.version += 1
        session.commit()


def _finish_failure(session_factory, claim, *, code: str, exc: Exception) -> None:
    with session_factory() as session:
        owner = _load_claim(session, claim)
        _mark_failed(owner, code, type(exc).__name__)
        session.commit()


def _finish_unknown(session_factory, claim, exc: Exception) -> None:
    with session_factory() as session:
        owner = _load_claim(session, claim)
        owner.status = "reconcile_unknown"
        owner.stage = "reconcile_unknown"
        owner.two_fa_status = "reconcile_unknown"
        owner.two_fa_call_state = "unknown"
        owner.failure_type = "two_fa_remote_unknown"
        owner.failure_detail = type(exc).__name__
        owner.finished_at = _now()
        owner.lease_token = ""
        owner.lease_expires_at = None
        owner.version += 1
        session.commit()


def _mark_manual(owner, code: str, detail: str) -> None:
    owner.status = "manual_required"
    owner.stage = "manual_required"
    owner.two_fa_status = "manual_required"
    owner.failure_type = code
    owner.failure_detail = detail[:500]
    owner.finished_at = _now()
    owner.lease_token = ""
    owner.lease_expires_at = None
    owner.version += 1


def _mark_failed(owner, code: str, detail: str) -> None:
    owner.status = "failed"
    owner.stage = "failed"
    owner.two_fa_status = "failed"
    owner.failure_type = code
    owner.failure_detail = detail[:500]
    owner.finished_at = _now()
    owner.lease_token = ""
    owner.lease_expires_at = None
    owner.version += 1


def _validated_account(session, owner) -> TgAccount:
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
        raise RuntimeError("post-login initialization lifecycle fence changed")
    return account


def _load_claim(session, claim) -> TgAccountFullInitialization:
    owner = session.get(TgAccountFullInitialization, claim.initialization_id)
    if not owner or owner.lease_token != claim.lease_token or owner.stage != claim.stage:
        raise RuntimeError("post-login initialization claim is stale")
    return owner


def _clear_bound_credentials(session, owner_id: int) -> None:
    items = session.scalars(
        select(TgAccountLoginBatchItem)
        .join(
            TgAccountLoginPostInitializationBinding,
            TgAccountLoginPostInitializationBinding.login_item_id
            == TgAccountLoginBatchItem.id,
        )
        .where(
            TgAccountLoginPostInitializationBinding.full_initialization_id == owner_id
        )
    )
    for item in items:
        item.code_url_ciphertext = None


__all__ = ["execute_two_fa_stage"]
