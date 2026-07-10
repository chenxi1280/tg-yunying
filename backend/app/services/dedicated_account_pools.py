from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AccountPool, Tenant

from ._common import audit
from .account_usage_policy import DEDICATED_ACCOUNT_USAGES, VALID_ACCOUNT_USAGES

CODE_RECEIVER_POOL_KEY = "code_receiver"
RANK_DEBOOST_POOL_KEY = "rank_deboost"
RANK_DEBOOST_SYSTEM_POOL_NAME = "降权任务专用分组"


def ensure_code_receiver_account_pool(session: Session, tenant_id: int) -> AccountPool:
    pool = session.scalar(
        select(AccountPool)
        .where(
            AccountPool.tenant_id == tenant_id,
            AccountPool.system_key == CODE_RECEIVER_POOL_KEY,
        )
        .order_by(AccountPool.id.asc())
    )
    if pool is None:
        pool = _new_code_receiver_pool(tenant_id)
        session.add(pool)
        session.flush()
    _mark_system_pool(pool, CODE_RECEIVER_POOL_KEY)
    return pool


def ensure_rank_deboost_account_pool(session: Session, tenant_id: int) -> AccountPool:
    _lock_tenant(session, tenant_id)
    pools = list(
        session.scalars(
            select(AccountPool)
            .where(
                AccountPool.tenant_id == tenant_id,
                AccountPool.system_key == RANK_DEBOOST_POOL_KEY,
            )
            .order_by(AccountPool.id.asc())
            .with_for_update()
        )
    )
    if len(pools) > 1:
        raise ValueError("同租户最多一个 rank_deboost 系统默认组")
    pool = pools[0] if pools else _create_system_rank_pool(session, tenant_id)
    _mark_system_pool(pool, RANK_DEBOOST_POOL_KEY)
    return pool


def create_rank_deboost_account_pool(
    session: Session,
    *,
    tenant_id: int,
    name: str,
    description: str = "",
    actor: str = "",
) -> AccountPool:
    try:
        _lock_tenant(session, tenant_id)
        pool_name = (name or RANK_DEBOOST_SYSTEM_POOL_NAME).strip()
        assert_unique_account_pool_name(session, tenant_id, pool_name)
        pool = AccountPool(
            tenant_id=tenant_id,
            name=pool_name,
            description=description.strip(),
            pool_purpose=RANK_DEBOOST_POOL_KEY,
            is_system=False,
            system_key="",
        )
        session.add(pool)
        session.flush()
        _audit_rank_pool_creation(session, pool, actor)
        session.commit()
        session.refresh(pool)
        return pool
    except IntegrityError as exc:
        _raise_pool_name_conflict(session, exc)


def validate_account_pool_admission(pool: AccountPool) -> None:
    if not pool.is_enabled:
        raise ValueError("account pool disabled")
    purpose = str(pool.pool_purpose or "")
    system_key = str(pool.system_key or "")
    if purpose not in VALID_ACCOUNT_USAGES:
        raise ValueError("invalid account pool purpose")
    if system_key in DEDICATED_ACCOUNT_USAGES and system_key != purpose:
        raise ValueError("account_purpose_mismatch")
    if purpose in DEDICATED_ACCOUNT_USAGES and system_key not in {"", purpose}:
        raise ValueError("account_purpose_mismatch")


def is_code_receiver_pool(pool: AccountPool | None) -> bool:
    if pool is None:
        return False
    return pool.pool_purpose == CODE_RECEIVER_POOL_KEY or pool.system_key == CODE_RECEIVER_POOL_KEY


def is_rank_deboost_pool(pool: AccountPool | None) -> bool:
    if pool is None:
        return False
    return pool.pool_purpose == RANK_DEBOOST_POOL_KEY or pool.system_key == RANK_DEBOOST_POOL_KEY


def _new_code_receiver_pool(tenant_id: int) -> AccountPool:
    return AccountPool(
        tenant_id=tenant_id,
        name="接码专用分组",
        description="系统固定接码分组",
        pool_purpose=CODE_RECEIVER_POOL_KEY,
        is_system=True,
        system_key=CODE_RECEIVER_POOL_KEY,
    )


def _create_system_rank_pool(session: Session, tenant_id: int) -> AccountPool:
    pool = AccountPool(
        tenant_id=tenant_id,
        name=_available_system_pool_name(session, tenant_id),
        description="系统固定降权任务专用分组",
        pool_purpose=RANK_DEBOOST_POOL_KEY,
        is_system=True,
        system_key=RANK_DEBOOST_POOL_KEY,
    )
    session.add(pool)
    session.flush()
    return pool


def _mark_system_pool(pool: AccountPool, purpose: str) -> None:
    pool.pool_purpose = purpose
    pool.is_system = True
    pool.system_key = purpose


def _lock_tenant(session: Session, tenant_id: int) -> Tenant:
    tenant = session.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    if tenant is None:
        raise ValueError("tenant not found")
    return tenant


def _available_system_pool_name(session: Session, tenant_id: int) -> str:
    existing = set(session.scalars(select(AccountPool.name).where(AccountPool.tenant_id == tenant_id)))
    if RANK_DEBOOST_SYSTEM_POOL_NAME not in existing:
        return RANK_DEBOOST_SYSTEM_POOL_NAME
    suffix = 2
    while f"{RANK_DEBOOST_SYSTEM_POOL_NAME}-{suffix}" in existing:
        suffix += 1
    return f"{RANK_DEBOOST_SYSTEM_POOL_NAME}-{suffix}"


def assert_unique_account_pool_name(
    session: Session,
    tenant_id: int,
    name: str,
    *,
    exclude_pool_id: int | None = None,
) -> None:
    if not name:
        raise ValueError("account pool name is required")
    stmt = select(AccountPool.id).where(
        AccountPool.tenant_id == tenant_id,
        AccountPool.name == name,
    )
    if exclude_pool_id is not None:
        stmt = stmt.where(AccountPool.id != exclude_pool_id)
    existing_id = session.scalar(stmt)
    if existing_id:
        raise ValueError("同租户账号组名称必须唯一")


def _raise_pool_name_conflict(session: Session, exc: IntegrityError):
    session.rollback()
    raise ValueError("同租户账号组名称必须唯一") from exc


def _audit_rank_pool_creation(session: Session, pool: AccountPool, actor: str) -> None:
    audit(
        session,
        tenant_id=pool.tenant_id,
        actor=actor,
        action="新增降权任务专用分组",
        target_type="account_pool",
        target_id=str(pool.id),
    )


__all__ = [
    "CODE_RECEIVER_POOL_KEY",
    "RANK_DEBOOST_POOL_KEY",
    "assert_unique_account_pool_name",
    "create_rank_deboost_account_pool",
    "ensure_code_receiver_account_pool",
    "ensure_rank_deboost_account_pool",
    "is_code_receiver_pool",
    "is_rank_deboost_pool",
    "validate_account_pool_admission",
]
