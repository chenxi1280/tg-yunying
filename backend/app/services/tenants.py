from __future__ import annotations
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AccountStatus, MessageTask, Tenant, TgAccount
from app.schemas import TenantGroupRescueSettingsUpdate, TenantNotificationSettingsUpdate, TenantUpdate
from app.security import encrypt_secret

from ._common import audit
from .auth import create_tenant


def tenant_usage_snapshot(session: Session, tenant_id: int) -> dict[str, int]:
    account_used = session.scalar(select(func.count(TgAccount.id)).where(TgAccount.tenant_id == tenant_id, TgAccount.deleted_at.is_(None))) or 0
    task_used = session.scalar(select(func.count(MessageTask.id)).where(MessageTask.tenant_id == tenant_id)) or 0
    return {"accounts_used": int(account_used), "tasks_used": int(task_used)}


def ensure_account_quota_available(session: Session, tenant_id: int, increment: int = 1) -> None:
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise ValueError("tenant not found")
    return


def ensure_task_quota_available(session: Session, tenant_id: int, increment: int = 1) -> None:
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise ValueError("tenant not found")
    usage = tenant_usage_snapshot(session, tenant_id)
    if usage["tasks_used"] + increment > tenant.task_quota:
        raise ValueError(
            f"任务配额不足：当前已用 {usage['tasks_used']} / {tenant.task_quota}，本次需新增 {increment} 条任务"
        )


def update_tenant(session: Session, tenant_id: int, payload: TenantUpdate, actor: str) -> Tenant:
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise ValueError("tenant not found")
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        if key in {"id", "created_at", "updated_at"}:
            continue
        if key == "account_quota":
            value = 0
        if isinstance(value, str):
            value = value.strip()
        setattr(tenant, key, value)
    usage = tenant_usage_snapshot(session, tenant.id)
    if usage["tasks_used"] > tenant.task_quota:
        raise ValueError(
            f"任务配额不能低于已用数量：当前已用 {usage['tasks_used']}，目标配额 {tenant.task_quota}"
        )
    audit(
        session,
        tenant_id=tenant.id,
        actor=actor,
        action="更新租户配额",
        target_type="tenant",
        target_id=str(tenant.id),
        detail=f"plan={tenant.plan_name}; accounts={tenant.account_quota}; tasks={tenant.task_quota}",
    )
    session.commit()
    session.refresh(tenant)
    return tenant


def notification_settings_payload(tenant: Tenant) -> dict:
    return {
        "tenant_id": tenant.id,
        "notify_ai_failures_enabled": tenant.notify_ai_failures_enabled,
        "admin_chat_id": tenant.admin_chat_id,
        "telegram_bot_configured": tenant.telegram_bot_configured,
        "ai_group_bot_enabled": bool(tenant.ai_group_bot_enabled),
        "telegram_bot_webhook_status": tenant.telegram_bot_webhook_status,
        "telegram_bot_last_error": tenant.telegram_bot_last_error,
        **group_rescue_settings_payload(tenant, None),
    }


def group_rescue_settings_payload(tenant: Tenant, session: Session | None) -> dict:
    account_payload = None
    account_id = tenant.group_rescue_admin_account_id
    if session is not None and account_id:
        account = session.get(TgAccount, account_id)
        if account and account.tenant_id == tenant.id and account.deleted_at is None:
            account_payload = {
                "id": account.id,
                "display_name": account.display_name,
                "username": account.username,
                "status": account.status,
            }
    return {
        "group_rescue_enabled": bool(tenant.group_rescue_enabled),
        "group_rescue_admin_account_id": account_id,
        "group_rescue_admin_account": account_payload,
    }


def update_group_rescue_settings(
    session: Session,
    tenant_id: int,
    payload: TenantGroupRescueSettingsUpdate,
    actor: str,
) -> dict:
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise ValueError("tenant not found")
    data = payload.model_dump(exclude_unset=True)
    enabled = bool(data.get("group_rescue_enabled", tenant.group_rescue_enabled))
    account_id = data.get("group_rescue_admin_account_id", tenant.group_rescue_admin_account_id)
    if enabled:
        _validate_group_rescue_account(session, tenant_id, account_id)
    tenant.group_rescue_enabled = enabled
    tenant.group_rescue_admin_account_id = account_id
    audit(session, tenant_id=tenant.id, actor=actor, action="更新群聊救援配置", target_type="tenant", target_id=str(tenant.id), detail=f"enabled={enabled}; account={account_id}")
    session.commit()
    session.refresh(tenant)
    return group_rescue_settings_payload(tenant, session)


def _validate_group_rescue_account(session: Session, tenant_id: int, account_id: int | None) -> TgAccount:
    if not account_id:
        raise ValueError("救援管理员账号必填")
    account = session.get(TgAccount, int(account_id))
    if not account or account.tenant_id != tenant_id or account.deleted_at is not None:
        raise ValueError("救援管理员账号不存在")
    if account.status != AccountStatus.ACTIVE.value:
        raise ValueError("救援管理员账号必须是在线账号")
    if not account.session_ciphertext:
        raise ValueError("救援管理员账号必须有可用 session")
    return account


def update_tenant_notification_settings(
    session: Session,
    tenant_id: int,
    payload: TenantNotificationSettingsUpdate,
    actor: str,
) -> dict:
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise ValueError("tenant not found")
    data = payload.model_dump(exclude_unset=True)
    if "notify_ai_failures_enabled" in data and data["notify_ai_failures_enabled"] is not None:
        tenant.notify_ai_failures_enabled = bool(data["notify_ai_failures_enabled"])
    if "admin_chat_id" in data and data["admin_chat_id"] is not None:
        tenant.admin_chat_id = data["admin_chat_id"].strip()
    if data.get("telegram_bot_token"):
        tenant.telegram_bot_token_ciphertext = encrypt_secret(data["telegram_bot_token"].strip())
    audit(
        session,
        tenant_id=tenant.id,
        actor=actor,
        action="更新AI失败通知配置",
        target_type="tenant",
        target_id=str(tenant.id),
        detail=f"enabled={tenant.notify_ai_failures_enabled}; chat={bool(tenant.admin_chat_id)}; bot={tenant.telegram_bot_configured}",
    )
    session.commit()
    session.refresh(tenant)
    return notification_settings_payload(tenant)

__all__ = [
    "create_tenant",
    "ensure_account_quota_available",
    "ensure_task_quota_available",
    "group_rescue_settings_payload",
    "tenant_usage_snapshot",
    "notification_settings_payload",
    "update_tenant",
    "update_group_rescue_settings",
    "update_tenant_notification_settings",
]
