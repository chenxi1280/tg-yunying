from __future__ import annotations

from datetime import datetime
from typing import Any

from app.models import TgAccountOnlineState
from app.services.account_online_probe import only_low_frequency_sources, stale_deadline_for_sources
from app.timezone import as_beijing


def apply_desired_state(state: TgAccountOnlineState, meta: dict[str, Any], now: datetime) -> int:
    before = _state_signature(state)
    values = _desired_state_values(state, meta, now)
    if all(getattr(state, key) == value for key, value in values.items()):
        return 0
    for key, value in values.items():
        setattr(state, key, value)
    state.reconciled_at = now
    state.updated_at = now
    return int(before != _state_signature(state))


def clear_desired_state(state: TgAccountOnlineState, now: datetime) -> int:
    before = _state_signature(state)
    state.desired_online = False
    state.desired_sources = []
    state.active_task_count = 0
    state.online_status = "offline"
    state.failure_type = "desired_source_removed"
    state.failure_detail = "在线需求来源已移除"
    state.reconciled_at = now
    state.updated_at = now
    return int(before != _state_signature(state))


def _desired_state_values(state: TgAccountOnlineState, meta: dict[str, Any], now: datetime) -> dict[str, Any]:
    sources = meta["sources"]
    active_reprobe = _requires_active_reprobe(state, sources)
    online_status = "warming" if active_reprobe else _next_desired_status(state)
    desired_deadline = stale_deadline_for_sources(sources, now)
    stale_after_at = desired_deadline if active_reprobe else state.stale_after_at or desired_deadline
    next_probe_at = now if active_reprobe else state.next_probe_at
    if not active_reprobe and online_status == "online" and _probe_after_stale_deadline(state):
        next_probe_at = now
    values = {
        "desired_online": True,
        "desired_sources": sources,
        "active_task_count": int(meta.get("active_task_count") or 0),
        "session_kind": str(meta.get("session_kind") or state.session_kind or "primary"),
        "session_id": str(meta.get("session_id") or state.session_id or ""),
        "online_status": online_status,
        "stale_after_at": stale_after_at,
        "next_probe_at": next_probe_at,
        "failure_type": "" if online_status != "blocked" else state.failure_type,
    }
    if "proxy_id" in meta:
        values["proxy_id"] = meta.get("proxy_id")
    return values


def _requires_active_reprobe(state: TgAccountOnlineState, sources: list[dict]) -> bool:
    return (
        state.online_status == "online"
        and only_low_frequency_sources(state.desired_sources)
        and not only_low_frequency_sources(sources)
    )


def _next_desired_status(state: TgAccountOnlineState) -> str:
    if state.online_status == "online":
        return "online"
    if state.online_status in {"blocked", "login_required"}:
        return state.online_status
    return "warming"


def _probe_after_stale_deadline(state: TgAccountOnlineState) -> bool:
    stale_after = as_beijing(state.stale_after_at)
    next_probe = as_beijing(state.next_probe_at)
    return bool(stale_after and (next_probe is None or next_probe >= stale_after))


def _state_signature(state: TgAccountOnlineState) -> tuple[Any, ...]:
    return (
        state.desired_online,
        state.desired_sources,
        state.online_status,
        state.session_kind,
        state.session_id,
        state.proxy_id,
        state.active_task_count,
    )


__all__ = ["apply_desired_state", "clear_desired_state"]
