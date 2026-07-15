from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AccountStatus, TgAccount, TgAccountOnlineState
from app.services._common import _now, gateway
from app.services.account_online_constants import (
    ONLINE_LOGIN_REQUIRED_RETRY_AFTER,
    ONLINE_LOW_FREQUENCY_PROBE_INTERVAL,
    ONLINE_PROBE_FAILURE_RETRY_AFTER,
    ONLINE_PROBE_INTERVAL,
    ONLINE_STALE_AFTER,
    ONLINE_STALE_GRACE,
)
from app.services.developer_apps import credentials_for_account


logger = logging.getLogger(__name__)
MAX_PROBE_FAILURE_DETAIL_LENGTH = 500


@dataclass(frozen=True)
class OnlineProbeJob:
    account_id: int
    session_ciphertext: str | None
    credentials: Any


@dataclass(frozen=True)
class OnlineProbeResult:
    account_id: int
    health: Any = None
    error: Exception | None = None


def probe_due_online_states(
    session: Session,
    *,
    limit: int = 100,
    now: datetime | None = None,
    commit_each: bool = False,
) -> int:
    current_time = now or _now()
    states = _due_probe_states(session, limit=limit, now=current_time)
    states_by_account = {state.account_id: state for state in states}
    accounts: dict[int, TgAccount] = {}
    jobs: list[OnlineProbeJob] = []
    for state in states:
        account = session.get(TgAccount, state.account_id)
        if not account or account.deleted_at is not None:
            _mark_probe_blocked(state, current_time, "account_missing", "账号不存在或已删除")
            _commit_probe_progress(session, commit_each)
            continue
        accounts[account.id] = account
        try:
            credentials = credentials_for_account(session, account, use_proxy=False)
        except ValueError as exc:
            _mark_probe_blocked(state, current_time, "developer_app_unavailable", str(exc))
            _commit_probe_progress(session, commit_each)
            continue
        jobs.append(OnlineProbeJob(account.id, account.session_ciphertext, credentials))
    for result in _run_health_probes(jobs):
        _apply_probe_result(session, accounts[result.account_id], states_by_account[result.account_id], current_time, result)
        _commit_probe_progress(session, commit_each)
    return len(states)


def _run_health_probes(jobs: list[OnlineProbeJob]) -> list[OnlineProbeResult]:
    if not jobs:
        return []
    worker_count = min(get_settings().account_online_probe_concurrency, len(jobs))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="account-online-probe") as executor:
        return list(executor.map(_run_health_probe, jobs))


def _run_health_probe(job: OnlineProbeJob) -> OnlineProbeResult:
    try:
        health = gateway.check_account_health(job.session_ciphertext, job.credentials)
        return OnlineProbeResult(account_id=job.account_id, health=health)
    except Exception as exc:
        return OnlineProbeResult(account_id=job.account_id, error=exc)


def _apply_probe_result(
    session: Session,
    account: TgAccount,
    state: TgAccountOnlineState,
    now: datetime,
    result: OnlineProbeResult,
) -> None:
    if isinstance(result.error, ValueError):
        _mark_probe_blocked(state, now, "developer_app_unavailable", str(result.error))
        return
    if result.error is not None:
        logger.warning("account online probe failed for account_id=%s: %s", account.id, result.error)
        _mark_probe_exception(account, state, now, result.error)
        return
    health = result.health
    if health.status == AccountStatus.ACTIVE.value:
        _mark_probe_online(account, state, now, health.health_score, health.detail)
        from app.services.task_center.daily_coverage import release_online_coverage_blockers

        release_online_coverage_blockers(session, tenant_id=account.tenant_id, account_id=account.id, now=now)
        return
    _mark_probe_unavailable(account, state, now, health.status, health.health_score, health.detail)


def _due_probe_states(session: Session, *, limit: int, now: datetime) -> list[TgAccountOnlineState]:
    return list(
        session.scalars(
            select(TgAccountOnlineState)
            .where(
                TgAccountOnlineState.desired_online.is_(True),
                TgAccountOnlineState.online_status.in_(["warming", "offline", "recovering", "online", "blocked"]),
                (TgAccountOnlineState.next_probe_at.is_(None) | (TgAccountOnlineState.next_probe_at <= now)),
            )
            .order_by(TgAccountOnlineState.next_probe_at.asc().nullsfirst(), TgAccountOnlineState.updated_at.asc())
            .limit(max(1, limit))
        )
    )


def _mark_probe_exception(account: TgAccount, state: TgAccountOnlineState, now: datetime, exc: Exception) -> None:
    detail = _probe_exception_detail(exc)
    if _is_auth_key_duplicated(exc):
        _mark_probe_unavailable(account, state, now, AccountStatus.SESSION_EXPIRED.value, 0, detail)
        return
    _mark_probe_blocked(state, now, "account_health_probe_failed", detail)


def _probe_exception_detail(exc: Exception) -> str:
    detail = f"{exc.__class__.__name__}: {exc}"
    return detail[:MAX_PROBE_FAILURE_DETAIL_LENGTH]


def _is_auth_key_duplicated(exc: Exception) -> bool:
    return exc.__class__.__name__ == "AuthKeyDuplicatedError"


def _commit_probe_progress(session: Session, commit_each: bool) -> None:
    if commit_each:
        session.commit()


def _mark_probe_online(account: TgAccount, state: TgAccountOnlineState, now: datetime, health_score: float, detail: str) -> None:
    account.status = AccountStatus.ACTIVE.value
    account.health_score = health_score
    account.last_active_at = now
    state.online_status = "online"
    state.failure_type = ""
    state.failure_detail = detail
    state.last_seen_at = now
    state.last_probe_at = now
    state.next_probe_at = now + _probe_interval_for_state(state)
    state.stale_after_at = stale_deadline_for_state(state, now)
    state.updated_at = now


def _mark_probe_unavailable(account: TgAccount, state: TgAccountOnlineState, now: datetime, status: str, health_score: float, detail: str) -> None:
    account.status = status
    account.health_score = health_score
    if status in {AccountStatus.NEED_RELOGIN.value, AccountStatus.SESSION_EXPIRED.value}:
        state.online_status = "login_required"
        state.next_probe_at = now + ONLINE_LOGIN_REQUIRED_RETRY_AFTER
    else:
        state.online_status = "blocked"
        state.next_probe_at = now + ONLINE_PROBE_FAILURE_RETRY_AFTER
    state.failure_type = "account_unavailable"
    state.failure_detail = detail
    state.last_probe_at = now
    state.updated_at = now


def _mark_probe_blocked(state: TgAccountOnlineState, now: datetime, failure_type: str, detail: str) -> None:
    state.online_status = "blocked"
    state.failure_type = failure_type
    state.failure_detail = detail
    state.last_probe_at = now
    state.next_probe_at = now + ONLINE_PROBE_FAILURE_RETRY_AFTER
    state.updated_at = now


def _probe_interval_for_state(state: TgAccountOnlineState) -> timedelta:
    if only_low_frequency_sources(state.desired_sources):
        return ONLINE_LOW_FREQUENCY_PROBE_INTERVAL
    return ONLINE_PROBE_INTERVAL


def stale_deadline_for_state(state: TgAccountOnlineState, now: datetime) -> datetime:
    return stale_deadline_for_sources(state.desired_sources, now)


def stale_deadline_for_sources(sources: list[dict], now: datetime) -> datetime:
    probe_interval = ONLINE_LOW_FREQUENCY_PROBE_INTERVAL if only_low_frequency_sources(sources) else ONLINE_PROBE_INTERVAL
    probe_window = probe_interval + ONLINE_STALE_GRACE
    stale_window = probe_window if probe_window > ONLINE_STALE_AFTER else ONLINE_STALE_AFTER
    return now + stale_window


def only_low_frequency_sources(raw_sources) -> bool:
    sources = raw_sources if isinstance(raw_sources, list) else []
    if not sources:
        return False
    return all(isinstance(source, dict) and source.get("keepalive_mode") == "low_frequency" for source in sources)


__all__ = ["only_low_frequency_sources", "probe_due_online_states", "stale_deadline_for_sources", "stale_deadline_for_state"]
