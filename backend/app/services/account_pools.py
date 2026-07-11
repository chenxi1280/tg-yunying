from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AccountGroupProxyBinding,
    AccountPool,
    AccountStatus,
    MessageTask,
    Task,
    Tenant,
    TgAccount,
    TgContact,
    VerificationTask,
)
from app.schemas import AccountPoolCreate, AccountPoolUpdate

from ._common import _now, audit, require_tenant
from .account_pool_usage_transition import locked_account_and_pool, migrate_account_usage
from .dedicated_account_pools import (
    CODE_RECEIVER_POOL_KEY,
    RANK_DEBOOST_POOL_KEY,
    assert_unique_account_pool_name,
    create_rank_deboost_account_pool,
    ensure_code_receiver_account_pool,
    ensure_rank_deboost_account_pool,
    is_code_receiver_pool,
    is_rank_deboost_pool,
)

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

def account_pool_snapshot(session: Session, pool: AccountPool) -> dict:
    binding = _active_rank_deboost_binding(session, pool)
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
        "rank_deboost_binding_status": binding.status if binding else "",
        "rank_deboost_runtime_proxy_id": binding.runtime_proxy_id if binding else None,
        "rank_deboost_observed_exit_ip": binding.observed_exit_ip if binding else "",
        "created_at": pool.created_at,
        "updated_at": pool.updated_at,
    }


def _active_rank_deboost_binding(session: Session, pool: AccountPool) -> AccountGroupProxyBinding | None:
    if not is_rank_deboost_pool(pool):
        return None
    return session.scalar(
        select(AccountGroupProxyBinding).where(
            AccountGroupProxyBinding.tenant_id == pool.tenant_id,
            AccountGroupProxyBinding.account_pool_id == pool.id,
            AccountGroupProxyBinding.status == "active",
            AccountGroupProxyBinding.unbound_at.is_(None),
        ).limit(1)
    )


def ensure_default_account_pool(session: Session, tenant_id: int) -> AccountPool:
    pool = session.scalar(
        select(AccountPool)
        .where(
            AccountPool.tenant_id == tenant_id,
            AccountPool.is_default.is_(True),
            AccountPool.is_enabled.is_(True),
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
                AccountPool.is_enabled.is_(True),
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
    _mark_default_account_pool(session, pool)
    return pool


def _mark_default_account_pool(session: Session, pool: AccountPool) -> None:
    pool.is_default = True
    for other in session.scalars(
        select(AccountPool).where(
            AccountPool.tenant_id == pool.tenant_id,
            AccountPool.id != pool.id,
            AccountPool.is_default.is_(True),
        )
    ):
        other.is_default = False


def _is_normal_pool(pool: AccountPool | None) -> bool:
    """普通分组：非空、非 code_receiver、非 rank_deboost。

    pool_purpose IS NULL 视为普通（兼容历史数据）。未分组（pool is None）不算普通分组，
    允许未分组账号移入 rank_deboost 分组。
    """
    if pool is None:
        return False
    return not is_code_receiver_pool(pool) and not is_rank_deboost_pool(pool)


def delete_account_pool(session: Session, pool_id: int, actor: str) -> None:
    """删除账号分组。

    rank_deboost 和 code_receiver 分组不可删除，只能禁用（通过 update_account_pool
    设置 is_default=False）。系统级分组（is_system=True）同样不可删除。
    """
    pool = session.get(AccountPool, pool_id)
    if not pool:
        raise ValueError("account pool not found")
    if is_rank_deboost_pool(pool):
        raise ValueError("rank_deboost 分组不可删除，只能禁用")
    if is_code_receiver_pool(pool):
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
    _assert_pool_has_no_active_binding(session, pool)
    _assert_pool_has_no_running_task(session, pool)
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


def _assert_pool_has_no_active_binding(session: Session, pool: AccountPool) -> None:
    binding_id = session.scalar(
        select(AccountGroupProxyBinding.id).where(
            AccountGroupProxyBinding.tenant_id == pool.tenant_id,
            AccountGroupProxyBinding.account_pool_id == pool.id,
            AccountGroupProxyBinding.status == "active",
            AccountGroupProxyBinding.unbound_at.is_(None),
        )
    )
    if binding_id:
        raise ValueError("账号组存在 active 分组绑定，不能删除")


def _assert_pool_has_no_running_task(session: Session, pool: AccountPool) -> None:
    tasks = session.scalars(
        select(Task).where(
            Task.tenant_id == pool.tenant_id,
            Task.status.in_(("running", "paused")),
        )
    )
    if any(_task_references_pool(task, pool.id) for task in tasks):
        raise ValueError("账号组仍被 running/paused 任务引用，不能删除")


def _task_references_pool(task: Task, pool_id: int) -> bool:
    configs = (task.account_config or {}, task.type_config or {})
    keys = ("account_group_id", "account_pool_id", "pool_id")
    return any(_config_pool_id(config, key) == pool_id for config in configs for key in keys)


def _config_pool_id(config: dict, key: str) -> int | None:
    value = config.get(key)
    return int(value) if value is not None else None


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
    in_rank_deboost_pool = is_rank_deboost_pool(pool)
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
    _assert_default_pool_enabled(payload.is_default, payload.is_enabled)
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
    enabled_default_exists = session.scalar(
        select(AccountPool.id).where(
            AccountPool.tenant_id == payload.tenant_id,
            AccountPool.is_default.is_(True),
            AccountPool.is_enabled.is_(True),
            AccountPool.id != pool.id,
        )
    )
    if pool.is_default or (pool.is_enabled and not enabled_default_exists):
        _mark_default_account_pool(session, pool)
    audit(session, tenant_id=pool.tenant_id, actor=actor, action="新增账号池", target_type="account_pool", target_id=str(pool.id))
    session.commit()
    session.refresh(pool)
    return account_pool_snapshot(session, pool)


def update_account_pool(session: Session, pool_id: int, payload: AccountPoolUpdate, actor: str) -> dict:
    pool = session.get(AccountPool, pool_id)
    if not pool:
        raise ValueError("account pool not found")
    data = payload.model_dump(exclude_unset=True)
    _validate_pool_update_lifecycle(pool, data)
    if data.get("name") is not None:
        name = data["name"].strip()
        assert_unique_account_pool_name(session, pool.tenant_id, name, exclude_pool_id=pool.id)
        pool.name = name
    if data.get("description") is not None:
        pool.description = data["description"].strip()
    if data.get("is_default") is not None:
        pool.is_default = bool(data["is_default"])
        if pool.is_default:
            _mark_default_account_pool(session, pool)
    if data.get("is_enabled") is not None:
        _apply_pool_enabled_state(
            pool,
            bool(data["is_enabled"]),
            actor,
            str(data.get("disable_reason") or ""),
        )
    elif data.get("disable_reason") is not None:
        pool.disable_reason = str(data["disable_reason"]).strip()
    pool.updated_at = _now()
    audit(session, tenant_id=pool.tenant_id, actor=actor, action="更新账号池", target_type="account_pool", target_id=str(pool.id))
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ValueError("同租户账号组名称必须唯一") from exc
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


def _validate_pool_update_lifecycle(pool: AccountPool, data: dict) -> None:
    if (is_rank_deboost_pool(pool) or is_code_receiver_pool(pool)) and data.get("is_default"):
        raise ValueError("专用分组不能设为默认")
    target_default = bool(data.get("is_default", pool.is_default))
    target_enabled = bool(data.get("is_enabled", pool.is_enabled))
    _assert_default_pool_enabled(target_default, target_enabled)


def _assert_default_pool_enabled(is_default: bool, is_enabled: bool) -> None:
    if is_default and not is_enabled:
        raise ValueError("default account pool must be enabled")


def move_account_pool(session: Session, account_id: int, pool_id: int, actor: str) -> TgAccount:
    account, pool = locked_account_and_pool(session, account_id, pool_id)
    return migrate_account_usage(session, account, pool, actor=actor, audit_action="移动账号池")


def set_account_identity(session: Session, account_id: int, identity: str, actor: str) -> TgAccount:
    if identity not in {"normal", CODE_RECEIVER_POOL_KEY}:
        raise ValueError("unsupported account identity")
    account = session.scalar(select(TgAccount).where(TgAccount.id == account_id).with_for_update())
    if not account or account.deleted_at is not None:
        raise ValueError("account not found")
    target_pool = _identity_target_pool(session, account, identity)
    return migrate_account_usage(session, account, target_pool, actor=actor, audit_action="设置账号身份")


def _identity_target_pool(session: Session, account: TgAccount, identity: str) -> AccountPool:
    if identity == CODE_RECEIVER_POOL_KEY:
        return ensure_code_receiver_account_pool(session, account.tenant_id)
    pool = session.get(AccountPool, account.pool_id) if account.pool_id else None
    if pool and _is_normal_pool(pool) and pool.is_enabled:
        return pool
    return ensure_default_account_pool(session, account.tenant_id)
