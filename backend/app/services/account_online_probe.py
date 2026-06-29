from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

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


def probe_due_online_states(session: Session, *, limit: int = 100, now: datetime | None = None) -> int:
    current_time = now or _now()
    states = _due_probe_states(session, limit=limit, now=current_time)
    for state in states:
        account = session.get(TgAccount, state.account_id)
        if not account or account.deleted_at is not None:
            _mark_probe_blocked(state, current_time, "account_missing", "账号不存在或已删除")
            continue
        _probe_account_state(session, account, state, current_time)
    return len(states)


def _due_probe_states(session: Session, *, limit: int, now: datetime) -> list[TgAccountOnlineState]:
    return list(
        session.scalars(
            select(TgAccountOnlineState)
            .where(
                TgAccountOnlineState.desired_online.is_(True),
                TgAccountOnlineState.online_status.in_(["warming", "offline", "recovering", "online"]),
                (TgAccountOnlineState.next_probe_at.is_(None) | (TgAccountOnlineState.next_probe_at <= now)),
            )
            .order_by(TgAccountOnlineState.next_probe_at.asc().nullsfirst(), TgAccountOnlineState.updated_at.asc())
            .limit(max(1, limit))
        )
    )


def _probe_account_state(session: Session, account: TgAccount, state: TgAccountOnlineState, now: datetime) -> None:
    try:
        result = gateway.check_account_health(account.session_ciphertext, credentials_for_account(session, account))
    except ValueError as exc:
        _mark_probe_blocked(state, now, "developer_app_unavailable", str(exc))
        return
    if result.status == AccountStatus.ACTIVE.value:
        _mark_probe_online(account, state, now, result.health_score, result.detail)
        return
    _mark_probe_unavailable(account, state, now, result.status, result.health_score, result.detail)


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
    if _only_low_frequency_sources(state):
        return ONLINE_LOW_FREQUENCY_PROBE_INTERVAL
    return ONLINE_PROBE_INTERVAL


def stale_deadline_for_state(state: TgAccountOnlineState, now: datetime) -> datetime:
    probe_window = _probe_interval_for_state(state) + ONLINE_STALE_GRACE
    stale_window = probe_window if probe_window > ONLINE_STALE_AFTER else ONLINE_STALE_AFTER
    return now + stale_window


def _only_low_frequency_sources(state: TgAccountOnlineState) -> bool:
    sources = state.desired_sources if isinstance(state.desired_sources, list) else []
    if not sources:
        return False
    return all(isinstance(source, dict) and source.get("keepalive_mode") == "low_frequency" for source in sources)


__all__ = ["probe_due_online_states", "stale_deadline_for_state"]
