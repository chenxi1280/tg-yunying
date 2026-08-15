from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    AccountPool,
    TgAccount,
    TgAccountLoginBatchItem,
    TgAccountPhoneFingerprintAlias,
)
from app.security import decrypt_secret, encrypt_secret
from app.services._common import _now, audit
from app.services.dedicated_account_pools import validate_account_pool_admission
from app.services.developer_apps import first_assignable_developer_app
from app.services.tenants import ensure_account_quota_available

from .contracts import BatchLoginError
from .identity import parse_code_source_url, phone_fingerprints


@dataclass(frozen=True)
class AccountBindingResult:
    account: TgAccount
    created: bool


def bind_or_create_account(
    session: Session,
    item: TgAccountLoginBatchItem,
    pool_id: int,
    actor: str,
) -> AccountBindingResult:
    phone = decrypt_secret(item.phone_ciphertext)
    if not phone:
        raise BatchLoginError("account_create_failed", "手机号凭据不可用", line_no=item.line_no)
    fingerprints = phone_fingerprints(item.tenant_id, phone, _accepted_versions())
    _lock_phone_fingerprints(session, item.tenant_id, fingerprints)
    existing = _account_for_fingerprints(session, item.tenant_id, fingerprints)
    if existing:
        item.account_id = existing.id
        return AccountBindingResult(existing, False)
    account = _create_account(session, item, pool_id, phone, actor)
    _insert_aliases(session, account, fingerprints)
    item.account_id = account.id
    return AccountBindingResult(account, True)


def _create_account(
    session: Session,
    item: TgAccountLoginBatchItem,
    pool_id: int,
    phone: str,
    actor: str,
) -> TgAccount:
    ensure_account_quota_available(session, item.tenant_id)
    pool = session.get(AccountPool, pool_id)
    if not pool or pool.tenant_id != item.tenant_id:
        raise BatchLoginError("pool_admission_rejected", "目标分组不存在", line_no=item.line_no)
    try:
        validate_account_pool_admission(pool)
    except ValueError as exc:
        raise BatchLoginError("pool_admission_rejected", "目标分组不可用", line_no=item.line_no) from exc
    if not first_assignable_developer_app(session):
        raise BatchLoginError("developer_app_unavailable", "没有可用的 Telegram 开发者应用", line_no=item.line_no)
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
    audit(session, tenant_id=item.tenant_id, actor=actor, action="批量登录创建TG账号", target_type="tg_account", target_id=str(account.id), detail=f"batch_item_id={item.id}")
    return account


def _account_for_fingerprints(
    session: Session,
    tenant_id: int,
    fingerprints: dict[int, str],
) -> TgAccount | None:
    aliases = list(session.scalars(select(TgAccountPhoneFingerprintAlias).where(
        TgAccountPhoneFingerprintAlias.tenant_id == tenant_id,
        TgAccountPhoneFingerprintAlias.is_active.is_(True),
        or_(*[
            and_(TgAccountPhoneFingerprintAlias.key_version == version, TgAccountPhoneFingerprintAlias.fingerprint == value)
            for version, value in fingerprints.items()
        ]),
    ).with_for_update()))
    account_ids = {alias.account_id for alias in aliases}
    if len(account_ids) > 1:
        raise BatchLoginError("code_source_binding_conflict", "手机号身份别名冲突")
    if not account_ids:
        return None
    account = session.get(TgAccount, account_ids.pop())
    if not account:
        raise BatchLoginError("code_source_binding_conflict", "手机号关联到已删除账号")
    if account.deleted_at is not None:
        raise BatchLoginError("soft_deleted_account_conflict", "手机号关联到已删除账号")
    return account


def _lock_phone_fingerprints(session: Session, tenant_id: int, fingerprints: dict[int, str]) -> None:
    if not session.bind or session.bind.dialect.name != "postgresql":
        return
    for version, fingerprint in sorted(fingerprints.items()):
        digest = hashlib.sha256(f"{tenant_id}:{version}:{fingerprint}".encode()).digest()
        lock_key = int.from_bytes(digest[:8], "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})


def _insert_aliases(session: Session, account: TgAccount, fingerprints: dict[int, str]) -> None:
    try:
        for version, fingerprint in sorted(fingerprints.items()):
            session.add(TgAccountPhoneFingerprintAlias(
                tenant_id=account.tenant_id,
                account_id=account.id,
                key_version=version,
                fingerprint=fingerprint,
            ))
        session.flush()
    except IntegrityError as exc:
        raise BatchLoginError("code_source_binding_conflict", "手机号身份别名并发冲突") from exc


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


def backfill_phone_aliases(session: Session, tenant_id: int, *, apply: bool) -> dict[str, int]:
    accounts = list(session.scalars(select(TgAccount).where(TgAccount.tenant_id == tenant_id).order_by(TgAccount.id)))
    result = {"scanned": len(accounts), "created": 0, "conflicts": 0, "missing_phone": 0}
    for account in accounts:
        phone = decrypt_secret(account.phone_ciphertext)
        if not phone:
            result["missing_phone"] += 1
            continue
        fingerprints = phone_fingerprints(tenant_id, phone, _accepted_versions())
        if _alias_conflicts(session, account, fingerprints):
            result["conflicts"] += 1
            continue
        missing = _missing_aliases(session, account, fingerprints)
        result["created"] += len(missing)
        if apply:
            _insert_aliases(session, account, missing)
    if apply and result["conflicts"]:
        session.rollback()
        raise BatchLoginError("code_source_binding_conflict", "手机号别名回填存在冲突，未应用")
    if apply:
        session.commit()
    return result


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


def _missing_aliases(session: Session, account: TgAccount, fingerprints: dict[int, str]) -> dict[int, str]:
    existing = set(session.scalars(select(TgAccountPhoneFingerprintAlias.key_version).where(
        TgAccountPhoneFingerprintAlias.tenant_id == account.tenant_id,
        TgAccountPhoneFingerprintAlias.account_id == account.id,
        TgAccountPhoneFingerprintAlias.key_version.in_(fingerprints),
    )))
    return {version: value for version, value in fingerprints.items() if version not in existing}


def _accepted_versions() -> tuple[int, ...]:
    raw = get_settings().account_batch_phone_fingerprint_versions
    return tuple(sorted({int(value.strip()) for value in raw.split(",") if value.strip()}))


__all__ = [
    "AccountBindingResult",
    "backfill_phone_aliases",
    "bind_account_code_source",
    "bind_or_create_account",
    "reveal_account_code_source",
]
