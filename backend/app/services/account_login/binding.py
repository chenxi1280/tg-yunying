from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountPool,
    TgAccount,
    TgAccountLoginBatchItem,
    TgAccountPhoneFingerprintAlias,
)
from app.services.account_phone_aliases import (
    PhoneAliasConflict,
    accepted_phone_fingerprint_versions,
    account_for_phone_fingerprints,
    ensure_phone_aliases_for_account,
    insert_phone_aliases,
    lock_phone_fingerprints,
    missing_phone_aliases,
    phone_fingerprints,
)
from app.security import decrypt_secret, encrypt_secret
from app.services._common import _now, audit
from app.services.account_group_revision_snapshot import lock_membership_tenant
from app.services.account_group_revisions import begin_membership_change, finish_membership_change
from app.services.dedicated_account_pools import validate_account_pool_admission
from app.services.developer_apps import first_assignable_developer_app
from app.services.tenants import ensure_account_quota_available

from .contracts import BatchLoginError
from .identity import parse_code_source_url


@dataclass(frozen=True)
class AccountBindingResult:
    account: TgAccount
    created: bool


@dataclass(frozen=True)
class PhoneAliasCandidate:
    account: TgAccount
    fingerprints: dict[int, str]


def bind_or_create_account(
    session: Session,
    item: TgAccountLoginBatchItem,
    pool_id: int,
    actor: str,
) -> AccountBindingResult:
    phone = decrypt_secret(item.phone_ciphertext)
    if not phone:
        raise BatchLoginError("account_create_failed", "手机号凭据不可用", line_no=item.line_no)
    lock_membership_tenant(session, item.tenant_id)
    fingerprints = phone_fingerprints(item.tenant_id, phone, accepted_phone_fingerprint_versions())
    lock_phone_fingerprints(session, item.tenant_id, fingerprints)
    existing = _batch_account_for_fingerprints(session, item.tenant_id, fingerprints)
    if existing:
        item.account_id = existing.id
        return AccountBindingResult(existing, False)
    account = _create_account(session, item, pool_id, phone, actor)
    _batch_insert_aliases(session, account, fingerprints)
    item.account_id = account.id
    return AccountBindingResult(account, True)


def _create_account(
    session: Session,
    item: TgAccountLoginBatchItem,
    pool_id: int,
    phone: str,
    actor: str,
) -> TgAccount:
    lock_membership_tenant(session, item.tenant_id)
    ensure_account_quota_available(session, item.tenant_id)
    pool = session.get(AccountPool, pool_id)
    if not pool or pool.tenant_id != item.tenant_id:
        raise BatchLoginError("pool_admission_rejected", "目标分组不存在", line_no=item.line_no)
    try:
        from app.services.account_group_revision_snapshot import locked_membership_pools

        pool, = locked_membership_pools(session, item.tenant_id, (pool_id,))
        validate_account_pool_admission(pool)
    except ValueError as exc:
        raise BatchLoginError("pool_admission_rejected", "目标分组不可用", line_no=item.line_no) from exc
    if not first_assignable_developer_app(session):
        raise BatchLoginError("developer_app_unavailable", "没有可用的 Telegram 开发者应用", line_no=item.line_no)
    membership_change = begin_membership_change(session, item.tenant_id, (pool.id,),
        actor=actor, reason="batch_login_account_created")
    account = TgAccount(
        tenant_id=item.tenant_id,
        pool_id=pool.id,
        account_identity=pool.pool_purpose,
        display_name=f"待初始化账号-{item.phone_masked}",
        phone_masked=item.phone_masked,
        phone_ciphertext=encrypt_secret(phone),
    )
    session.add(account)
    session.flush()
    finish_membership_change(session, membership_change)
    audit(session, tenant_id=item.tenant_id, actor=actor, action="批量登录创建TG账号", target_type="tg_account", target_id=str(account.id), detail=f"batch_item_id={item.id}")
    return account


def _batch_account_for_fingerprints(
    session: Session,
    tenant_id: int,
    fingerprints: dict[int, str],
) -> TgAccount | None:
    try:
        return account_for_phone_fingerprints(session, tenant_id, fingerprints)
    except PhoneAliasConflict as exc:
        raise BatchLoginError(exc.code, str(exc)) from exc


def _batch_insert_aliases(session: Session, account: TgAccount, fingerprints: dict[int, str]) -> None:
    try:
        insert_phone_aliases(session, account, fingerprints)
    except PhoneAliasConflict as exc:
        raise BatchLoginError(exc.code, str(exc)) from exc


def bind_account_code_source(
    session: Session,
    account: TgAccount,
    item: TgAccountLoginBatchItem,
    actor: str,
    *,
    verified: bool,
) -> None:
    if account.tenant_id != item.tenant_id:
        raise BatchLoginError("code_source_binding_conflict", "账号与批量行不属于同一租户")
    url = decrypt_secret(item.code_url_ciphertext)
    if not url:
        raise BatchLoginError("credential_expired", "接码地址已过期", line_no=item.line_no)
    spec = parse_code_source_url(url)
    conflict = session.scalar(select(TgAccount).where(
        TgAccount.tenant_id == account.tenant_id,
        TgAccount.code_source_uuid_fingerprint == spec.uuid_fingerprint,
        TgAccount.id != account.id,
    ))
    if conflict:
        raise BatchLoginError("code_source_binding_conflict", "UUID 已绑定其他账号", line_no=item.line_no)
    _assert_binding_version(account, item, spec.uuid_fingerprint)
    same_binding = account.code_source_uuid_fingerprint == spec.uuid_fingerprint
    account.code_source_host = spec.host
    account.code_source_uuid_ciphertext = encrypt_secret(spec.uuid)
    account.code_source_uuid_fingerprint = spec.uuid_fingerprint
    account.code_source_uuid_hint = spec.uuid_hint
    if verified or not same_binding or account.code_source_binding_status != "verified_readable":
        account.code_source_binding_status = "verified_readable" if verified else "provided_unverified"
    account.code_source_binding_version += 1
    account.code_source_bound_at = _now()
    account.code_source_bound_by = actor
    audit(session, tenant_id=account.tenant_id, actor=actor, action="绑定账号接码备注", target_type="tg_account", target_id=str(account.id), detail=f"batch_item_id={item.id}; status={account.code_source_binding_status}")


def _assert_binding_version(account: TgAccount, item: TgAccountLoginBatchItem, fingerprint: str) -> None:
    replacing = bool(account.code_source_uuid_fingerprint and account.code_source_uuid_fingerprint != fingerprint)
    if not replacing:
        return
    if not item.replace_binding or item.expected_binding_version != account.code_source_binding_version:
        raise BatchLoginError("code_source_binding_conflict", "账号接码绑定版本已变化", line_no=item.line_no)


def reveal_account_code_source(
    session: Session,
    tenant_id: int,
    account_id: int,
    expected_version: int,
    actor: str,
    reason: str,
) -> dict[str, object]:
    account = session.get(TgAccount, account_id)
    if not account or account.tenant_id != tenant_id or account.deleted_at is not None:
        raise BatchLoginError("not_found", "账号不存在")
    if account.code_source_binding_version != expected_version:
        raise BatchLoginError("state_conflict", "接码绑定版本已变化")
    uuid_value = decrypt_secret(account.code_source_uuid_ciphertext)
    if not uuid_value:
        raise BatchLoginError("not_found", "账号没有接码绑定")
    audit(session, tenant_id=tenant_id, actor=actor, action="查看账号完整接码UUID", target_type="tg_account", target_id=str(account.id), detail=f"reason={reason.strip()[:80]}; binding_version={expected_version}")
    session.commit()
    return {"account_id": account.id, "host": account.code_source_host, "uuid": uuid_value, "binding_version": account.code_source_binding_version}


def backfill_phone_aliases(
    session: Session,
    tenant_id: int,
    *,
    apply: bool,
    actor: str = "",
    approval_ref: str = "",
) -> dict[str, int]:
    if apply and (not actor.strip() or not approval_ref.strip()):
        raise ValueError("apply requires actor and approval_ref")
    accounts = list(session.scalars(select(TgAccount).where(TgAccount.tenant_id == tenant_id).order_by(TgAccount.id)))
    candidates, missing_phone = _phone_alias_candidates(accounts, tenant_id)
    selected, duplicate_conflicts, shadowed_deleted = _select_phone_alias_owners(candidates)
    plans: list[tuple[TgAccount, dict[int, str]]] = []
    result = {
        "scanned": len(accounts),
        "created": 0,
        "conflicts": duplicate_conflicts,
        "missing_phone": missing_phone,
        "shadowed_deleted": shadowed_deleted,
    }
    for candidate in selected:
        if _alias_conflicts(session, candidate.account, candidate.fingerprints):
            result["conflicts"] += 1
            continue
        missing = missing_phone_aliases(session, candidate.account, candidate.fingerprints)
        result["created"] += len(missing)
        plans.append((candidate.account, missing))
    if apply and result["conflicts"]:
        session.rollback()
        raise BatchLoginError("code_source_binding_conflict", "手机号别名回填存在冲突，未应用")
    if apply:
        for account, missing in plans:
            _batch_insert_aliases(session, account, missing)
        audit(
            session,
            tenant_id=tenant_id,
            actor=actor.strip()[:100],
            action="回填账号手机号别名",
            target_type="tenant",
            target_id=str(tenant_id),
            detail=json.dumps({"approval_ref": approval_ref.strip(), **result}, ensure_ascii=False),
        )
        session.commit()
    return result


def _phone_alias_candidates(accounts: list[TgAccount], tenant_id: int) -> tuple[list[PhoneAliasCandidate], int]:
    candidates: list[PhoneAliasCandidate] = []
    missing_phone = 0
    for account in accounts:
        phone = decrypt_secret(account.phone_ciphertext)
        if not phone:
            missing_phone += 1
            continue
        candidates.append(PhoneAliasCandidate(
            account,
            phone_fingerprints(tenant_id, phone, accepted_phone_fingerprint_versions()),
        ))
    return candidates, missing_phone


def _select_phone_alias_owners(
    candidates: list[PhoneAliasCandidate],
) -> tuple[list[PhoneAliasCandidate], int, int]:
    claims: dict[tuple[int, str], TgAccount] = {}
    selected: list[PhoneAliasCandidate] = []
    conflicts = 0
    shadowed_deleted = 0
    ordered = sorted(
        candidates,
        key=lambda candidate: (candidate.account.deleted_at is not None, candidate.account.id),
    )
    for candidate in ordered:
        owners = [
            claims[(version, fingerprint)]
            for version, fingerprint in candidate.fingerprints.items()
            if (version, fingerprint) in claims
        ]
        if owners:
            if candidate.account.deleted_at is not None and all(owner.deleted_at is None for owner in owners):
                shadowed_deleted += 1
            else:
                conflicts += 1
            continue
        for version, fingerprint in candidate.fingerprints.items():
            claims[(version, fingerprint)] = candidate.account
        selected.append(candidate)
    return selected, conflicts, shadowed_deleted


def _alias_conflicts(session: Session, account: TgAccount, fingerprints: dict[int, str]) -> bool:
    for version, fingerprint in fingerprints.items():
        alias = session.scalar(select(TgAccountPhoneFingerprintAlias).where(
            TgAccountPhoneFingerprintAlias.tenant_id == account.tenant_id,
            TgAccountPhoneFingerprintAlias.key_version == version,
            TgAccountPhoneFingerprintAlias.fingerprint == fingerprint,
        ))
        if alias and alias.account_id != account.id:
            return True
    return False


__all__ = [
    "AccountBindingResult",
    "backfill_phone_aliases",
    "bind_account_code_source",
    "bind_or_create_account",
    "ensure_phone_aliases_for_account",
    "reveal_account_code_source",
]
