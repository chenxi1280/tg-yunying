from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountProxy, TelegramDeveloperApp, TgAccountAuthorization
from app.security import encrypt_secret

from ._common import audit, gateway
from .developer_apps import credentials_for_developer_app

STANDBY_ROLES = {"standby_1", "standby_2"}
ACTIVE_STANDBY_STATUSES = {"active", "standby"}


@dataclass(frozen=True)
class AuthorizationMetadata:
    authorization_hash: str
    api_id: int


def backfill_standby_authorization_metadata(
    session: Session,
    *,
    tenant_id: int,
    apply: bool,
    actor: str,
    limit: int = 1000,
    account_id: int | None = None,
) -> dict[str, Any]:
    candidates = _candidate_authorizations(session, tenant_id, limit, account_id)
    updated: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for authorization in candidates:
        try:
            metadata = _read_current_authorization_metadata(session, authorization)
            updated.append(_result_item(authorization, metadata))
            if apply:
                _apply_metadata(authorization, metadata)
        except Exception as exc:  # noqa: BLE001 - production backfill must expose every row failure.
            failures.append(_failure_item(authorization, exc))
            if apply:
                _mark_metadata_backfill_failed(authorization, exc)
    if apply and updated:
        audit(
            session,
            tenant_id=tenant_id,
            actor=actor,
            action="回填备用授权设备 hash",
            target_type="tg_account_authorizations",
            target_id=str(tenant_id),
            detail=f"updated={len(updated)}; failed={len(failures)}",
        )
    if apply:
        session.commit()
    return {
        "mode": "apply" if apply else "dry_run",
        "candidate_count": len(candidates),
        "updated_count": len(updated),
        "failed_count": len(failures),
        "updated": updated,
        "failures": failures,
    }


def _candidate_authorizations(
    session: Session,
    tenant_id: int,
    limit: int,
    account_id: int | None,
) -> list[TgAccountAuthorization]:
    filters = [
        TgAccountAuthorization.tenant_id == tenant_id,
        TgAccountAuthorization.disabled_at.is_(None),
        TgAccountAuthorization.role.in_(STANDBY_ROLES),
        TgAccountAuthorization.status.in_(ACTIVE_STANDBY_STATUSES),
        TgAccountAuthorization.session_ciphertext.is_not(None),
        TgAccountAuthorization.session_ciphertext != "",
        (
            (TgAccountAuthorization.telegram_authorization_hash_ciphertext == "")
            | TgAccountAuthorization.telegram_authorization_hash_ciphertext.is_(None)
            | (TgAccountAuthorization.developer_app_api_id_snapshot == 0)
        ),
    ]
    if account_id is not None:
        filters.append(TgAccountAuthorization.account_id == account_id)
    query = select(TgAccountAuthorization).where(*filters).order_by(TgAccountAuthorization.id.asc()).limit(max(1, limit))
    return list(session.scalars(query))


def _read_current_authorization_metadata(session: Session, authorization: TgAccountAuthorization) -> AuthorizationMetadata:
    app = _developer_app(session, authorization)
    proxy = session.get(AccountProxy, authorization.proxy_id) if authorization.proxy_id else None
    credentials = credentials_for_developer_app(app, proxy)
    authorizations = gateway.list_authorizations(authorization.session_ciphertext, credentials)
    current = next((item for item in authorizations if item.is_current), None)
    if current is None:
        raise ValueError("current authorization not found")
    if not current.authorization_hash:
        raise ValueError("current authorization hash missing")
    api_id = int(current.api_id or app.api_id or 0)
    if not api_id:
        raise ValueError("current authorization api_id missing")
    return AuthorizationMetadata(authorization_hash=str(current.authorization_hash), api_id=api_id)


def _developer_app(session: Session, authorization: TgAccountAuthorization) -> TelegramDeveloperApp:
    if authorization.developer_app_id is None:
        raise ValueError("authorization missing developer app")
    app = session.get(TelegramDeveloperApp, authorization.developer_app_id)
    if app is None:
        raise ValueError("authorization developer app not found")
    return app


def _apply_metadata(authorization: TgAccountAuthorization, metadata: AuthorizationMetadata) -> None:
    authorization.telegram_authorization_hash_ciphertext = encrypt_secret(metadata.authorization_hash)
    authorization.developer_app_api_id_snapshot = metadata.api_id


def _mark_metadata_backfill_failed(authorization: TgAccountAuthorization, exc: Exception) -> None:
    authorization.status = "needs_repair"
    authorization.health_status = "failed"
    authorization.derived_status = "manual_required"
    authorization.failure_reason = f"备用授权元数据回填失败：{exc}"


def _result_item(authorization: TgAccountAuthorization, metadata: AuthorizationMetadata) -> dict[str, Any]:
    return {
        "authorization_id": authorization.id,
        "account_id": authorization.account_id,
        "role": authorization.role,
        "api_id": metadata.api_id,
        "has_hash": bool(metadata.authorization_hash),
    }


def _failure_item(authorization: TgAccountAuthorization, exc: Exception) -> dict[str, Any]:
    return {
        "authorization_id": authorization.id,
        "account_id": authorization.account_id,
        "role": authorization.role,
        "error": str(exc),
    }


__all__ = ["backfill_standby_authorization_metadata"]
