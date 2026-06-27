from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Task, TgAccount, TgGroupAccount

from .account_pool import (
    COVERAGE_ACTION_TYPES_BY_TASK_TYPE,
    DAILY_COVERAGE_STATUSES,
    DAILY_COVERAGE_SUCCESS_STATUSES,
    _account_query,
    _unique_accounts,
    daily_account_coverage_counts,
)


def task_account_coverage(session: Session, task: Task) -> dict[str, object]:
    action_types = COVERAGE_ACTION_TYPES_BY_TASK_TYPE.get(task.type)
    if not action_types:
        return {}
    config = task.type_config or {}
    target_count = _task_coverage_target_count(task)
    statuses = _task_coverage_statuses(task)
    target_accounts = _task_coverage_all_accounts(session, task)
    readiness = _coverage_readiness_by_account(session, task, target_accounts)
    eligible_ids = {int(account.id) for account in target_accounts if readiness.get(int(account.id)) == "ready"}
    counts = daily_account_coverage_counts(session, task.id, action_types, list(eligible_ids), statuses=statuses)
    covered_count = sum(1 for account_id in eligible_ids if counts.get(account_id, 0) >= target_count)
    remaining_count = sum(1 for account_id in eligible_ids if counts.get(account_id, 0) < target_count)
    remaining_messages = sum(max(0, target_count - counts.get(account_id, 0)) for account_id in eligible_ids)
    pending_admission_count = sum(1 for status in readiness.values() if status == "pending_admission")
    restricted_count = sum(1 for status in readiness.values() if status == "cannot_send")
    eligible_count = len(eligible_ids)
    coverage_rate = covered_count / eligible_count if eligible_count else 0
    return {
        "mode": str(config.get("account_coverage_mode") or "natural") if task.type == "group_ai_chat" else "natural",
        "covered_count": covered_count,
        "eligible_count": eligible_count,
        "target_account_count": len(target_accounts),
        "remaining_count": remaining_count,
        "remaining_message_count": remaining_messages,
        "pending_admission_count": pending_admission_count,
        "restricted_count": restricted_count,
        "target_per_account": target_count,
        "coverage_rate": coverage_rate,
        "coverage_percent": round(coverage_rate * 100),
        "action_types": list(action_types),
        "statuses": list(statuses),
        "blocked_reasons": _coverage_blocked_reasons(task, pending_admission_count, restricted_count, remaining_count),
        "estimated_completion_window": _coverage_estimated_window(task, remaining_messages),
        "pending_accounts": _coverage_pending_accounts(target_accounts, readiness, counts, target_count),
    }


def _task_coverage_target_count(task: Task) -> int:
    if task.type != "group_ai_chat":
        return 1
    config = task.type_config or {}
    if config.get("account_coverage_mode") != "all_accounts_daily":
        return 1
    return max(1, min(2, int(config.get("per_account_daily_min_messages") or 1)))


def _task_coverage_statuses(task: Task) -> tuple[str, ...]:
    if task.type == "group_ai_chat" and (task.type_config or {}).get("account_coverage_mode") == "all_accounts_daily":
        return DAILY_COVERAGE_SUCCESS_STATUSES
    return DAILY_COVERAGE_STATUSES


def _task_coverage_all_accounts(session: Session, task: Task) -> list[TgAccount]:
    stmt = _account_query(session, task.tenant_id, task.account_config or {}, enforce_shard=False)
    return _unique_accounts(session.scalars(stmt)) if stmt is not None else []


def _coverage_readiness_by_account(session: Session, task: Task, accounts: list[TgAccount]) -> dict[int, str]:
    account_ids = [int(account.id) for account in accounts]
    if not account_ids:
        return {}
    target_group_id = _task_coverage_target_group_id(task)
    if task.type != "group_ai_chat" or not target_group_id:
        return {account_id: "ready" for account_id in account_ids}
    rows = session.execute(
        select(TgGroupAccount.account_id, TgGroupAccount.can_send).where(
            TgGroupAccount.tenant_id == task.tenant_id,
            TgGroupAccount.group_id == target_group_id,
            TgGroupAccount.account_id.in_(account_ids),
        )
    )
    sendable_by_account = {int(account_id): bool(can_send) for account_id, can_send in rows}
    return {
        account_id: "ready" if sendable_by_account.get(account_id) else "cannot_send" if account_id in sendable_by_account else "pending_admission"
        for account_id in account_ids
    }


def _coverage_blocked_reasons(task: Task, pending_count: int, restricted_count: int, remaining_count: int) -> list[dict[str, object]]:
    reasons: list[dict[str, object]] = []
    if remaining_count:
        reasons.append({"reason": "coverage_remaining", "count": remaining_count, "message": "仍有可发言账号未达今日覆盖目标"})
    if pending_count:
        reasons.append({"reason": "pending_admission", "count": pending_count, "message": "账号尚未完成入群或可发言准入"})
    if restricted_count:
        reasons.append({"reason": "cannot_send", "count": restricted_count, "message": "账号已关联目标群但当前不可发言"})
    last_error = str(task.last_error or "").strip()
    if last_error:
        reasons.append({"reason": "last_error", "count": 1, "message": last_error})
    return reasons


def _coverage_estimated_window(task: Task, remaining_messages: int) -> dict[str, object]:
    if remaining_messages <= 0:
        return {"status": "completed", "estimated_min_hours": 0, "label": "已完成今日覆盖"}
    try:
        hourly_cap = int((task.pacing_config or {}).get("max_actions_per_hour") or 0)
    except (TypeError, ValueError):
        hourly_cap = 0
    if hourly_cap <= 0:
        return {"status": "unproven", "estimated_min_hours": None, "label": "缺少小时发送上限，无法估算补齐窗口"}
    window_hours = _task_coverage_window_hours(task)
    estimated_hours = max(1, (remaining_messages + hourly_cap - 1) // hourly_cap)
    status = "within_window" if estimated_hours <= window_hours else "insufficient_hourly_budget"
    label = f"仅按当前小时上限估算最少约 {estimated_hours} 小时，窗口 {window_hours} 小时"
    return {"status": status, "estimated_min_hours": estimated_hours, "label": label}


def _task_coverage_window_hours(task: Task) -> int:
    try:
        value = int((task.type_config or {}).get("coverage_window_hours") or 24)
    except (TypeError, ValueError):
        value = 24
    return max(1, value)


def _coverage_pending_accounts(
    accounts: list[TgAccount],
    readiness: dict[int, str],
    counts: dict[int, int],
    target_count: int,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for account in accounts:
        account_id = int(account.id)
        status = readiness.get(account_id, "ready")
        completed = max(0, int(counts.get(account_id, 0)))
        remaining = max(0, target_count - completed)
        reason = "coverage_remaining" if status == "ready" and remaining else status
        if status == "ready" and remaining <= 0:
            continue
        rows.append(
            {
                "account_id": account_id,
                "display_name": account.display_name,
                "completed_count": completed if status == "ready" else 0,
                "target_count": target_count,
                "remaining_count": remaining if status == "ready" else target_count,
                "reason": reason,
            }
        )
    order = {"coverage_remaining": 0, "pending_admission": 1, "cannot_send": 2}
    return sorted(rows, key=lambda item: (order.get(str(item["reason"]), 9), int(item["account_id"])))[:10]


def _task_coverage_target_group_id(task: Task) -> int | None:
    if task.type != "group_ai_chat":
        return None
    try:
        parsed_id = int((task.type_config or {}).get("target_group_id") or 0)
    except (TypeError, ValueError):
        return None
    return parsed_id or None


__all__ = ["task_account_coverage"]
