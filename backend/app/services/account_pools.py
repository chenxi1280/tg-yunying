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
    "assert_account_not_in_rank_deboost_conflict",
    "create_account_pool",
    "create_rank_deboost_account_pool",
    "delete_account_pool",
    "ensure_code_receiver_account_pool",
    "ensure_default_account_pool",
    "ensure_rank_deboost_account_pool",
    "list_account_pools",
    "move_account_pool",
    "seed_account_pools",
    "set_account_identity",
    "update_account_pool",
]

CODE_RECEIVER_POOL_KEY = "code_receiver"
RANK_DEBOOST_POOL_KEY = "rank_deboost"


def account_pool_snapshot(session: Session, pool: AccountPool) -> dict:
    return {
        "id": pool.id,
        "tenant_id": pool.tenant_id,
        "name": pool.name,
        "description": pool.description,
        "is_default": pool.is_default,
        "pool_purpose": pool.pool_purpose,
        "is_system": pool.is_system,
        "system_key": pool.system_key,
        "is_enabled": pool.is_enabled,
        "disabled_at": pool.disabled_at,
        "disabled_by": pool.disabled_by,
        "disable_reason": pool.disable_reason,
        "account_count": session.scalar(select(func.count(TgAccount.id)).where(TgAccount.pool_id == pool.id, TgAccount.deleted_at.is_(None))) or 0,
        "created_at": pool.created_at,
        "updated_at": pool.updated_at,
    }


def ensure_default_account_pool(session: Session, tenant_id: int) -> AccountPool:
    pool = session.scalar(
        select(AccountPool)
        .where(
            AccountPool.tenant_id == tenant_id,
            AccountPool.is_default.is_(True),
            AccountPool.pool_purpose != CODE_RECEIVER_POOL_KEY,
            AccountPool.system_key != CODE_RECEIVER_POOL_KEY,
            AccountPool.pool_purpose != RANK_DEBOOST_POOL_KEY,
            AccountPool.system_key != RANK_DEBOOST_POOL_KEY,
        )
        .order_by(AccountPool.id.asc())
    )
    if not pool:
        pool = session.scalar(
            select(AccountPool)
            .where(
                AccountPool.tenant_id == tenant_id,
                AccountPool.pool_purpose != CODE_RECEIVER_POOL_KEY,
                AccountPool.system_key != CODE_RECEIVER_POOL_KEY,
                AccountPool.pool_purpose != RANK_DEBOOST_POOL_KEY,
                AccountPool.system_key != RANK_DEBOOST_POOL_KEY,
            )
            .order_by(AccountPool.id.asc())
        )
    if not pool:
        pool = AccountPool(tenant_id=tenant_id, name="默认账号池", description="系统默认账号分组", is_default=True)
        session.add(pool)
        session.flush()
    return pool


def ensure_code_receiver_account_pool(session: Session, tenant_id: int) -> AccountPool:
    pool = session.scalar(
        select(AccountPool)
        .where(
            AccountPool.tenant_id == tenant_id,
            AccountPool.system_key == CODE_RECEIVER_POOL_KEY,
        )
        .order_by(AccountPool.id.asc())
    )
    if not pool:
        pool = _create_code_receiver_pool(tenant_id)
        session.add(pool)
        session.flush()
    _mark_code_receiver_pool(pool)
    return pool


def _create_code_receiver_pool(tenant_id: int) -> AccountPool:
    return AccountPool(
        tenant_id=tenant_id,
        name="接码专用分组",
        description="系统固定接码分组",
        pool_purpose=CODE_RECEIVER_POOL_KEY,
        is_system=True,
        system_key=CODE_RECEIVER_POOL_KEY,
    )


def _mark_code_receiver_pool(pool: AccountPool) -> None:
    pool.pool_purpose = CODE_RECEIVER_POOL_KEY
    pool.is_system = True
    pool.system_key = CODE_RECEIVER_POOL_KEY


def _is_code_receiver_pool(pool: AccountPool) -> bool:
    return pool.pool_purpose == CODE_RECEIVER_POOL_KEY or pool.system_key == CODE_RECEIVER_POOL_KEY


def ensure_rank_deboost_account_pool(session: Session, tenant_id: int) -> AccountPool:
    """确保租户存在系统级降权任务专用分组（与接码专用分组并列）。

    系统级分组 is_system=True、system_key=rank_deboost，不可删除。
    """
    pool = session.scalar(
        select(AccountPool)
        .where(
            AccountPool.tenant_id == tenant_id,
            AccountPool.system_key == RANK_DEBOOST_POOL_KEY,
        )
        .order_by(AccountPool.id.asc())
    )
    if not pool:
        pool = AccountPool(
            tenant_id=tenant_id,
            name="降权任务专用分组",
            description="系统固定降权任务专用分组",
            pool_purpose=RANK_DEBOOST_POOL_KEY,
            is_system=True,
            system_key=RANK_DEBOOST_POOL_KEY,
        )
        session.add(pool)
        session.flush()
    pool.pool_purpose = RANK_DEBOOST_POOL_KEY
    pool.is_system = True
    pool.system_key = RANK_DEBOOST_POOL_KEY
    return pool


def _is_rank_deboost_pool(pool: AccountPool | None) -> bool:
    if pool is None:
        return False
    return pool.pool_purpose == RANK_DEBOOST_POOL_KEY or pool.system_key == RANK_DEBOOST_POOL_KEY


def _is_normal_pool(pool: AccountPool | None) -> bool:
    """普通分组：非空、非 code_receiver、非 rank_deboost。

    pool_purpose IS NULL 视为普通（兼容历史数据）。未分组（pool is None）不算普通分组，
    允许未分组账号移入 rank_deboost 分组。
    """
    if pool is None:
        return False
    return not _is_code_receiver_pool(pool) and not _is_rank_deboost_pool(pool)


def create_rank_deboost_account_pool(
    session: Session,
    *,
    tenant_id: int,
    name: str,
    description: str = "",
    actor: str = "",
) -> AccountPool:
    """创建降权任务专用分组（非系统级，可禁用但不可删除）。

    与接码专用分组并列；pool_purpose='rank_deboost' 由代码层校验（模型保持字符串字段）。
    """
    require_tenant(session, tenant_id)
    pool_name = (name or "降权任务专用分组").strip()
    if not pool_name:
        raise ValueError("account pool name is required")
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
    audit(
        session,
        tenant_id=pool.tenant_id,
        actor=actor,
        action="新增降权任务专用分组",
        target_type="account_pool",
        target_id=str(pool.id),
    )
    session.commit()
    session.refresh(pool)
    return pool


def delete_account_pool(session: Session, pool_id: int, actor: str) -> None:
    """删除账号分组。

    rank_deboost 和 code_receiver 分组不可删除，只能禁用（通过 update_account_pool
    设置 is_default=False）。系统级分组（is_system=True）同样不可删除。
    """
    pool = session.get(AccountPool, pool_id)
    if not pool:
        raise ValueError("account pool not found")
    if _is_rank_deboost_pool(pool):
        raise ValueError("rank_deboost 分组不可删除，只能禁用")
    if _is_code_receiver_pool(pool):
        raise ValueError("code_receiver 分组不可删除，只能禁用")
    if pool.is_system:
        raise ValueError("系统级分组不可删除")
    # 拒绝删除非空分组，避免账号孤儿
    account_count = session.scalar(
        select(func.count(TgAccount.id)).where(
            TgAccount.tenant_id == pool.tenant_id,
            TgAccount.pool_id == pool.id,
            TgAccount.deleted_at.is_(None),
        )
    ) or 0
    if account_count > 0:
        raise ValueError(f"分组非空（{account_count} 个账号），请先迁移账号再删除")
    audit(
        session,
        tenant_id=pool.tenant_id,
        actor=actor,
        action="删除账号池",
        target_type="account_pool",
        target_id=str(pool.id),
    )
    session.delete(pool)
    session.commit()


def assert_account_not_in_rank_deboost_conflict(
    session: Session,
    account: TgAccount,
) -> None:
    """数据一致性校验：账号的 account_identity 与 pool_purpose 必须一致。

    若账号 account_identity='rank_deboost' 但 pool_id 指向普通分组，或
    账号在 rank_deboost 分组但 account_identity 不为 'rank_deboost'，视为
    「同账号同时存在于 rank_deboost 和普通分组」的数据异常，raise ValueError。
    """
    pool = session.get(AccountPool, account.pool_id) if account.pool_id else None
    in_rank_deboost_pool = _is_rank_deboost_pool(pool)
    identity_is_rank_deboost = account.account_identity == RANK_DEBOOST_POOL_KEY
    if in_rank_deboost_pool and not identity_is_rank_deboost:
        raise ValueError(
            "rank_deboost 分组内账号不得同时存在于普通分组"
        )
    if identity_is_rank_deboost and not in_rank_deboost_pool:
        raise ValueError(
            "rank_deboost 分组内账号不得同时存在于普通分组"
        )


def seed_account_pools(session: Session) -> None:
    for tenant_id in session.scalars(select(Tenant.id)).all():
        pool = ensure_default_account_pool(session, tenant_id)
        ensure_code_receiver_account_pool(session, tenant_id)
        accounts = session.scalars(select(TgAccount).where(TgAccount.tenant_id == tenant_id, TgAccount.pool_id.is_(None), TgAccount.deleted_at.is_(None))).all()
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
    account_ids = select(TgAccount.id).where(TgAccount.tenant_id == pool.tenant_id, TgAccount.pool_id == pool.id, TgAccount.deleted_at.is_(None))
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
            .where(TgAccount.tenant_id == pool.tenant_id, TgAccount.pool_id == pool.id, TgAccount.deleted_at.is_(None))
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
    _apply_pool_enabled_state(pool, payload.is_enabled, actor, "")
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
    if data.get("is_enabled") is not None:
        _apply_pool_enabled_state(
            pool,
            bool(data["is_enabled"]),
            actor,
            str(data.get("disable_reason") or ""),
        )
    pool.updated_at = _now()
    audit(session, tenant_id=pool.tenant_id, actor=actor, action="更新账号池", target_type="account_pool", target_id=str(pool.id))
    session.commit()
    session.refresh(pool)
    return account_pool_snapshot(session, pool)


def _apply_pool_enabled_state(pool: AccountPool, is_enabled: bool, actor: str, reason: str) -> None:
    pool.is_enabled = is_enabled
    if is_enabled:
        pool.disabled_at = None
        pool.disabled_by = ""
        pool.disable_reason = ""
        return
    pool.disabled_at = _now()
    pool.disabled_by = actor
    pool.disable_reason = reason.strip()


def move_account_pool(session: Session, account_id: int, pool_id: int, actor: str) -> TgAccount:
    account = session.get(TgAccount, account_id)
    pool = session.get(AccountPool, pool_id)
    if not account or account.deleted_at is not None or not pool or account.tenant_id != pool.tenant_id:
        raise ValueError("account or pool not found")
    # rank_deboost 隔离硬校验：
    # - 移动到 rank_deboost 分组时，账号当前不得在普通分组（pool_purpose='normal' 或 IS NULL）
    # - 移动到普通分组时，账号当前不得在 rank_deboost 分组
    current_pool = session.get(AccountPool, account.pool_id) if account.pool_id else None
    target_is_rank_deboost = _is_rank_deboost_pool(pool)
    current_is_rank_deboost = _is_rank_deboost_pool(current_pool)
    current_is_normal = _is_normal_pool(current_pool)
    if target_is_rank_deboost and current_is_normal:
        raise ValueError("rank_deboost 分组内账号不得同时存在于普通分组")
    if not target_is_rank_deboost and current_is_rank_deboost:
        raise ValueError("rank_deboost 分组内账号不得同时存在于普通分组")
    account.pool_id = pool.id
    if target_is_rank_deboost:
        account.account_identity = RANK_DEBOOST_POOL_KEY
    elif _is_code_receiver_pool(pool):
        account.account_identity = CODE_RECEIVER_POOL_KEY
    else:
        account.account_identity = "normal"
    audit(session, tenant_id=account.tenant_id, actor=actor, action="移动账号池", target_type="tg_account", target_id=str(account.id), detail=pool.name)
    session.commit()
    session.refresh(account)
    return account


def set_account_identity(session: Session, account_id: int, identity: str, actor: str) -> TgAccount:
    if identity not in {"normal", CODE_RECEIVER_POOL_KEY}:
        raise ValueError("unsupported account identity")
    account = session.get(TgAccount, account_id)
    if not account or account.deleted_at is not None:
        raise ValueError("account not found")
    target_pool = _identity_target_pool(session, account, identity)
    account.account_identity = identity
    account.pool_id = target_pool.id
    audit(session, tenant_id=account.tenant_id, actor=actor, action="设置账号身份", target_type="tg_account", target_id=str(account.id), detail=identity)
    session.commit()
    session.refresh(account)
    return account


def _identity_target_pool(session: Session, account: TgAccount, identity: str) -> AccountPool:
    if identity == CODE_RECEIVER_POOL_KEY:
        return ensure_code_receiver_account_pool(session, account.tenant_id)
    pool = session.get(AccountPool, account.pool_id) if account.pool_id else None
    if pool and not _is_code_receiver_pool(pool):
        return pool
    return ensure_default_account_pool(session, account.tenant_id)
