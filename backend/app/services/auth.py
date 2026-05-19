from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AccountPool, Tenant
from app.schemas import TenantCreate

from ._common import audit
from .account_pools import seed_account_pools
from .ai_config import seed_ai_configuration
from .developer_apps import backfill_account_developer_apps, seed_developer_apps


def ensure_seed_data(session: Session) -> None:
    if session.scalar(select(func.count(Tenant.id))) > 0:
        seed_developer_apps(session)
        seed_ai_configuration(session)
        seed_account_pools(session)
        backfill_account_developer_apps(session)
        session.commit()
        return

    tenant = Tenant(name="默认运营空间", plan_name="单空间", account_quota=0, task_quota=5000)
    session.add(tenant)
    session.flush()
    session.add(AccountPool(tenant_id=tenant.id, name="默认账号池", description="系统默认账号分组", is_default=True))
    seed_developer_apps(session)
    seed_ai_configuration(session)
    audit(session, tenant_id=tenant.id, actor="system", action="初始化本地工作区", target_type="tenant", target_id=str(tenant.id))
    session.commit()


def create_tenant(session: Session, payload: TenantCreate) -> Tenant:
    tenant = Tenant(**payload.model_dump(exclude={"account_quota"}), account_quota=0)
    session.add(tenant)
    session.flush()
    seed_ai_configuration(session)
    audit(session, tenant_id=tenant.id, actor="系统管理员", action="创建运营空间", target_type="tenant", target_id=str(tenant.id))
    session.commit()
    session.refresh(tenant)
    return tenant


__all__ = ["create_tenant", "ensure_seed_data"]
