from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.models import Action


SOURCE_TIMEZONE = ZoneInfo("Asia/Shanghai")


def search_join_fact_in_window(
    action: Action,
    start_at: datetime | None,
    end_at: datetime | None,
    fact_kind: str,
) -> bool:
    if fact_kind == "click":
        if not has_confirmed_click_fact(action.result):
            return False
        observed_at = target_found_at(action.result) or _action_time(action)
    else:
        if not has_confirmed_membership_fact(action.result):
            return False
        observed_at = membership_observed_at(action.result) or _action_time(action)
    return _time_in_window(observed_at, start_at, end_at)


def search_join_held_in_window(
    action: Action,
    start_at: datetime | None,
    end_at: datetime | None,
    fact_kind: str,
    *,
    statuses: tuple[str, ...],
) -> bool:
    if action.status in statuses:
        return _action_in_window(action, start_at, end_at)
    if fact_kind == "membership" and has_pending_membership(action.result):
        return _action_in_window(action, start_at, end_at)
    return False


def has_pending_membership(result: object) -> bool:
    return isinstance(result, dict) and result.get("join_status") == "membership_pending"


def membership_observed_at(result: object) -> datetime | None:
    return _result_timestamp(result, "membership_observed_at")


def target_found_at(result: object) -> datetime | None:
    return _result_timestamp(result, "target_found_at")


def has_confirmed_click_fact(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    return bool(
        result.get("target_click_observed") is True
        or result.get("search_end_reason") == "target_found"
        or result.get("join_status") == "target_found"
    )


def has_confirmed_membership_fact(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("join_status") in {"join_request_pending", "membership_pending"}:
        return False
    return result.get("join_status") == "membership_observed" or result.get("membership_observed") is True


def _result_timestamp(result: object, field: str) -> datetime | None:
    if not isinstance(result, dict):
        return None
    raw = str(result.get(field) or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is None else _source_naive(parsed)
    except ValueError:
        return None


def _action_in_window(action: Action, start_at: datetime | None, end_at: datetime | None) -> bool:
    return _time_in_window(_action_time(action), start_at, end_at)


def _action_time(action: Action) -> datetime | None:
    return action.executed_at or action.scheduled_at


def _time_in_window(value: datetime | None, start_at: datetime | None, end_at: datetime | None) -> bool:
    if value is None:
        return False
    source_value = _source_naive(value) if value.tzinfo else value
    if start_at is None or end_at is None:
        return True
    return start_at <= source_value < end_at


def _source_naive(value: datetime) -> datetime:
    return value.astimezone(SOURCE_TIMEZONE).replace(tzinfo=None)


__all__ = [
    "has_confirmed_click_fact",
    "has_confirmed_membership_fact",
    "has_pending_membership",
    "membership_observed_at",
    "search_join_fact_in_window",
    "search_join_held_in_window",
    "target_found_at",
]
