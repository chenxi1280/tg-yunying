from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountPool,
    AccountStatus,
    MessageTask,
    Tenant,
    TgAccount,
    TgContact,
    VerificationTask,
)
from app.schemas import AccountPoolCreate, AccountPoolUpdate

from ._common import _now, audit, require_tenant

__all__ = [
    "account_pool_contacts",
    "account_pool_detail",
    "account_pool_snapshot",
    "create_account_pool",
    "ensure_default_account_pool",
    "list_account_pools",
    "move_account_pool",
    "seed_account_pools",
    "update_account_pool",
]


def account_pool_snapshot(session: Session, pool: AccountPool) -> dict:
    return {
        "id": pool.id,
        "tenant_id": pool.tenant_id,
        "name": pool.name,
        "description": pool.description,
        "is_default": pool.is_default,
        "account_count": session.scalar(select(func.count(TgAccount.id)).where(TgAccount.pool_id == pool.id)) or 0,
        "created_at": pool.created_at,
        "updated_at": pool.updated_at,
    }


def ensure_default_account_pool(session: Session, tenant_id: int) -> AccountPool:
    pool = session.scalar(
        select(AccountPool)
        .where(AccountPool.tenant_id == tenant_id, AccountPool.is_default.is_(True))
        .order_by(AccountPool.id.asc())
    )
    if not pool:
        pool = session.scalar(select(AccountPool).where(AccountPool.tenant_id == tenant_id).order_by(AccountPool.id.asc()))
    if not pool:
        pool = AccountPool(tenant_id=tenant_id, name="默认账号池", description="系统默认账号分组", is_default=True)
        session.add(pool)
        session.flush()
    return pool


def seed_account_pools(session: Session) -> None:
    for tenant_id in session.scalars(select(Tenant.id)).all():
        pool = ensure_default_account_pool(session, tenant_id)
        accounts = session.scalars(select(TgAccount).where(TgAccount.tenant_id == tenant_id, TgAccount.pool_id.is_(None))).all()
        for account in accounts:
            account.pool_id = pool.id


def list_account_pools(session: Session, tenant_id: int) -> list[dict]:
    require_tenant(session, tenant_id)
    seed_account_pools(session)
    session.flush()
    pools = session.scalars(select(AccountPool).where(AccountPool.tenant_id == tenant_id).order_by(AccountPool.is_default.desc(), AccountPool.id.asc())).all()
    return [account_pool_snapshot(session, pool) for pool in pools]


def account_pool_contacts(session: Session, pool_id: int, limit: int = 300) -> list[TgContact]:
    pool = session.get(AccountPool, pool_id)
    if not pool:
        raise ValueError("account pool not found")
    account_ids = select(TgAccount.id).where(TgAccount.tenant_id == pool.tenant_id, TgAccount.pool_id == pool.id)
    return list(
        session.scalars(
            select(TgContact)
            .where(TgContact.tenant_id == pool.tenant_id, TgContact.account_id.in_(account_ids))
            .order_by(TgContact.last_synced_at.desc(), TgContact.id.desc())
            .limit(limit)
        )
    )


def account_pool_detail(session: Session, pool_id: int) -> dict:
    pool = session.get(AccountPool, pool_id)
    if not pool:
        raise ValueError("account pool not found")
    accounts = list(
        session.scalars(
            select(TgAccount)
            .where(TgAccount.tenant_id == pool.tenant_id, TgAccount.pool_id == pool.id)
            .order_by(TgAccount.id.desc())
        )
    )
    account_ids = [account.id for account in accounts]
    contacts = account_pool_contacts(session, pool.id)
    verification_tasks = list(
        session.scalars(
            select(VerificationTask)
            .where(VerificationTask.tenant_id == pool.tenant_id, VerificationTask.account_id.in_(account_ids) if account_ids else False)
            .order_by(VerificationTask.id.desc())
            .limit(30)
        )
    )
    from .cloning import account_clone_plans

    clone_plans = account_clone_plans(session, pool.tenant_id, limit=20)
    clone_plans = [
        plan for plan in clone_plans
        if plan["source_account_id"] in account_ids or any(target_id in account_ids for target_id in plan.get("target_account_ids", []))
    ]
    message_records = list(
        session.scalars(
            select(MessageTask)
            .where(MessageTask.tenant_id == pool.tenant_id, MessageTask.account_id.in_(account_ids) if account_ids else False)
            .order_by(MessageTask.id.desc())
            .limit(50)
        )
    )
    return {
        "pool": account_pool_snapshot(session, pool),
        "accounts": accounts,
        "contacts": contacts,
        "verification_tasks": verification_tasks,
        "clone_plans": clone_plans,
        "message_records": message_records,
        "stats": {
            "accounts": len(accounts),
            "online": sum(1 for account in accounts if account.status == AccountStatus.ACTIVE.value),
            "contacts": len(contacts),
            "verification_tasks": len(verification_tasks),
            "clone_plans": len(clone_plans),
            "message_records": len(message_records),
        },
    }


def create_account_pool(session: Session, payload: AccountPoolCreate, actor: str) -> dict:
    require_tenant(session, payload.tenant_id)
    if payload.is_default:
        for pool in session.scalars(select(AccountPool).where(AccountPool.tenant_id == payload.tenant_id)):
            pool.is_default = False
    pool = AccountPool(
        tenant_id=payload.tenant_id,
        name=payload.name.strip(),
        description=payload.description.strip(),
        is_default=payload.is_default,
    )
    if not pool.name:
        raise ValueError("account pool name is required")
    session.add(pool)
    session.flush()
    if not session.scalar(select(AccountPool.id).where(AccountPool.tenant_id == payload.tenant_id, AccountPool.is_default.is_(True), AccountPool.id != pool.id)):
        pool.is_default = True
    audit(session, tenant_id=pool.tenant_id, actor=actor, action="新增账号池", target_type="account_pool", target_id=str(pool.id))
    session.commit()
    session.refresh(pool)
    return account_pool_snapshot(session, pool)


def update_account_pool(session: Session, pool_id: int, payload: AccountPoolUpdate, actor: str) -> dict:
    pool = session.get(AccountPool, pool_id)
    if not pool:
        raise ValueError("account pool not found")
    data = payload.model_dump(exclude_unset=True)
    if data.get("name") is not None:
        pool.name = data["name"].strip()
    if data.get("description") is not None:
        pool.description = data["description"].strip()
    if data.get("is_default") is not None:
        pool.is_default = bool(data["is_default"])
        if pool.is_default:
            for other in session.scalars(select(AccountPool).where(AccountPool.tenant_id == pool.tenant_id, AccountPool.id != pool.id)):
                other.is_default = False
    pool.updated_at = _now()
    audit(session, tenant_id=pool.tenant_id, actor=actor, action="更新账号池", target_type="account_pool", target_id=str(pool.id))
    session.commit()
    session.refresh(pool)
    return account_pool_snapshot(session, pool)


def move_account_pool(session: Session, account_id: int, pool_id: int, actor: str) -> TgAccount:
    account = session.get(TgAccount, account_id)
    pool = session.get(AccountPool, pool_id)
    if not account or not pool or account.tenant_id != pool.tenant_id:
        raise ValueError("account or pool not found")
    account.pool_id = pool.id
    audit(session, tenant_id=account.tenant_id, actor=actor, action="移动账号池", target_type="tg_account", target_id=str(account.id), detail=pool.name)
    session.commit()
    session.refresh(account)
    return account
