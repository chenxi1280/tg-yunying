"""Resolve configured membership candidates without changing account scope."""
from typing import Any

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.models import AccountStatus, Tenant, TgAccount
from app.services.account_usage_policy import apply_operational_account_filters


def candidate_accounts_for_config(session: Session, tenant_id: int, account_config: dict[str, Any]) -> list[TgAccount]:
    stmt = (
        select(TgAccount)
        .where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.deleted_at.is_(None),
            TgAccount.status == AccountStatus.ACTIVE.value,
        )
        .order_by(TgAccount.health_score.desc(), TgAccount.id.asc())
    )
    stmt = apply_operational_account_filters(stmt)
    rescue_admin_id = _rescue_admin_account_id(session, tenant_id)
    if rescue_admin_id:
        stmt = stmt.where(TgAccount.id != rescue_admin_id)
    mode = account_config.get("selection_mode") or "all"
    if mode == "manual":
        account_ids = [int(item) for item in account_config.get("account_ids") or []]
        if not account_ids:
            stmt = None
        else:
            account_order = case(
                {account_id: index for index, account_id in enumerate(account_ids)},
                value=TgAccount.id,
            )
            stmt = stmt.where(TgAccount.id.in_(account_ids)).order_by(None).order_by(account_order.asc())
    elif mode == "group":
        raw_ids = account_config.get("account_group_ids") or []
        if not raw_ids and account_config.get("account_group_id"):
            raw_ids = [account_config["account_group_id"]]
        pool_ids = [int(item) for item in raw_ids if int(item) > 0]
        if not pool_ids:
            stmt = None
        else:
            stmt = stmt.where(TgAccount.pool_id.in_(pool_ids))
    return list(session.scalars(stmt)) if stmt is not None else []



def _rescue_admin_account_id(session: Session, tenant_id: int) -> int:
    tenant = session.get(Tenant, tenant_id)
    return int(tenant.group_rescue_admin_account_id or 0) if tenant else 0
