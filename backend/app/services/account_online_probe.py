from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
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
    completed_at: datetime | None = None


def probe_due_online_states(
    session: Session,
    *,
    limit: int = 100,
    now: datetime | None = None,
    commit_each: bool = False,
) -> int:
    with _preserve_probe_objects_across_commits(session, enabled=commit_each):
        current_time = now or _now()
        fixed_time = now is not None
        states = _due_probe_states(session, limit=limit, now=current_time)
        states_by_account = {state.account_id: state for state in states}
        accounts: dict[int, TgAccount] = {}
        jobs: list[OnlineProbeJob] = []
        schedules: list[tuple[str, timedelta, timedelta | None]] = []
        batch_completed_at = current_time
        for state in states:
            account = session.get(TgAccount, state.account_id)
            if not account or account.deleted_at is not None:
                _mark_probe_blocked(state, current_time, "account_missing", "账号不存在或已删除")
                schedules.append(_probe_schedule(state))
                _commit_probe_progress(session, commit_each)
                continue
            accounts[account.id] = account
            try:
                credentials = credentials_for_account(session, account, use_proxy=False)
            except ValueError as exc:
                _mark_probe_blocked(state, current_time, "developer_app_unavailable", str(exc))
                schedules.append(_probe_schedule(state))
                _commit_probe_progress(session, commit_each)
                continue
            jobs.append(OnlineProbeJob(account.id, account.session_ciphertext, credentials))
        _commit_probe_progress(session, commit_each and bool(jobs))
        for result in _run_health_probes(jobs):
            completed_at = current_time if fixed_time else max(current_time, result.completed_at or _now())
            state = states_by_account[result.account_id]
            _apply_probe_result(session, accounts[result.account_id], state, completed_at, result)
            schedules.append(_probe_schedule(state))
            batch_completed_at = max(batch_completed_at, completed_at)
            _commit_probe_progress(session, commit_each)
        if schedules and not fixed_time:
            batch_completed_at = max(batch_completed_at, _now())
        _apply_batch_probe_schedules(session, schedules, batch_completed_at)
        _commit_probe_progress(session, commit_each and bool(schedules))
        return len(states)


@contextmanager
def _preserve_probe_objects_across_commits(session: Session, *, enabled: bool) -> Iterator[None]:
    previous = session.expire_on_commit
    if enabled:
        session.expire_on_commit = False
    try:
        yield
    finally:
        session.expire_on_commit = previous


def _probe_schedule(state: TgAccountOnlineState) -> tuple[str, timedelta, timedelta | None]:
    retry_interval = _probe_retry_interval(state)
    stale_interval = None
    if state.online_status == "online":
        baseline = state.last_probe_at or _now()
        stale_interval = stale_deadline_for_state(state, baseline) - baseline
    return state.id, retry_interval, stale_interval


def _apply_batch_probe_schedules(
    session: Session,
    schedules: list[tuple[str, timedelta, timedelta | None]],
    completed_at: datetime,
) -> None:
    grouped: dict[tuple[timedelta, timedelta | None], list[str]] = defaultdict(list)
    for state_id, retry_interval, stale_interval in schedules:
        grouped[(retry_interval, stale_interval)].append(state_id)
    for (retry_interval, stale_interval), state_ids in grouped.items():
        values = {
            "next_probe_at": completed_at + retry_interval,
            "updated_at": completed_at,
        }
        if stale_interval is not None:
            values["stale_after_at"] = completed_at + stale_interval
        session.execute(
            update(TgAccountOnlineState)
            .where(TgAccountOnlineState.id.in_(state_ids))
            .values(**values)
            .execution_options(synchronize_session="fetch")
        )


def _probe_retry_interval(state: TgAccountOnlineState) -> timedelta:
    if state.online_status == "login_required":
        return ONLINE_LOGIN_REQUIRED_RETRY_AFTER
    if state.online_status == "online":
        return _probe_interval_for_state(state)
    return ONLINE_PROBE_FAILURE_RETRY_AFTER


def _run_health_probes(jobs: list[OnlineProbeJob]) -> Iterator[OnlineProbeResult]:
    if not jobs:
        return
    worker_count = min(get_settings().account_online_probe_concurrency, len(jobs))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="account-online-probe") as executor:
        futures = [executor.submit(_run_health_probe, job) for job in jobs]
        for future in as_completed(futures):
            yield future.result()


def _run_health_probe(job: OnlineProbeJob) -> OnlineProbeResult:
    try:
        health = gateway.check_account_health_isolated(job.session_ciphertext, job.credentials)
        return OnlineProbeResult(account_id=job.account_id, health=health, completed_at=_now())
    except Exception as exc:
        return OnlineProbeResult(account_id=job.account_id, error=exc, completed_at=_now())


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
