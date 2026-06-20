from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AccountPool, AccountStatus, TgAccount, TgLoginFlow
from app.schemas.operations_center import OperationMetricDetailOut
from app.services.account_authorization_read_model import authorization_summaries_for_accounts


LOGIN_DROP_STATUSES = {
    AccountStatus.PENDING_LOGIN.value,
    AccountStatus.WAITING_CODE.value,
    AccountStatus.WAITING_QR.value,
    AccountStatus.WAITING_2FA.value,
    AccountStatus.NEED_RELOGIN.value,
    AccountStatus.SESSION_EXPIRED.value,
    AccountStatus.ERROR.value,
}


@dataclass(frozen=True)
class LoginIssueFlags:
    by_status: bool
    by_login_failure: bool
    by_primary_unavailable: bool

    @property
    def matched(self) -> bool:
        return self.by_status or self.by_login_failure or self.by_primary_unavailable


def account_pool_login_drop_rates(session: Session, tenant_id: int) -> list[OperationMetricDetailOut]:
    pools = _account_pools(session, tenant_id)
    accounts = _accounts(session, tenant_id)
    latest_flows = _latest_login_flows_by_account(session, [account.id for account in accounts])
    auth_summaries = authorization_summaries_for_accounts(session, accounts)
    buckets = _empty_buckets(pools)
    for account in accounts:
        bucket = buckets.get(account.pool_id)
        if bucket is None:
            continue
        bucket["total"] += 1
        flags = _login_issue_flags(account, latest_flows.get(account.id), auth_summaries.get(account.id))
        if not flags.matched:
            continue
        bucket["issues"] += 1
        bucket["status_counts"][account.status] += 1
        if flags.by_login_failure:
            bucket["failures"] += 1
        if flags.by_primary_unavailable:
            bucket["primary_unavailable"] += 1
    return [_metric_detail(pool, buckets[pool.id]) for pool in _sorted_pools(pools, buckets)]


def _account_pools(session: Session, tenant_id: int) -> list[AccountPool]:
    return list(session.scalars(select(AccountPool).where(AccountPool.tenant_id == tenant_id).order_by(AccountPool.id.asc())))


def _accounts(session: Session, tenant_id: int) -> list[TgAccount]:
    return list(session.scalars(select(TgAccount).where(TgAccount.tenant_id == tenant_id, TgAccount.deleted_at.is_(None))))


def _latest_login_flows_by_account(session: Session, account_ids: list[int]) -> dict[int, TgLoginFlow]:
    if not account_ids:
        return {}
    latest_ids = (
        select(func.max(TgLoginFlow.id).label("id"))
        .where(TgLoginFlow.account_id.in_(account_ids))
        .group_by(TgLoginFlow.account_id)
        .subquery()
    )
    rows = session.scalars(select(TgLoginFlow).join(latest_ids, TgLoginFlow.id == latest_ids.c.id))
    return {row.account_id: row for row in rows}


def _empty_buckets(pools: list[AccountPool]) -> dict[int, dict[str, Any]]:
    return {
        pool.id: {
            "total": 0,
            "issues": 0,
            "failures": 0,
            "primary_unavailable": 0,
            "status_counts": Counter(),
        }
        for pool in pools
    }


def _login_issue_flags(account: TgAccount, flow: TgLoginFlow | None, summary: dict[str, Any] | None) -> LoginIssueFlags:
    primary_status = str((summary or {}).get("primary_status") or "")
    return LoginIssueFlags(
        by_status=account.status in LOGIN_DROP_STATUSES,
        by_login_failure=bool(flow and (flow.failure_type or flow.failure_detail)),
        by_primary_unavailable=primary_status != "active",
    )


def _sorted_pools(pools: list[AccountPool], buckets: dict[int, dict[str, Any]]) -> list[AccountPool]:
    return sorted(pools, key=lambda pool: (-_rate(buckets[pool.id]), -int(buckets[pool.id]["issues"]), pool.id))


def _metric_detail(pool: AccountPool, bucket: dict[str, Any]) -> OperationMetricDetailOut:
    rate = _rate(bucket)
    return OperationMetricDetailOut(
        key=f"account-pool-login-drop:{pool.id}",
        title=pool.name,
        category="账号分组登录掉号",
        status=f"{rate}%",
        detail=_detail_text(bucket, rate),
        related_id=str(pool.id),
    )


def _rate(bucket: dict[str, Any]) -> float:
    total = int(bucket["total"] or 0)
    issues = int(bucket["issues"] or 0)
    return round(issues * 100 / total, 1) if total else 0.0


def _detail_text(bucket: dict[str, Any], rate: float) -> str:
    parts = [
        f"登录问题 {int(bucket['issues'])}/{int(bucket['total'])}",
        f"登录掉号 {rate}%",
        f"状态分布 {_status_counts_text(bucket['status_counts'])}",
        f"登录失败 {int(bucket['failures'])}",
        f"主授权不可用 {int(bucket['primary_unavailable'])}",
    ]
    return "；".join(parts)


def _status_counts_text(status_counts: Counter[str]) -> str:
    if not status_counts:
        return "无"
    return "、".join(f"{status} {count}" for status, count in sorted(status_counts.items()))


__all__ = ["account_pool_login_drop_rates"]
