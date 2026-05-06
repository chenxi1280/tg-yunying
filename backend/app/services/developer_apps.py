from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.gateways import DeveloperAppCredentials
from app.models import (
    AccountStatus,
    DeveloperAppHealthStatus,
    TelegramDeveloperApp,
    TgAccount,
)
from app.schemas import DeveloperAppCreate, DeveloperAppUpdate
from app.security import decrypt_secret, encrypt_secret

from ._common import _as_utc, _now, audit


def seed_developer_apps(session: Session) -> None:
    if session.scalar(select(func.count(TelegramDeveloperApp.id))) > 0:
        return
    settings = get_settings()
    if settings.tg_api_id and settings.tg_api_hash:
        app_name = "环境默认开发者应用"
        api_id = int(settings.tg_api_id)
        api_hash = settings.tg_api_hash
        notes = "由 TG_API_ID/TG_API_HASH 初始化"
    else:
        app_name = "Mock 开发者应用"
        api_id = 100000
        api_hash = "mock_api_hash_for_development"
        notes = "开发和测试默认凭证，真实接入时请替换"
    session.add(
        TelegramDeveloperApp(
            app_name=app_name,
            api_id=api_id,
            api_hash_ciphertext=encrypt_secret(api_hash),
            health_status=DeveloperAppHealthStatus.HEALTHY.value,
            notes=notes,
        )
    )
    session.flush()


def first_assignable_developer_app(session: Session) -> TelegramDeveloperApp | None:
    return session.scalar(select(TelegramDeveloperApp).order_by(TelegramDeveloperApp.id.asc()).limit(1))


def backfill_account_developer_apps(session: Session) -> None:
    app = first_assignable_developer_app(session)
    if not app:
        return
    accounts = list(session.scalars(select(TgAccount).where(TgAccount.developer_app_id.is_(None))))
    for account in accounts:
        account.developer_app_id = app.id
        account.developer_app_version = app.credentials_version


def developer_app_snapshot(session: Session, app: TelegramDeveloperApp) -> dict:
    assigned = session.scalar(select(func.count(TgAccount.id)).where(TgAccount.developer_app_id == app.id)) or 0
    return {
        "id": app.id,
        "app_name": app.app_name,
        "api_id": app.api_id,
        "is_active": app.is_active,
        "health_status": app.health_status,
        "max_accounts": app.max_accounts,
        "assigned_accounts": assigned,
        "credentials_version": app.credentials_version,
        "last_assigned_at": app.last_assigned_at,
        "last_check_at": app.last_check_at,
        "last_error": app.last_error,
        "notes": app.notes,
        "created_at": app.created_at,
        "updated_at": app.updated_at,
    }


def list_developer_apps(session: Session) -> list[dict]:
    apps = session.scalars(select(TelegramDeveloperApp).order_by(TelegramDeveloperApp.id.asc())).all()
    return [developer_app_snapshot(session, app) for app in apps]


def create_developer_app(session: Session, payload: DeveloperAppCreate, actor: str) -> dict:
    app = TelegramDeveloperApp(
        app_name=payload.app_name,
        api_id=payload.api_id,
        api_hash_ciphertext=encrypt_secret(payload.api_hash),
        is_active=payload.is_active,
        health_status=DeveloperAppHealthStatus.HEALTHY.value if payload.is_active else DeveloperAppHealthStatus.DISABLED.value,
        max_accounts=payload.max_accounts,
        notes=payload.notes,
    )
    session.add(app)
    session.flush()
    audit(session, tenant_id=None, actor=actor, action="新增开发者应用", target_type="developer_app", target_id=str(app.id))
    session.commit()
    session.refresh(app)
    return developer_app_snapshot(session, app)


def update_developer_app(session: Session, app_id: int, payload: DeveloperAppUpdate, actor: str) -> dict:
    app = session.get(TelegramDeveloperApp, app_id)
    if not app:
        raise ValueError("developer app not found")
    data = payload.model_dump(exclude_unset=True)
    if data.get("app_name") is not None:
        app.app_name = data["app_name"]
    if data.get("api_hash"):
        app.api_hash_ciphertext = encrypt_secret(data["api_hash"])
        app.credentials_version += 1
    if data.get("is_active") is not None:
        app.is_active = data["is_active"]
        app.health_status = DeveloperAppHealthStatus.HEALTHY.value if app.is_active else DeveloperAppHealthStatus.DISABLED.value
    if data.get("max_accounts") is not None:
        app.max_accounts = data["max_accounts"]
    if data.get("notes") is not None:
        app.notes = data["notes"]
    app.updated_at = _now()
    audit(session, tenant_id=None, actor=actor, action="更新开发者应用", target_type="developer_app", target_id=str(app.id))
    session.commit()
    session.refresh(app)
    return developer_app_snapshot(session, app)


def set_developer_app_active(session: Session, app_id: int, is_active: bool, actor: str) -> dict:
    return update_developer_app(session, app_id, DeveloperAppUpdate(is_active=is_active), actor)


def check_developer_app(session: Session, app_id: int, actor: str) -> dict:
    app = session.get(TelegramDeveloperApp, app_id)
    if not app:
        raise ValueError("developer app not found")
    app.last_check_at = _now()
    if not app.is_active:
        app.health_status = DeveloperAppHealthStatus.DISABLED.value
        app.last_error = "开发者应用已禁用"
    else:
        try:
            decrypt_secret(app.api_hash_ciphertext)
            app.health_status = DeveloperAppHealthStatus.HEALTHY.value
            app.last_error = ""
        except Exception as exc:
            app.health_status = DeveloperAppHealthStatus.UNHEALTHY.value
            app.last_error = str(exc)
    app.updated_at = _now()
    audit(session, tenant_id=None, actor=actor, action="检查开发者应用", target_type="developer_app", target_id=str(app.id), detail=app.health_status)
    session.commit()
    session.refresh(app)
    return developer_app_snapshot(session, app)


def assign_developer_app_round_robin(session: Session, account: TgAccount) -> TelegramDeveloperApp:
    if account.developer_app_id:
        app = session.get(TelegramDeveloperApp, account.developer_app_id)
        if app and app.is_active and app.health_status == DeveloperAppHealthStatus.HEALTHY.value:
            return app

    apps = session.scalars(
        select(TelegramDeveloperApp).where(
            TelegramDeveloperApp.is_active.is_(True),
            TelegramDeveloperApp.health_status == DeveloperAppHealthStatus.HEALTHY.value,
        )
    ).all()
    candidates: list[tuple[float, int, TelegramDeveloperApp]] = []
    for app in apps:
        assigned = session.scalar(select(func.count(TgAccount.id)).where(TgAccount.developer_app_id == app.id)) or 0
        if app.max_accounts > 0 and assigned >= app.max_accounts:
            continue
        assigned_at = app.last_assigned_at
        if assigned_at is None:
            assigned_at = datetime(1970, 1, 1, tzinfo=UTC)
        else:
            assigned_at = _as_utc(assigned_at)
        candidates.append((assigned_at.timestamp(), app.id, app))
    if not candidates:
        raise ValueError("没有可用的 TG 开发者应用")
    _, _, app = sorted(candidates, key=lambda item: (item[0], item[1]))[0]
    account.developer_app_id = app.id
    account.developer_app_version = app.credentials_version
    app.last_assigned_at = _now()
    return app


def credentials_for_developer_app(app: TelegramDeveloperApp) -> DeveloperAppCredentials:
    if not app.is_active:
        raise ValueError("开发者应用未启用")
    if app.health_status != DeveloperAppHealthStatus.HEALTHY.value:
        raise ValueError("开发者应用当前不健康")
    api_hash = decrypt_secret(app.api_hash_ciphertext)
    if not api_hash:
        raise ValueError("开发者应用缺少 api_hash")
    return DeveloperAppCredentials(
        app_id=app.id,
        api_id=app.api_id,
        api_hash=api_hash,
        credentials_version=app.credentials_version,
        app_name=app.app_name,
    )


def credentials_for_account(session: Session, account: TgAccount, *, assign_if_missing: bool = False) -> DeveloperAppCredentials:
    app = assign_developer_app_round_robin(session, account) if assign_if_missing or not account.developer_app_id else session.get(TelegramDeveloperApp, account.developer_app_id)
    if not app:
        raise ValueError("账号未绑定开发者应用")
    if app.credentials_version > account.developer_app_version:
        account.status = AccountStatus.NEED_RELOGIN.value
        raise ValueError("开发者应用凭证已轮换，账号需要重新登录")
    return credentials_for_developer_app(app)


__all__ = [
    "assign_developer_app_round_robin",
    "backfill_account_developer_apps",
    "check_developer_app",
    "create_developer_app",
    "credentials_for_account",
    "credentials_for_developer_app",
    "developer_app_snapshot",
    "first_assignable_developer_app",
    "list_developer_apps",
    "seed_developer_apps",
    "set_developer_app_active",
    "update_developer_app",
]
