from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AccountPool, AccountStatus, Action, TgAccount, TgGroupAccount
from app.services._common import _now
from app.services.account_capacity import available_accounts_by_capacity


def select_task_accounts(
    session: Session,
    tenant_id: int,
    account_config: dict,
    *,
    target_group_id: int | None = None,
    limit: int | None = None,
    scheduled_at=None,
) -> list[TgAccount]:
    max_concurrent = int(account_config.get("max_concurrent") or 20)
    wanted = min(limit or max_concurrent, max_concurrent)
    stmt = (
        select(TgAccount)
        .where(TgAccount.tenant_id == tenant_id, TgAccount.deleted_at.is_(None), TgAccount.status == AccountStatus.ACTIVE.value)
        .order_by(TgAccount.health_score.desc(), TgAccount.id.asc())
    )
    stmt = apply_account_shard_filter(stmt)
    mode = account_config.get("selection_mode") or "all"
    if mode == "manual":
        account_ids = [int(item) for item in account_config.get("account_ids") or []]
        if not account_ids:
            return []
        stmt = stmt.where(TgAccount.id.in_(account_ids))
    elif mode == "group":
        pool_id = account_config.get("account_group_id")
        pool = session.get(AccountPool, int(pool_id)) if pool_id else None
        if not pool or pool.tenant_id != tenant_id:
            return []
        stmt = stmt.where(TgAccount.pool_id == pool.id)
    if target_group_id:
        stmt = stmt.join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id).where(
            TgGroupAccount.group_id == target_group_id,
            TgGroupAccount.can_send.is_(True),
        )
    accounts = _unique_accounts(session.scalars(stmt.limit(max(wanted * 3, wanted))))
    cooldown = int(account_config.get("cooldown_per_account_minutes") or 0)
    if cooldown > 0:
        cutoff = _now() - timedelta(minutes=cooldown)
        cooled: list[TgAccount] = []
        for account in accounts:
            recent = session.scalar(
                select(Action.id).where(
                    Action.account_id == account.id,
                    Action.status == "success",
                    Action.executed_at >= cutoff,
                ).limit(1)
            )
            if not recent:
                cooled.append(account)
            if len(cooled) >= max(wanted * 3, wanted):
                break
        accounts = cooled
    return available_accounts_by_capacity(
        session,
        tenant_id=tenant_id,
        accounts=accounts,
        scheduled_at=scheduled_at,
        limit=wanted,
    )


def _unique_accounts(accounts) -> list[TgAccount]:
    result: list[TgAccount] = []
    seen: set[int] = set()
    for account in accounts:
        if account.id in seen:
            continue
        seen.add(account.id)
        result.append(account)
    return result


def current_account_shard() -> tuple[int, int]:
    settings = get_settings()
    total = max(1, int(settings.account_shard_total or 1))
    index = max(0, min(total - 1, int(settings.account_shard_index or 0)))
    return total, index


def account_matches_current_shard(account_id: int | None) -> bool:
    if account_id is None:
        return True
    total, index = current_account_shard()
    if total <= 1:
        return True
    return int(account_id) % total == index


def apply_account_shard_filter(stmt):
    total, index = current_account_shard()
    if total <= 1:
        return stmt
    return stmt.where((TgAccount.id % total) == index)


__all__ = ["account_matches_current_shard", "apply_account_shard_filter", "current_account_shard", "select_task_accounts"]
