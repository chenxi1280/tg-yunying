from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AccountProxy, TelegramDeveloperApp, TgAccountAuthorization, TgAccountAuthorizationSnapshot
from app.schemas.account_environment import AccountEnvironmentBindingOut
from app.security import encrypt_secret
from app.services._common import _now, audit, gateway
from app.services.account_environment import _authorization_rows, list_account_environment_bindings
from app.services.developer_apps import credentials_for_developer_app


def refresh_account_environment_observations(
    session: Session,
    *,
    tenant_id: int,
    actor: str,
) -> list[AccountEnvironmentBindingOut]:
    refreshed_slots = _refresh_remote_authorization_snapshots(session, tenant_id)
    rows = list_account_environment_bindings(session, tenant_id=tenant_id)
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="刷新授权环境远端观测",
        target_type="account_environment_binding",
        target_id="all",
        detail=f"authorization_slots={len(rows)}; refreshed_slots={refreshed_slots}; source=telegram_authorization_list",
    )
    return rows


def _refresh_remote_authorization_snapshots(session: Session, tenant_id: int) -> int:
    authorizations = _authorization_rows(session, tenant_id)
    _delete_existing_snapshots(session, tenant_id, authorizations)
    refreshed = 0
    for authorization in authorizations:
        if not _can_refresh_authorization(authorization):
            continue
        app = session.get(TelegramDeveloperApp, authorization.developer_app_id or 0)
        if app is None:
            continue
        proxy = session.get(AccountProxy, authorization.proxy_id or 0) if authorization.proxy_id else None
        credentials = credentials_for_developer_app(app, proxy)
        snapshots = gateway.list_authorizations(authorization.session_ciphertext, credentials)
        for snapshot in _current_session_snapshots(snapshots):
            session.add(_new_authorization_snapshot(authorization, snapshot))
        refreshed += 1
    session.flush()
    return refreshed


def _delete_existing_snapshots(
    session: Session,
    tenant_id: int,
    authorizations: list[TgAccountAuthorization],
) -> None:
    authorization_ids = sorted({authorization.id for authorization in authorizations if authorization.id is not None})
    if not authorization_ids:
        return
    session.query(TgAccountAuthorizationSnapshot).filter(
        TgAccountAuthorizationSnapshot.tenant_id == tenant_id,
        TgAccountAuthorizationSnapshot.authorization_id.in_(authorization_ids),
    ).delete(synchronize_session=False)


def _can_refresh_authorization(authorization: TgAccountAuthorization) -> bool:
    return bool(authorization.session_ciphertext and authorization.developer_app_id)


def _new_authorization_snapshot(
    authorization: TgAccountAuthorization,
    snapshot,
) -> TgAccountAuthorizationSnapshot:
    return TgAccountAuthorizationSnapshot(
        tenant_id=authorization.tenant_id,
        account_id=authorization.account_id,
        authorization_id=authorization.id,
        developer_app_id=authorization.developer_app_id,
        session_role=authorization.role,
        authorization_hash_ciphertext=encrypt_secret(snapshot.authorization_hash),
        is_platform_trusted=bool(snapshot.is_current),
        is_current_session=bool(snapshot.is_current),
        device_model=snapshot.device_model,
        platform=snapshot.platform,
        system_version=snapshot.system_version,
        api_id=snapshot.api_id,
        app_name=snapshot.app_name,
        app_version=snapshot.app_version,
        ip_masked=_mask_ip(snapshot.ip),
        country=snapshot.country,
        region=snapshot.region,
        date_created=snapshot.date_created,
        date_active=snapshot.date_active,
        scanned_at=_now(),
    )


def _current_session_snapshots(snapshots) -> list:
    return [snapshot for snapshot in snapshots if bool(snapshot.is_current)]


def _mask_ip(value: str | None) -> str:
    parts = str(value or "").split(".")
    if len(parts) == 4:
        return ".".join([parts[0], parts[1], "*", "*"])
    return ""


__all__ = ["refresh_account_environment_observations"]
