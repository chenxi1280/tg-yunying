from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    AccountPool,
    AccountRuntimeSummary,
    AccountStatus,
    Action,
    TgAccount,
    TgAccountSecuritySnapshot,
    TgGroupAccount,
)
from app.services._common import _now
from app.services.account_capacity import available_accounts_by_capacity


HEALTH_WEIGHT_MEDIUM = 55
HEALTH_WEIGHT_LOW = 30
LOW_HEALTH_PARTICIPATION_STEP = 4


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
    stmt = _account_query(session, tenant_id, account_config)
    if stmt is None:
        return []
    if target_group_id:
        stmt = stmt.join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id).where(
            TgGroupAccount.group_id == target_group_id,
            TgGroupAccount.can_send.is_(True),
        )
    scan_limit = max(wanted * LOW_HEALTH_PARTICIPATION_STEP, wanted)
    accounts = _cooldown_filtered_accounts(
        session,
        _unique_accounts(session.scalars(stmt.limit(scan_limit))),
        account_config,
        scan_limit,
    )
    available = available_accounts_by_capacity(
        session,
        tenant_id=tenant_id,
        accounts=accounts,
        scheduled_at=scheduled_at,
    )
    scores = _effective_health_scores(session, tenant_id, available)
    return _health_weighted_accounts(available, wanted, scores)


def _account_query(session: Session, tenant_id: int, account_config: dict):
    stmt = (
        select(TgAccount)
        .outerjoin(
            AccountRuntimeSummary,
            and_(
                AccountRuntimeSummary.tenant_id == tenant_id,
                AccountRuntimeSummary.account_id == TgAccount.id,
            ),
        )
        .where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.deleted_at.is_(None),
            TgAccount.status == AccountStatus.ACTIVE.value,
        )
        .order_by(
            func.coalesce(AccountRuntimeSummary.health_score, TgAccount.health_score).desc(),
            TgAccount.id.asc(),
        )
    )
    stmt = apply_account_shard_filter(stmt)
    mode = account_config.get("selection_mode") or "all"
    if mode == "manual":
        account_ids = [int(item) for item in account_config.get("account_ids") or []]
        return stmt.where(TgAccount.id.in_(account_ids)) if account_ids else None
    if mode == "group":
        pool_id = account_config.get("account_group_id")
        pool = session.get(AccountPool, int(pool_id)) if pool_id else None
        return stmt.where(TgAccount.pool_id == pool.id) if pool and pool.tenant_id == tenant_id else None
    return stmt


def _cooldown_filtered_accounts(
    session: Session,
    accounts: list[TgAccount],
    account_config: dict,
    limit: int,
) -> list[TgAccount]:
    cooldown = int(account_config.get("cooldown_per_account_minutes") or 0)
    if cooldown <= 0:
        return accounts
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
        if len(cooled) >= limit:
            break
    return cooled


def _health_weighted_accounts(
    accounts: list[TgAccount],
    wanted: int,
    scores: dict[int, float],
) -> list[TgAccount]:
    selected: list[TgAccount] = []
    low_score_seen = 0
    for account in accounts:
        score = scores.get(account.id, float(account.health_score or 0))
        if score < HEALTH_WEIGHT_LOW:
            continue
        if score < HEALTH_WEIGHT_MEDIUM:
            low_score_seen += 1
            if low_score_seen % LOW_HEALTH_PARTICIPATION_STEP != 1:
                continue
        selected.append(account)
        if len(selected) >= wanted:
            break
    return selected


def _effective_health_scores(session: Session, tenant_id: int, accounts: list[TgAccount]) -> dict[int, float]:
    account_ids = [account.id for account in accounts]
    snapshots = _security_snapshots_by_account(session, tenant_id, account_ids)
    runtime_scores = _runtime_scores_by_account(session, tenant_id, account_ids)
    return {
        account.id: _effective_health_score(account, runtime_scores, snapshots)
        for account in accounts
    }


def _effective_health_score(
    account: TgAccount,
    runtime_scores: dict[int, float],
    snapshots: dict[int, TgAccountSecuritySnapshot],
) -> float:
    if account.id in runtime_scores:
        return _clamped_score(runtime_scores[account.id])
    legacy_score = float(account.health_score or 0)
    legacy_score += _proxy_score_delta(account)
    legacy_score += _security_score_delta(snapshots.get(account.id))
    return _clamped_score(legacy_score)


def _clamped_score(score: float) -> float:
    return max(0, min(100, score))


def _runtime_scores_by_account(session: Session, tenant_id: int, account_ids: list[int]) -> dict[int, float]:
    if not account_ids:
        return {}
    rows = session.execute(
        select(AccountRuntimeSummary.account_id, AccountRuntimeSummary.health_score).where(
            AccountRuntimeSummary.tenant_id == tenant_id,
            AccountRuntimeSummary.account_id.in_(account_ids),
        )
    )
    return {int(account_id): float(score or 0) for account_id, score in rows}


def _security_snapshots_by_account(
    session: Session,
    tenant_id: int,
    account_ids: list[int],
) -> dict[int, TgAccountSecuritySnapshot]:
    if not account_ids:
        return {}
    rows = session.scalars(
        select(TgAccountSecuritySnapshot).where(
            TgAccountSecuritySnapshot.tenant_id == tenant_id,
            TgAccountSecuritySnapshot.account_id.in_(account_ids),
        )
    )
    return {row.account_id: row for row in rows}


def _security_score_delta(snapshot: TgAccountSecuritySnapshot | None) -> int:
    if snapshot is None:
        return 0
    delta = 0
    if snapshot.trusted_session_status in {"missing", "unknown", "failed"}:
        delta -= 35
    if snapshot.external_authorization_count > 0:
        delta -= min(30, 10 + snapshot.external_authorization_count * 5)
    if snapshot.two_fa_status in {"missing", "unknown", "failed"}:
        delta -= 20
    elif snapshot.two_fa_status in {"email_confirmation_required", "pending_email_confirmation"}:
        delta -= 35
    if snapshot.profile_status in {"unknown", "incomplete", "update_failed"}:
        delta -= 10
    return delta


def _proxy_score_delta(account: TgAccount) -> int:
    proxy = account.proxy
    if proxy is None:
        return 0
    if proxy.status == "disabled" or proxy.alert_status == "disabled":
        return -100
    if proxy.alert_status == "alerting" or proxy.status == "unhealthy":
        return -60
    if proxy.alert_status in {"observing", "acknowledged"}:
        return -15
    return 0


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
