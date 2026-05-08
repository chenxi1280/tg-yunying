from __future__ import annotations
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import MessageTask, Tenant, TgAccount
from app.schemas import TenantUpdate

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
    usage = tenant_usage_snapshot(session, tenant_id)
    if usage["accounts_used"] + increment > tenant.account_quota:
        raise ValueError(
            f"账号配额不足：当前已用 {usage['accounts_used']} / {tenant.account_quota}，本次需新增 {increment} 个账号"
        )


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
        if isinstance(value, str):
            value = value.strip()
        setattr(tenant, key, value)
    usage = tenant_usage_snapshot(session, tenant.id)
    if usage["accounts_used"] > tenant.account_quota:
        raise ValueError(
            f"账号配额不能低于已用数量：当前已用 {usage['accounts_used']}，目标配额 {tenant.account_quota}"
        )
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

__all__ = [
    "create_tenant",
    "ensure_account_quota_available",
    "ensure_task_quota_available",
    "tenant_usage_snapshot",
    "update_tenant",
]
