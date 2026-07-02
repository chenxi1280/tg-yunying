from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountStatus, Task, TgAccount, TgAccountOnlineState
from app.services._common import _now
from app.timezone import as_beijing


def task_account_online_summary(session: Session, task: Task, *, now: datetime | None = None) -> dict[str, Any]:
    current_time = now or _now()
    states = _task_online_states(session, task)
    desired_ids = {state.account_id for state in states}
    desired_ids.update(_fallback_configured_account_ids(session, task, desired_ids))
    rows = [_state_projection(state, current_time) for state in states]
    status_counts: dict[str, int] = {}
    for row in rows:
        status_counts[row["bucket"]] = status_counts.get(row["bucket"], 0) + 1
    missing_count = max(0, len(desired_ids) - len({row["account_id"] for row in rows}))
    if missing_count:
        status_counts["missing_state"] = missing_count
    return {
        "desired_count": len(desired_ids),
        "online_count": status_counts.get("online", 0),
        "warming_count": status_counts.get("warming", 0),
        "recovering_count": status_counts.get("recovering", 0),
        "relogin_required_count": status_counts.get("relogin_required", 0),
        "blocked_count": status_counts.get("blocked", 0),
        "stale_count": status_counts.get("stale", 0),
        "offline_count": status_counts.get("offline", 0),
        "missing_state_count": missing_count,
        "status_counts": status_counts,
        "samples": [row for row in rows if row["bucket"] != "online"][:10],
    }


def _task_online_states(session: Session, task: Task) -> list[TgAccountOnlineState]:
    states = list(
        session.scalars(
            select(TgAccountOnlineState).where(
                TgAccountOnlineState.tenant_id == task.tenant_id,
                TgAccountOnlineState.desired_online.is_(True),
            )
        )
    )
    return [state for state in states if _has_task_source(state, task.id)]


def _has_task_source(state: TgAccountOnlineState, task_id: str) -> bool:
    sources = state.desired_sources if isinstance(state.desired_sources, list) else []
    for source in sources:
        if not isinstance(source, dict) or source.get("source_type") != "task":
            continue
        source_id = str(source.get("source_id") or "")
        if source_id == task_id or source_id.startswith(f"{task_id}:"):
            return True
    return False


def _fallback_configured_account_ids(session: Session, task: Task, existing_ids: set[int]) -> set[int]:
    account_config = task.account_config if isinstance(task.account_config, dict) else {}
    raw_ids = account_config.get("account_ids") if account_config.get("selection_mode") == "manual" else []
    account_ids = {_as_int(item) for item in raw_ids or []}
    account_ids.discard(0)
    if account_ids:
        return account_ids
    if existing_ids or account_config.get("selection_mode") not in {"", None, "all"}:
        return set()
    rows = session.scalars(
        select(TgAccount.id).where(
            TgAccount.tenant_id == task.tenant_id,
            TgAccount.deleted_at.is_(None),
            TgAccount.status == AccountStatus.ACTIVE.value,
            TgAccount.account_identity != "code_receiver",
            TgAccount.session_ciphertext != "",
        )
    )
    return {int(account_id) for account_id in rows}


def _state_projection(state: TgAccountOnlineState, now: datetime) -> dict[str, Any]:
    bucket = _state_bucket(state, now)
    return {
        "account_id": state.account_id,
        "online_status": state.online_status,
        "bucket": bucket,
        "failure_type": state.failure_type,
        "failure_detail": state.failure_detail,
        "recovery_status": state.recovery_status,
        "last_seen_at": state.last_seen_at,
        "last_probe_at": state.last_probe_at,
        "stale_after_at": state.stale_after_at,
        "desired_sources": state.desired_sources or [],
    }


def _state_bucket(state: TgAccountOnlineState, now: datetime) -> str:
    stale_after = as_beijing(state.stale_after_at)
    current_time = as_beijing(now) or now
    if stale_after and stale_after <= current_time:
        return "stale"
    if state.failure_type in {"session_invalid", "login_required", "relogin_required"}:
        return "relogin_required"
    if state.online_status in {"online", "warming", "recovering", "offline"}:
        return state.online_status
    if state.online_status in {"blocked", "proxy_failed", "restricted"}:
        return "blocked"
    return state.online_status or "offline"


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = ["task_account_online_summary"]
