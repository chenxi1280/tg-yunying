from __future__ import annotations

import hashlib
import hmac

from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import TgAccount, TgAccountPhoneFingerprintAlias
from app.security import get_token_key


class PhoneAliasConflict(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def phone_fingerprint(tenant_id: int, phone: str, key_version: int) -> str:
    message = f"account-batch-phone:v{key_version}:{tenant_id}:{phone}".encode()
    return hmac.new(get_token_key(), message, hashlib.sha256).hexdigest()


def phone_fingerprints(tenant_id: int, phone: str, versions: tuple[int, ...]) -> dict[int, str]:
    return {version: phone_fingerprint(tenant_id, phone, version) for version in versions}


def accepted_phone_fingerprint_versions() -> tuple[int, ...]:
    raw = get_settings().account_batch_phone_fingerprint_versions
    return tuple(sorted({int(value.strip()) for value in raw.split(",") if value.strip()}))


def lock_phone_fingerprints(session: Session, tenant_id: int, fingerprints: dict[int, str]) -> None:
    if not session.bind or session.bind.dialect.name != "postgresql":
        return
    for version, fingerprint in sorted(fingerprints.items()):
        digest = hashlib.sha256(f"{tenant_id}:{version}:{fingerprint}".encode()).digest()
        lock_key = int.from_bytes(digest[:8], "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})


def account_for_phone_fingerprints(
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
        raise PhoneAliasConflict("code_source_binding_conflict", "手机号身份别名冲突")
    if not account_ids:
        return None
    account = session.get(TgAccount, account_ids.pop())
    if not account:
        raise PhoneAliasConflict("code_source_binding_conflict", "手机号关联到已删除账号")
    if account.deleted_at is not None:
        raise PhoneAliasConflict("soft_deleted_account_conflict", "手机号关联到已删除账号")
    return account


def insert_phone_aliases(session: Session, account: TgAccount, fingerprints: dict[int, str]) -> None:
    try:
        for version, fingerprint in sorted(fingerprints.items()):
            alias = session.scalar(select(TgAccountPhoneFingerprintAlias).where(
                TgAccountPhoneFingerprintAlias.tenant_id == account.tenant_id,
                TgAccountPhoneFingerprintAlias.key_version == version,
                TgAccountPhoneFingerprintAlias.fingerprint == fingerprint,
            ).with_for_update())
            if alias:
                if alias.is_active and alias.account_id != account.id:
                    raise PhoneAliasConflict("code_source_binding_conflict", "手机号身份别名已绑定其他账号")
                alias.account_id = account.id
                alias.is_active = True
                continue
            session.add(TgAccountPhoneFingerprintAlias(
                tenant_id=account.tenant_id,
                account_id=account.id,
                key_version=version,
                fingerprint=fingerprint,
            ))
        session.flush()
    except IntegrityError as exc:
        raise PhoneAliasConflict("code_source_binding_conflict", "手机号身份别名并发冲突") from exc


def missing_phone_aliases(session: Session, account: TgAccount, fingerprints: dict[int, str]) -> dict[int, str]:
    existing = set(session.scalars(select(TgAccountPhoneFingerprintAlias.key_version).where(
        TgAccountPhoneFingerprintAlias.tenant_id == account.tenant_id,
        TgAccountPhoneFingerprintAlias.account_id == account.id,
        TgAccountPhoneFingerprintAlias.key_version.in_(fingerprints),
    )))
    return {version: value for version, value in fingerprints.items() if version not in existing}


def deactivate_account_phone_aliases(session: Session, account: TgAccount) -> None:
    for alias in session.scalars(select(TgAccountPhoneFingerprintAlias).where(
        TgAccountPhoneFingerprintAlias.tenant_id == account.tenant_id,
        TgAccountPhoneFingerprintAlias.account_id == account.id,
        TgAccountPhoneFingerprintAlias.is_active.is_(True),
    ).with_for_update()):
        alias.is_active = False


def ensure_phone_aliases_for_account(session: Session, account: TgAccount, phone: str) -> None:
    fingerprints = phone_fingerprints(account.tenant_id, phone, accepted_phone_fingerprint_versions())
    lock_phone_fingerprints(session, account.tenant_id, fingerprints)
    existing = account_for_phone_fingerprints(session, account.tenant_id, fingerprints)
    if existing and existing.id != account.id:
        raise PhoneAliasConflict("code_source_binding_conflict", "手机号身份别名已绑定其他账号")
    missing = missing_phone_aliases(session, account, fingerprints)
    if missing:
        insert_phone_aliases(session, account, missing)
