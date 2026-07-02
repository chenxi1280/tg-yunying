from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.telegram.contracts import AccountAuthorizationSnapshot
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
    for authorizations in _peer_authorization_views(session, account, exclude_authorization_id):
        matches = [item for item in authorizations if _is_matching_peer_authorization(item, current)]
        usable_hashes = {_usable_hash(item.authorization_hash) for item in matches}
        usable_hashes.discard("")
        if len(usable_hashes) == 1:
            return usable_hashes.pop()
        if len(usable_hashes) > 1:
            raise ValueError("current authorization hash ambiguous")
    return ""


def _peer_authorization_views(
    session: Session,
    account: TgAccount,
    exclude_authorization_id: int | None,
) -> Iterator[list[AccountAuthorizationSnapshot]]:
    if account.session_ciphertext:
        yield gateway.list_authorizations(account.session_ciphertext, credentials_for_account(session, account))
    for row in _peer_authorization_rows(session, account.id, exclude_authorization_id):
        app = session.get(TelegramDeveloperApp, row.developer_app_id) if row.developer_app_id else None
        if app is None:
            continue
        proxy = session.get(AccountProxy, row.proxy_id) if row.proxy_id else None
        credentials = credentials_for_developer_app(app, proxy)
        yield gateway.list_authorizations(row.session_ciphertext, credentials)


def _peer_authorization_rows(
    session: Session,
    account_id: int,
    exclude_authorization_id: int | None,
) -> list[TgAccountAuthorization]:
    query = select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account_id,
        TgAccountAuthorization.disabled_at.is_(None),
        TgAccountAuthorization.status.in_(ACTIVE_PEER_STATUSES),
        TgAccountAuthorization.session_ciphertext.is_not(None),
        TgAccountAuthorization.session_ciphertext != "",
    )
    if exclude_authorization_id is not None:
        query = query.where(TgAccountAuthorization.id != exclude_authorization_id)
    return list(session.scalars(query.order_by(TgAccountAuthorization.id.asc())))


def _is_matching_peer_authorization(
    candidate: AccountAuthorizationSnapshot,
    current: AccountAuthorizationSnapshot,
) -> bool:
    if candidate.is_current:
        return False
    return _fingerprint(candidate) == _fingerprint(current)


def _fingerprint(item: AccountAuthorizationSnapshot) -> tuple[object, ...]:
    return (
        int(item.api_id or 0),
        _text(item.app_name),
        _text(item.device_model),
        _text(item.platform),
        _text(item.system_version),
        _text(item.app_version),
        _timestamp(item.date_created),
    )


def _text(value: str | None) -> str:
    return str(value or "").strip()


def _timestamp(value: datetime | None) -> datetime | None:
    return value.replace(microsecond=0) if value else None


def _usable_hash(value: str | int | None) -> str:
    raw = str(value or "").strip()
    return "" if raw in {"", "0"} else raw


__all__ = ["AuthorizationMetadata", "read_authorization_metadata"]
