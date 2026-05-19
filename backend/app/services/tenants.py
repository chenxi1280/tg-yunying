from __future__ import annotations
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import MessageTask, Tenant, TgAccount
from app.schemas import TenantNotificationSettingsUpdate, TenantUpdate
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
    }


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
    "tenant_usage_snapshot",
    "notification_settings_payload",
    "update_tenant",
    "update_tenant_notification_settings",
]
