from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

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
    accounts = list(session.scalars(stmt.limit(max(wanted * 3, wanted))))
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


__all__ = ["select_task_accounts"]
