from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import select

from app.models import TgAccount, TgAccountAuthorization
from app.security import decrypt_secret, decrypt_session, encrypt_secret
from app.services._common import _now, audit, gateway
from app.services.account_authorization_metadata import resolve_authorization_identity_hash
from app.services.developer_apps import credentials_for_authorization

from .contracts import AuthorizationDrError


@dataclass(frozen=True)
class StandbyQualificationSnapshot:
    account_id: int
    primary_id: int
    primary_fact_version: int
    primary_session_digest: str
    authorization_generation: int
    authorization_fact_generation: int
    connection_generation: int
    standby_id: int
    standby_fact_version: int
    standby_session_digest: str
    standby_app_id: int
    standby_proxy_id: int | None
    standby_logical_slot: str


def qualify_existing_standby_1(session, item, *, actor: str, approval_ref: str) -> dict:
    snapshot, primary, standby = _snapshot(session, item)
    if _identity_complete(standby):
        return {"authorization_id": standby.id, "status": "already_qualified"}
    identity = gateway.authorization_identity(
        decrypt_session(standby.session_ciphertext),
        credentials_for_authorization(session, standby),
    )
    identity, hash_source = resolve_authorization_identity_hash(
        session,
        item.account_id,
        identity,
        exclude_authorization_id=standby.id,
    )
    _validate_identity(primary, standby, identity)
    session.expire_all()
    locked_snapshot, _primary, locked = _snapshot(session, item, for_update=True)
    if locked_snapshot != snapshot:
        session.rollback()
        raise AuthorizationDrError("online_abc_primary_drift", "A or existing B changed during qualification")
    _apply_identity(locked, identity)
    audit(
        session,
        tenant_id=item.tenant_id,
        actor=actor,
        action="补齐既有 standby_1 授权身份",
        target_type="tg_account_authorization",
        target_id=str(locked.id),
        detail=f"account_id={item.account_id}; approval_ref={approval_ref}; hash_source={hash_source}",
    )
    session.commit()
    return {"authorization_id": locked.id, "status": "qualified"}


def require_existing_standby_1_candidate(session, item) -> None:
    _snapshot(session, item)


def _snapshot(session, item, *, for_update: bool = False):
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    standby = _standby(session, item, primary, for_update=for_update)
    _validate_frozen_primary(account, primary, item)
    value = StandbyQualificationSnapshot(
        account_id=account.id,
        primary_id=primary.id,
        primary_fact_version=primary.fact_version,
        primary_session_digest=_digest(primary.session_ciphertext or ""),
        authorization_generation=account.authorization_generation,
        authorization_fact_generation=account.authorization_fact_generation,
        connection_generation=account.connection_generation,
        standby_id=standby.id,
        standby_fact_version=standby.fact_version,
        standby_session_digest=_digest(standby.session_ciphertext or ""),
        standby_app_id=standby.developer_app_id,
        standby_proxy_id=standby.proxy_id,
        standby_logical_slot=standby.logical_slot,
    )
    return value, primary, standby


def _standby(session, item, primary, *, for_update: bool):
    target_slot = "primary" if primary and primary.logical_slot == "standby_1" else "standby_1"
    query = select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == item.account_id,
        TgAccountAuthorization.id != item.primary_authorization_id,
        TgAccountAuthorization.logical_slot == target_slot,
        TgAccountAuthorization.is_slot_current.is_(True),
        TgAccountAuthorization.is_current.is_(False),
        TgAccountAuthorization.provision_region_code == "sv",
        TgAccountAuthorization.status.in_({"active", "standby"}),
        TgAccountAuthorization.health_status == "healthy",
        TgAccountAuthorization.session_ciphertext.is_not(None),
        TgAccountAuthorization.disabled_at.is_(None),
    )
    rows = list(session.scalars(query.with_for_update() if for_update else query))
    if len(rows) != 1 or rows[0].developer_app_id != item.app_b_id:
        raise AuthorizationDrError("sv_redundancy_incomplete", "Frozen existing B is unavailable")
    return rows[0]


def _validate_frozen_primary(account, primary, item) -> None:
    valid = bool(
        account
        and primary
        and account.current_authorization_id == primary.id
        and account.authorization_generation == item.authorization_generation
        and account.authorization_fact_generation == item.authorization_fact_generation + 1
        and account.connection_generation == item.connection_generation
        and primary.fact_version == item.primary_fact_version + 1
        and _digest(primary.session_ciphertext or "") == item.primary_session_digest
        and primary.is_current
        and primary.telegram_user_id_digest
        and primary.auth_key_fingerprint_digest
    )
    if not valid:
        raise AuthorizationDrError("online_abc_primary_drift", "Frozen A changed before B qualification")


def _identity_complete(standby) -> bool:
    raw_hash = decrypt_secret(standby.telegram_authorization_hash_ciphertext or "")
    return bool(
        standby.telegram_user_id_digest
        and standby.auth_key_fingerprint_digest
        and raw_hash
        and raw_hash != "0"
    )


def _validate_identity(primary, standby, identity) -> None:
    if identity.telegram_user_id_digest != primary.telegram_user_id_digest:
        raise AuthorizationDrError("authorization_identity_mismatch", "Existing B belongs to another Telegram account")
    if identity.auth_key_fingerprint_digest == primary.auth_key_fingerprint_digest:
        raise AuthorizationDrError("authorization_identity_mismatch", "Existing B duplicates A AuthKey")
    if standby.telegram_user_id_digest and standby.telegram_user_id_digest != identity.telegram_user_id_digest:
        raise AuthorizationDrError("authorization_identity_mismatch", "Existing B UID changed")
    stored_key = standby.auth_key_fingerprint_digest
    if stored_key and stored_key != identity.auth_key_fingerprint_digest:
        raise AuthorizationDrError("authorization_identity_mismatch", "Existing B AuthKey changed")
    if not identity.authorization_hash or identity.authorization_hash == "0":
        raise AuthorizationDrError("authorization_hash_missing", "Existing B authorization hash is unavailable")


def _apply_identity(standby, identity) -> None:
    standby.telegram_authorization_hash_ciphertext = encrypt_secret(identity.authorization_hash)
    standby.telegram_user_id_digest = identity.telegram_user_id_digest
    standby.auth_key_fingerprint_digest = identity.auth_key_fingerprint_digest
    standby.status = "standby"
    standby.health_status = "healthy"
    standby.derived_status = "healthy"
    standby.remote_authorization_state = "active"
    standby.last_health_check_at = _now()
    standby.last_success_at = _now()
    standby.fact_version += 1


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


__all__ = ["qualify_existing_standby_1", "require_existing_standby_1_candidate"]
