from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    AccountPool,
    AccountRuntimeSummary,
    AccountStatus,
    Action,
    Tenant,
    TgAccount,
    TgAccountSecuritySnapshot,
    TgGroupAccount,
)
from app.services._common import _now
from app.services.account_capacity import available_accounts_by_capacity
from app.timezone import as_beijing, beijing_day_bounds


HEALTH_WEIGHT_MEDIUM = 55
HEALTH_WEIGHT_LOW = 30
LOW_HEALTH_PARTICIPATION_STEP = 4
DAILY_COVERAGE_STATUSES = ("pending", "executing", "success")
DAILY_COVERAGE_SUCCESS_STATUSES = ("success",)
COVERAGE_ACTION_TYPES_BY_TASK_TYPE = {
    "group_ai_chat": ("send_message",),
    "channel_view": ("view_message",),
    "channel_like": ("like_message",),
    "channel_comment": ("post_comment",),
}


def select_task_accounts(
    session: Session,
    tenant_id: int,
    account_config: dict,
    *,
    target_group_id: int | None = None,
    limit: int | None = None,
    scheduled_at=None,
    enforce_max_concurrent: bool = True,
    enforce_capacity: bool = True,
    enforce_shard: bool = False,
    daily_coverage_task_id: str | None = None,
    daily_coverage_action_types: tuple[str, ...] = (),
    daily_coverage_target_count: int = 1,
    daily_coverage_statuses: tuple[str, ...] = DAILY_COVERAGE_STATUSES,
) -> list[TgAccount]:
    max_concurrent = int(account_config.get("max_concurrent") or 20)
    requested = int(limit or max_concurrent)
    wanted = min(requested, max_concurrent) if enforce_max_concurrent else requested
    stmt = _account_query(session, tenant_id, account_config, enforce_shard=enforce_shard)
    if stmt is None:
        return []
    if target_group_id:
        stmt = stmt.join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id).where(
            TgGroupAccount.group_id == target_group_id,
            TgGroupAccount.can_send.is_(True),
        )
    if daily_coverage_task_id and daily_coverage_action_types:
        stmt = _daily_coverage_ordered_query(
            stmt,
            daily_coverage_task_id,
            daily_coverage_action_types,
            target_count=daily_coverage_target_count,
            statuses=daily_coverage_statuses,
        )
    scan_limit = max(wanted * LOW_HEALTH_PARTICIPATION_STEP, wanted)
    accounts = _unique_accounts(session.scalars(stmt.limit(scan_limit)))
    accounts = _cooldown_filtered_accounts(session, accounts, account_config, scan_limit)
    available = (
        available_accounts_by_capacity(
            session,
            tenant_id=tenant_id,
            accounts=accounts,
            scheduled_at=scheduled_at,
        )
        if enforce_capacity
        else accounts
    )
    scores = _effective_health_scores(session, tenant_id, available)
    return _health_weighted_accounts(available, wanted, scores)


def _account_query(session: Session, tenant_id: int, account_config: dict, *, enforce_shard: bool):
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
            TgAccount.account_identity != "code_receiver",
            TgAccount.account_identity != "rank_deboost",
        )
        .order_by(
            func.coalesce(AccountRuntimeSummary.health_score, TgAccount.health_score).desc(),
            TgAccount.id.asc(),
        )
    )
    rescue_admin_id = _rescue_admin_account_id(session, tenant_id)
    if rescue_admin_id:
        stmt = stmt.where(TgAccount.id != rescue_admin_id)
    if enforce_shard:
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


def _rescue_admin_account_id(session: Session, tenant_id: int) -> int:
    tenant = session.get(Tenant, tenant_id)
    return int(tenant.group_rescue_admin_account_id or 0) if tenant else 0


def _daily_coverage_ordered_query(
    stmt,
    task_id: str,
    action_types: tuple[str, ...],
    *,
    target_count: int,
    statuses: tuple[str, ...],
):
    counts = _daily_covered_account_counts_query(task_id, action_types, statuses)
    coverage_count = func.coalesce(counts.c.coverage_count, 0)
    covered_rank = case((coverage_count >= max(1, int(target_count or 1)), 1), else_=0)
    return stmt.order_by(None).order_by(
        covered_rank.asc(),
        coverage_count.asc(),
        func.coalesce(AccountRuntimeSummary.health_score, TgAccount.health_score).desc(),
        TgAccount.id.asc(),
    ).outerjoin(counts, counts.c.account_id == TgAccount.id)


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
    recent_account_ids = _recent_success_account_ids(session, accounts, cutoff)
    cooled: list[TgAccount] = []
    for account in accounts:
        if account.id not in recent_account_ids:
            cooled.append(account)
        if len(cooled) >= limit:
            break
    return cooled


def _recent_success_account_ids(session: Session, accounts: list[TgAccount], cutoff) -> set[int]:
    account_ids = [int(account.id) for account in accounts]
    if not account_ids:
        return set()
    rows = session.scalars(
        select(Action.account_id)
        .where(
            Action.account_id.in_(account_ids),
            Action.status == "success",
            Action.executed_at >= cutoff,
        )
        .distinct()
    )
    return {int(account_id) for account_id in rows if account_id is not None}


def daily_uncovered_account_count(
    session: Session,
    task_id: str,
    action_types: tuple[str, ...],
    accounts: list[TgAccount],
    *,
    target_count: int = 1,
    statuses: tuple[str, ...] = DAILY_COVERAGE_STATUSES,
    count_empty_as_uncovered: bool = False,
) -> int:
    if not accounts or not task_id or not action_types:
        return 0
    counts = daily_account_coverage_counts(
        session,
        task_id,
        action_types,
        [int(account.id) for account in accounts],
        statuses=statuses,
    )
    if not counts and not count_empty_as_uncovered:
        return 0
    target = max(1, int(target_count or 1))
    return sum(1 for account in accounts if counts.get(int(account.id), 0) < target)


def daily_account_coverage_counts(
    session: Session,
    task_id: str,
    action_types: tuple[str, ...],
    account_ids: list[int],
    *,
    statuses: tuple[str, ...] = DAILY_COVERAGE_STATUSES,
) -> dict[int, int]:
    if not task_id or not action_types or not account_ids:
        return {}
    day_start, day_end = beijing_day_bounds(_now())
    occupied_at = func.coalesce(Action.executed_at, Action.scheduled_at, Action.created_at)
    rows = session.execute(
        select(Action.account_id, func.count(Action.id))
        .where(
            Action.task_id == task_id,
            Action.action_type.in_(action_types),
            Action.account_id.in_(account_ids),
            Action.status.in_(statuses),
            occupied_at >= day_start,
            occupied_at < day_end,
        )
        .group_by(Action.account_id)
    )
    return {int(account_id): int(count or 0) for account_id, count in rows if account_id is not None}


def _daily_covered_account_ids(
    session: Session,
    task_id: str,
    action_types: tuple[str, ...],
) -> set[int]:
    day_start, day_end = beijing_day_bounds(_now())
    rows = session.execute(
        select(Action.account_id, Action.executed_at, Action.scheduled_at, Action.created_at).where(
            Action.task_id == task_id,
            Action.action_type.in_(action_types),
            Action.account_id.is_not(None),
            Action.status.in_(DAILY_COVERAGE_STATUSES),
        )
    )
    covered_ids: set[int] = set()
    for account_id, executed_at, scheduled_at, created_at in rows:
        if _in_day(executed_at or scheduled_at or created_at, day_start, day_end):
            covered_ids.add(int(account_id))
    return covered_ids


def _daily_covered_account_query(task_id: str, action_types: tuple[str, ...]):
    day_start, day_end = beijing_day_bounds(_now())
    occupied_at = func.coalesce(Action.executed_at, Action.scheduled_at, Action.created_at)
    return (
        select(Action.account_id)
        .where(
            Action.task_id == task_id,
            Action.action_type.in_(action_types),
            Action.account_id.is_not(None),
            Action.status.in_(DAILY_COVERAGE_STATUSES),
            occupied_at >= day_start,
            occupied_at < day_end,
        )
        .distinct()
    )


def _daily_covered_account_counts_query(
    task_id: str,
    action_types: tuple[str, ...],
    statuses: tuple[str, ...],
):
    day_start, day_end = beijing_day_bounds(_now())
    occupied_at = func.coalesce(Action.executed_at, Action.scheduled_at, Action.created_at)
    return (
        select(Action.account_id, func.count(Action.id).label("coverage_count"))
        .where(
            Action.task_id == task_id,
            Action.action_type.in_(action_types),
            Action.account_id.is_not(None),
            Action.status.in_(statuses),
            occupied_at >= day_start,
            occupied_at < day_end,
        )
        .group_by(Action.account_id)
        .subquery()
    )


def _in_day(value: datetime | None, day_start: datetime, day_end: datetime) -> bool:
    comparable = as_beijing(value)
    return comparable is not None and day_start <= comparable < day_end


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


__all__ = [
    "account_matches_current_shard",
    "apply_account_shard_filter",
    "current_account_shard",
    "daily_uncovered_account_count",
    "daily_account_coverage_counts",
    "select_task_accounts",
]
