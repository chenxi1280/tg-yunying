from __future__ import annotations

from dataclasses import dataclass, replace
from collections.abc import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.telegram.authorization_fingerprint import authorization_fingerprint_digest
from app.integrations.telegram.contracts import AccountAuthorizationSnapshot, AuthorizationIdentity
from app.models import AccountProxy, TelegramDeveloperApp, TgAccount, TgAccountAuthorization

from ._common import gateway
from .developer_apps import credentials_for_account, credentials_for_developer_app

ACTIVE_PEER_STATUSES = {"active", "standby"}


@dataclass(frozen=True)
class AuthorizationMetadata:
    authorization_hash: str
    api_id: int


def read_authorization_metadata(
    session: Session,
    *,
    account: TgAccount,
    app: TelegramDeveloperApp,
    proxy: AccountProxy | None,
    session_ciphertext: str,
    exclude_authorization_id: int | None = None,
) -> AuthorizationMetadata:
    credentials = credentials_for_developer_app(app, proxy)
    authorizations = gateway.list_authorizations(session_ciphertext, credentials)
    current = _current_authorization(authorizations)
    api_id = int(current.api_id or app.api_id or 0)
    if not api_id:
        raise ValueError("current authorization api_id missing")
    direct_hash = _usable_hash(current.authorization_hash)
    if direct_hash:
        return AuthorizationMetadata(authorization_hash=direct_hash, api_id=api_id)
    peer_hash = _peer_authorization_hash(session, account, current, exclude_authorization_id)
    if not peer_hash:
        raise ValueError("current authorization hash missing")
    return AuthorizationMetadata(authorization_hash=peer_hash, api_id=api_id)


def _current_authorization(authorizations: list[AccountAuthorizationSnapshot]) -> AccountAuthorizationSnapshot:
    current = next((item for item in authorizations if item.is_current), None)
    if current is None:
        raise ValueError("current authorization not found")
    return current


def _peer_authorization_hash(
    session: Session,
    account: TgAccount,
    current: AccountAuthorizationSnapshot,
    exclude_authorization_id: int | None,
) -> str:
    return resolve_peer_authorization_hash(
        session,
        account.id,
        authorization_fingerprint_digest(current),
        exclude_authorization_id=exclude_authorization_id,
    )


def resolve_peer_authorization_hash(
    session: Session,
    account_id: int,
    fingerprint_digest: str,
    *,
    exclude_authorization_id: int | None = None,
) -> str:
    account = session.get(TgAccount, account_id)
    if account is None:
        raise ValueError("authorization account not found")
    usable_hashes: set[str] = set()
    for authorizations in _peer_authorization_views(session, account, exclude_authorization_id):
        usable_hashes.update(
            _usable_hash(item.authorization_hash)
            for item in authorizations
            if not item.is_current and authorization_fingerprint_digest(item) == fingerprint_digest
        )
    usable_hashes.discard("")
    if len(usable_hashes) > 1:
        raise ValueError("current authorization hash ambiguous")
    return next(iter(usable_hashes), "")


def resolve_authorization_identity_hash(
    session: Session,
    account_id: int,
    identity: AuthorizationIdentity,
    *,
    exclude_authorization_id: int | None = None,
) -> tuple[AuthorizationIdentity, str]:
    direct = _usable_hash(identity.authorization_hash)
    if direct:
        return identity, "direct"
    resolved = resolve_peer_authorization_hash(
        session,
        account_id,
        identity.authorization_fingerprint_digest,
        exclude_authorization_id=exclude_authorization_id,
    )
    if not resolved:
        raise ValueError("current authorization hash missing")
    return replace(identity, authorization_hash=resolved), "peer_observer"


def _peer_authorization_views(
    session: Session,
    account: TgAccount,
    exclude_authorization_id: int | None,
) -> Iterator[list[AccountAuthorizationSnapshot]]:
    rows = _peer_authorization_rows(session, account.id, exclude_authorization_id)
    for row in rows:
        app = session.get(TelegramDeveloperApp, row.developer_app_id) if row.developer_app_id else None
        if app is None:
            continue
        credentials = credentials_for_developer_app(app)
        yield gateway.list_authorizations(row.session_ciphertext, credentials)
    if not rows and account.session_ciphertext:
        credentials = credentials_for_account(session, account)
        yield gateway.list_authorizations(account.session_ciphertext, credentials)


def _peer_authorization_rows(
    session: Session,
    account_id: int,
    exclude_authorization_id: int | None,
) -> list[TgAccountAuthorization]:
    query = select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account_id,
        TgAccountAuthorization.disabled_at.is_(None),
        TgAccountAuthorization.status.in_(ACTIVE_PEER_STATUSES),
        TgAccountAuthorization.health_status == "healthy",
        TgAccountAuthorization.is_slot_current.is_(True),
        TgAccountAuthorization.session_ciphertext.is_not(None),
        TgAccountAuthorization.session_ciphertext != "",
    )
    if exclude_authorization_id is not None:
        query = query.where(TgAccountAuthorization.id != exclude_authorization_id)
    return list(session.scalars(query.order_by(TgAccountAuthorization.id.asc())))


def _usable_hash(value: str | int | None) -> str:
    raw = str(value or "").strip()
    return "" if raw in {"", "0"} else raw


__all__ = [
    "AuthorizationMetadata",
    "read_authorization_metadata",
    "resolve_authorization_identity_hash",
    "resolve_peer_authorization_hash",
]
