from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TgAccount, TgAccountOnlineState
from app.services._common import _now
from app.timezone import as_beijing

ONLINE_AVAILABLE_STATUSES = {"online", "warming", "recovering"}


def online_ready_account_ids_for_planning(
    session: Session,
    *,
    tenant_id: int,
    accounts: list[TgAccount],
    now: datetime | None = None,
) -> set[int]:
    if not accounts:
        return set()
    current_time = now or _now()
    account_by_id = {account.id: account for account in accounts}
    states = session.scalars(
        select(TgAccountOnlineState).where(
            TgAccountOnlineState.tenant_id == tenant_id,
            TgAccountOnlineState.account_id.in_(account_by_id),
        )
    )
    return {
        state.account_id
        for state in states
        if state_is_ready(state, current_time)
        and state_matches_account_dimensions(state, account_by_id.get(state.account_id))
    }


def state_is_ready(state: TgAccountOnlineState | None, now: datetime) -> bool:
    if not state or not state.desired_online or state.online_status != "online":
        return False
    return not _state_is_stale(state, now)


def state_is_available(state: TgAccountOnlineState | None, now: datetime) -> bool:
    if not state or not state.desired_online or state.online_status not in ONLINE_AVAILABLE_STATUSES:
        return False
    return not _state_is_stale(state, now)


def state_matches_account_dimensions(state: TgAccountOnlineState | None, account: TgAccount | None) -> bool:
    if not state or not account or account.deleted_at is not None:
        return False
    if state.session_id and state.session_id != str(account.id):
        return False
    if state.proxy_id is not None and state.proxy_id != account.proxy_id:
        return False
    return True


def _state_is_stale(state: TgAccountOnlineState, now: datetime) -> bool:
    stale_after = as_beijing(state.stale_after_at)
    current_time = as_beijing(now) or now
    return bool(stale_after and stale_after <= current_time)


__all__ = ["online_ready_account_ids_for_planning", "state_is_available", "state_is_ready", "state_matches_account_dimensions"]
