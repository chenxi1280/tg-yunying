from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations.telegram import DeveloperAppCredentials
from app.models import (
    AccountProxy,
    DeveloperAppSlotAssignment,
    AccountStatus,
    DeveloperAppHealthStatus,
    TelegramDeveloperApp,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
)
from app.schemas import DeveloperAppCreate, DeveloperAppUpdate
from app.security import decrypt_secret, encrypt_secret
from app.timezone import BEIJING_TZ

from ._common import _as_utc, _now, audit


SLOT_PURPOSES = {
    "primary_sv": "app_a_id",
    "standby_1_sv": "app_b_id",
    "standby_2_my": "app_c_id",
}
TERMINAL_DR_OPERATION_STATUSES = frozenset({
    "succeeded", "failed", "cancelled", "expired", "migration_rolled_back_forward",
})


class DeveloperAppAssignmentVersionConflict(ValueError):
    pass


def seed_developer_apps(session: Session) -> None:
    if session.scalar(select(func.count(TelegramDeveloperApp.id))) > 0:
        return
    settings = get_settings()
    if not (settings.seed_tg_developer_app_from_env and settings.tg_api_id and settings.tg_api_hash):
        return
    session.add(
        TelegramDeveloperApp(
            app_name="环境默认开发者应用",
            api_id=int(settings.tg_api_id),
            api_hash_ciphertext=encrypt_secret(settings.tg_api_hash),
            health_status=DeveloperAppHealthStatus.HEALTHY.value,
            notes="由 TG_API_ID/TG_API_HASH 初始化",
        )
    )
    session.flush()


def first_assignable_developer_app(session: Session) -> TelegramDeveloperApp | None:
    assigned = _assigned_primary_app(session)
    if assigned:
        return assigned
    if session.scalar(select(func.count(DeveloperAppSlotAssignment.slot_purpose))) > 0:
        return None
    return session.scalar(
        select(TelegramDeveloperApp)
        .where(
            TelegramDeveloperApp.is_active.is_(True),
            TelegramDeveloperApp.health_status == DeveloperAppHealthStatus.HEALTHY.value,
        )
        .order_by(TelegramDeveloperApp.id.asc())
        .limit(1)
    )


def backfill_account_developer_apps(session: Session) -> None:
    app = first_assignable_developer_app(session)
    if not app:
        return
    accounts = list(session.scalars(select(TgAccount).where(TgAccount.developer_app_id.is_(None), TgAccount.deleted_at.is_(None))))
    for account in accounts:
        account.developer_app_id = app.id
        account.developer_app_version = app.credentials_version


def developer_app_snapshot(session: Session, app: TelegramDeveloperApp) -> dict:
    assigned = session.scalar(select(func.count(TgAccount.id)).where(TgAccount.developer_app_id == app.id, TgAccount.deleted_at.is_(None))) or 0
    assigned_ids = _assigned_account_ids(session, app.id)
    pending_ids = _pending_account_ids(session, app.id)
    used = assigned_ids | pending_ids
    assignment = session.scalar(select(DeveloperAppSlotAssignment).where(
        DeveloperAppSlotAssignment.developer_app_id == app.id,
    ))
    available = None if app.max_accounts <= 0 else max(app.max_accounts - len(used), 0)
    return {
        "id": app.id,
        "app_name": app.app_name,
        "api_id": app.api_id,
        "is_active": app.is_active,
        "health_status": app.health_status,
        "max_accounts": app.max_accounts,
        "assigned_accounts": assigned,
        "assigned_distinct_accounts": len(assigned_ids),
        "pending_distinct_accounts": len(pending_ids - assigned_ids),
        "used_distinct_accounts": len(used),
        "capacity_unlimited": app.max_accounts <= 0,
        "available_accounts": available,
        "slot_purpose": assignment.slot_purpose if assignment else "",
        "assignment_status": assignment.status if assignment else "unassigned",
        "assignment_version": assignment.assignment_version if assignment else 0,
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


def update_developer_app_slot_assignments(session: Session, payload, actor: str) -> list[dict]:
    desired = {purpose: int(getattr(payload, key)) for purpose, key in SLOT_PURPOSES.items()}
    if len(set(desired.values())) != len(SLOT_PURPOSES):
        raise ValueError("三种角色必须使用三个不同的开发者应用")
    apps = _require_assignable_apps(session, set(desired.values()))
    rows = list(session.scalars(select(DeveloperAppSlotAssignment).with_for_update()))
    current_version = max((row.assignment_version for row in rows), default=0)
    if current_version != payload.expected_assignment_version:
        raise DeveloperAppAssignmentVersionConflict("开发者应用角色映射版本已变化，请刷新后重试")
    _require_no_active_dr_operations(session, desired, rows)
    _replace_slot_assignments(session, desired, apps, current_version + 1, actor)
    audit(
        session,
        tenant_id=None,
        actor=actor,
        action="配置开发者应用固定角色",
        target_type="developer_app_slot_assignments",
        target_id=str(current_version + 1),
        detail="primary_sv/standby_1_sv/standby_2_my",
    )
    session.commit()
    return list_developer_apps(session)


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
    audit(session, tenant_id=None, actor=actor, action="新增开发者应用", target_type="developer_app", target_id=str(app.id), detail="包含密钥配置")
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
    secret_updated = bool(data.get("api_hash"))
    if secret_updated:
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
    action = "更新开发者应用密钥配置" if secret_updated else "更新开发者应用"
    audit(session, tenant_id=None, actor=actor, action=action, target_type="developer_app", target_id=str(app.id))
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

    fixed_primary = _assigned_primary_app(session)
    if fixed_primary:
        _require_app_capacity(session, fixed_primary)
        account.developer_app_id = fixed_primary.id
        account.developer_app_version = fixed_primary.credentials_version
        fixed_primary.last_assigned_at = _now()
        return fixed_primary
    if session.scalar(select(func.count(DeveloperAppSlotAssignment.slot_purpose))) > 0:
        raise ValueError("固定的硅谷主授权 Developer App 当前不可用")

    apps = session.scalars(
        select(TelegramDeveloperApp).where(
            TelegramDeveloperApp.is_active.is_(True),
            TelegramDeveloperApp.health_status == DeveloperAppHealthStatus.HEALTHY.value,
        )
    ).all()
    candidates: list[tuple[float, int, TelegramDeveloperApp]] = []
    for app in apps:
        assigned = session.scalar(select(func.count(TgAccount.id)).where(TgAccount.developer_app_id == app.id, TgAccount.deleted_at.is_(None))) or 0
        if app.max_accounts > 0 and assigned >= app.max_accounts:
            continue
        assigned_at = app.last_assigned_at
        if assigned_at is None:
            assigned_at = datetime(1970, 1, 1, tzinfo=BEIJING_TZ)
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


def _assigned_primary_app(session: Session) -> TelegramDeveloperApp | None:
    assignment = session.get(DeveloperAppSlotAssignment, "primary_sv")
    if not assignment or assignment.status != "active":
        return None
    app = session.get(TelegramDeveloperApp, assignment.developer_app_id)
    if not app or not app.is_active or app.health_status != DeveloperAppHealthStatus.HEALTHY.value:
        return None
    if app.credentials_version != assignment.credentials_version:
        return None
    return app


def _assigned_account_ids(session: Session, app_id: int) -> set[int]:
    account_ids = set(session.scalars(select(TgAccount.id).where(
        TgAccount.developer_app_id == app_id,
        TgAccount.deleted_at.is_(None),
    )))
    authorization_ids = set(session.scalars(select(TgAccountAuthorization.account_id).join(
        TgAccount, TgAccount.id == TgAccountAuthorization.account_id,
    ).where(
        TgAccountAuthorization.developer_app_id == app_id,
        TgAccountAuthorization.disabled_at.is_(None),
        TgAccount.deleted_at.is_(None),
    )))
    return account_ids | authorization_ids


def _pending_account_ids(session: Session, app_id: int) -> set[int]:
    return set(session.scalars(select(TgAuthorizationDrOperation.account_id).where(
        TgAuthorizationDrOperation.developer_app_id == app_id,
        TgAuthorizationDrOperation.status.not_in(TERMINAL_DR_OPERATION_STATUSES),
    )))


def _require_assignable_apps(session: Session, app_ids: set[int]) -> dict[int, TelegramDeveloperApp]:
    apps = list(session.scalars(select(TelegramDeveloperApp).where(
        TelegramDeveloperApp.id.in_(sorted(app_ids)),
    ).order_by(TelegramDeveloperApp.id).with_for_update()))
    by_id = {app.id: app for app in apps}
    for app_id in app_ids:
        app = by_id.get(app_id)
        if not app or not app.is_active or app.health_status != DeveloperAppHealthStatus.HEALTHY.value:
            raise ValueError(f"开发者应用 {app_id} 不可用于角色映射")
    return by_id


def _require_app_capacity(session: Session, app: TelegramDeveloperApp) -> None:
    if app.max_accounts > 0 and len(_assigned_account_ids(session, app.id)) >= app.max_accounts:
        raise ValueError("硅谷主授权 Developer App 账号名额不足")


def _require_no_active_dr_operations(session: Session, desired: dict, rows: list) -> None:
    current = {row.slot_purpose: row.developer_app_id for row in rows if row.status == "active"}
    if current == desired:
        return
    active = session.scalar(select(func.count(TgAuthorizationDrOperation.id)).where(
        TgAuthorizationDrOperation.status.not_in(TERMINAL_DR_OPERATION_STATUSES),
    ))
    if active:
        raise ValueError("存在进行中的授权灾备操作，不能修改开发者应用角色")


def _replace_slot_assignments(session: Session, desired: dict, apps: dict, version: int, actor: str) -> None:
    existing = list(session.scalars(select(DeveloperAppSlotAssignment).with_for_update()))
    for row in existing:
        session.delete(row)
    session.flush()
    for purpose, app_id in desired.items():
        session.add(DeveloperAppSlotAssignment(
            slot_purpose=purpose,
            developer_app_id=app_id,
            credentials_version=apps[app_id].credentials_version,
            assignment_version=version,
            status="active",
            assigned_by=actor,
            assigned_at=_now(),
        ))


def credentials_for_developer_app(app: TelegramDeveloperApp, proxy: AccountProxy | None = None) -> DeveloperAppCredentials:
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
        **_proxy_credentials(proxy),
    )


def credentials_for_account(
    session: Session,
    account: TgAccount,
    *,
    assign_if_missing: bool = False,
    use_proxy: bool = False,
) -> DeveloperAppCredentials:
    if account.deleted_at is not None:
        raise ValueError("账号已删除")
    app = assign_developer_app_round_robin(session, account) if assign_if_missing or not account.developer_app_id else session.get(TelegramDeveloperApp, account.developer_app_id)
    if not app:
        raise ValueError("账号未绑定开发者应用")
    if app.credentials_version > account.developer_app_version:
        account.status = AccountStatus.NEED_RELOGIN.value
        raise ValueError("开发者应用凭证已轮换，账号需要重新登录")
    return credentials_for_developer_app(app, account.proxy if use_proxy else None)


def credentials_for_authorization(
    session: Session,
    authorization,
) -> DeveloperAppCredentials:
    app = session.get(TelegramDeveloperApp, authorization.developer_app_id)
    if not app:
        raise ValueError("授权未绑定开发者应用")
    if authorization.developer_app_api_id_snapshot not in {0, app.api_id}:
        raise ValueError("授权的开发者应用已发生变化")
    proxy = session.get(AccountProxy, authorization.proxy_id) if authorization.proxy_id else None
    return credentials_for_developer_app(app, proxy)


def credentials_for_task_account(session: Session, account: TgAccount, _task_type: str | None) -> DeveloperAppCredentials:
    return credentials_for_account(session, account)


def _proxy_credentials(proxy: AccountProxy | None) -> dict:
    if proxy is None:
        return {}
    return {
        "proxy_id": proxy.id,
        "proxy_protocol": proxy.protocol,
        "proxy_host": proxy.host,
        "proxy_port": proxy.port,
        "proxy_username": proxy.username,
        "proxy_password": decrypt_secret(proxy.password_ciphertext) if proxy.password_ciphertext else "",
    }


__all__ = [
    "DeveloperAppAssignmentVersionConflict",
    "assign_developer_app_round_robin",
    "backfill_account_developer_apps",
    "check_developer_app",
    "create_developer_app",
    "credentials_for_account",
    "credentials_for_authorization",
    "credentials_for_developer_app",
    "credentials_for_task_account",
    "developer_app_snapshot",
    "first_assignable_developer_app",
    "list_developer_apps",
    "update_developer_app_slot_assignments",
    "seed_developer_apps",
    "set_developer_app_active",
    "update_developer_app",
]
