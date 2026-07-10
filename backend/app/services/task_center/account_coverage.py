from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Task, TaskAccountDailyCoverage, TgAccount, TgGroup, TgGroupAccount
from app.services._common import _now

from .account_pool import (
    COVERAGE_ACTION_TYPES_BY_TASK_TYPE,
    DAILY_COVERAGE_STATUSES,
    DAILY_COVERAGE_SUCCESS_STATUSES,
    _account_query,
    _unique_accounts,
    daily_account_coverage_counts,
)
from .config_normalization import apply_group_ai_account_coverage_defaults
from .coverage_capacity import task_coverage_capacity_proof


def task_account_coverage(session: Session, task: Task) -> dict[str, object]:
    action_types = COVERAGE_ACTION_TYPES_BY_TASK_TYPE.get(task.type)
    if not action_types:
        return {}
    config = _effective_coverage_config(task)
    if task.type == "group_ai_chat" and config.get("account_coverage_mode") == "all_accounts_daily":
        ledger_summary = _ledger_account_coverage(session, task)
        if ledger_summary:
            return ledger_summary
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


def list_task_account_coverage_page(
    session: Session,
    tenant_id: int,
    task_id: str,
    *,
    coverage_date: date,
    state: str | None = None,
    blocker_code: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict[str, object]], int]:
    task = session.get(Task, task_id)
    if task is None or task.tenant_id != tenant_id or task.deleted_at is not None:
        raise ValueError("task not found")
    filters = [
        TaskAccountDailyCoverage.tenant_id == tenant_id,
        TaskAccountDailyCoverage.task_id == task_id,
        TaskAccountDailyCoverage.coverage_date == coverage_date,
    ]
    if state:
        filters.append(TaskAccountDailyCoverage.state == state)
    if blocker_code:
        filters.append(TaskAccountDailyCoverage.blocker_code == blocker_code)
    total = session.scalar(select(func.count()).select_from(TaskAccountDailyCoverage).where(*filters)) or 0
    rows = session.execute(
        select(TaskAccountDailyCoverage, TgAccount)
        .join(TgAccount, TgAccount.id == TaskAccountDailyCoverage.account_id)
        .where(*filters)
        .order_by(TaskAccountDailyCoverage.account_id.asc())
        .offset((max(1, page) - 1) * max(1, page_size))
        .limit(max(1, page_size))
    )
    return [_coverage_item_payload(row, account) for row, account in rows], int(total)


def _ledger_account_coverage(session: Session, task: Task) -> dict[str, object]:
    from .daily_coverage import ensure_task_daily_coverage

    ensure_task_daily_coverage(session, task, now=_now())
    rows = list(
        session.scalars(
            select(TaskAccountDailyCoverage).where(
                TaskAccountDailyCoverage.task_id == task.id,
                TaskAccountDailyCoverage.coverage_date == _now().date(),
            )
        )
    )
    if not rows:
        return {}
    return _ledger_summary_payload(session, task, rows)


def _ledger_summary_payload(
    session: Session,
    task: Task,
    rows: list[TaskAccountDailyCoverage],
) -> dict[str, object]:
    target_messages = sum(row.target_count for row in rows)
    confirmed_messages = sum(min(row.target_count, row.confirmed_count) for row in rows)
    covered = sum(1 for row in rows if row.confirmed_count >= row.target_count)
    reason_counts = _ledger_reason_counts(rows)
    remaining_messages = max(0, target_messages - confirmed_messages)
    rate = confirmed_messages / target_messages if target_messages else 0
    capacity = _ledger_capacity_proof(session, task, rows)
    active_hours = max(1, int(capacity.get("active_window_hours") or 1))
    remaining_accounts = len(rows) - covered
    offline_blockers = {
        "account_offline", "session_expired", "session_invalid", "session_missing", "need_relogin",
    }
    return {
        "mode": "all_accounts_daily",
        "covered_count": covered,
        "confirmed_account_count": covered,
        "eligible_count": len(rows),
        "target_account_count": len(rows),
        "target_message_count": target_messages,
        "confirmed_message_count": confirmed_messages,
        "remaining_count": remaining_accounts,
        "remaining_account_count": remaining_accounts,
        "remaining_message_count": remaining_messages,
        "pending_admission_count": _state_count(rows, "pending_admission"),
        "restricted_count": reason_counts.get("cannot_send", 0),
        "cannot_send_count": reason_counts.get("cannot_send", 0),
        "offline_or_session_blocked_count": sum(reason_counts.get(code, 0) for code in offline_blockers),
        "blocked_count": _state_count(rows, "blocked"),
        "unknown_count": _state_count(rows, "unknown"),
        "unknown_after_send_count": _state_count(rows, "unknown"),
        "ready_count": _state_count(rows, "ready"),
        "reserved_count": _state_count(rows, "reserved") + _state_count(rows, "sending"),
        "target_per_account": max(row.target_count for row in rows),
        "coverage_rate": rate,
        "coverage_percent": round(rate * 100),
        "action_types": ["send_message"],
        "statuses": ["confirmed"],
        "blocked_reasons": _ledger_blocked_reasons(task, reason_counts, len(rows) - covered, capacity),
        "capacity_proof": capacity,
        "capacity_status": "sufficient" if capacity.get("sufficient") else "blocked",
        "required_daily_messages": target_messages,
        "required_hourly_rate": (remaining_messages + active_hours - 1) // active_hours,
        "estimated_completion_window": _coverage_estimated_window(task, remaining_messages),
        "pending_accounts": _ledger_pending_accounts(session, rows),
    }


def _ledger_reason_counts(rows: list[TaskAccountDailyCoverage]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if not row.blocker_code:
            continue
        counts[row.blocker_code] = counts.get(row.blocker_code, 0) + 1
    return counts


def _state_count(rows: list[TaskAccountDailyCoverage], state: str) -> int:
    return sum(1 for row in rows if row.state == state)


def _ledger_blocked_reasons(
    task: Task,
    counts: dict[str, int],
    remaining_count: int,
    capacity: dict[str, object],
) -> list[dict[str, object]]:
    reasons = [
        {"reason": reason, "count": count, "message": _blocker_message(reason)}
        for reason, count in sorted(counts.items())
    ]
    if remaining_count and not reasons:
        reasons.append({"reason": "coverage_remaining", "count": remaining_count, "message": "仍有账号未达今日覆盖目标"})
    if capacity and not capacity.get("sufficient"):
        reasons.append({
            "reason": "daily_coverage_capacity_insufficient",
            "count": int(capacity.get("capacity_gap") or 0),
            "message": "任务当前容量无法在活跃窗口内完成全部账号覆盖",
        })
    if task.last_error:
        reasons.append({"reason": "last_error", "count": 1, "message": task.last_error})
    return reasons


def _ledger_capacity_proof(
    session: Session,
    task: Task,
    rows: list[TaskAccountDailyCoverage],
) -> dict[str, object]:
    group_id = int((task.type_config or {}).get("target_group_id") or 0)
    group = session.get(TgGroup, group_id) if group_id else None
    if group is None:
        return {}
    return task_coverage_capacity_proof(
        session,
        task,
        group,
        target_account_count=len(rows),
        target_per_account=max(row.target_count for row in rows),
    )


def _blocker_message(reason: str) -> str:
    labels = {
        "not_in_group": "账号尚未进入目标群",
        "cannot_send": "账号在目标群不可发言",
        "account_limited": "账号当前受限",
        "session_expired": "账号 Session 已失效",
        "unknown_after_send": "发送结果未知，等待远端复核",
        "remote_message_id_missing": "发送缺少 Telegram 远端消息 ID",
        "duplicate_message": "AI 内容重复，等待自然对话重新规划",
    }
    return labels.get(reason, reason)


def _ledger_pending_accounts(session: Session, rows: list[TaskAccountDailyCoverage]) -> list[dict[str, object]]:
    pending = [row for row in rows if row.confirmed_count < row.target_count]
    accounts = {
        account.id: account
        for account in session.scalars(select(TgAccount).where(TgAccount.id.in_([row.account_id for row in pending])))
    } if pending else {}
    return [
        {
            "account_id": row.account_id,
            "display_name": accounts.get(row.account_id).display_name if accounts.get(row.account_id) else "",
            "completed_count": row.confirmed_count,
            "target_count": row.target_count,
            "remaining_count": max(0, row.target_count - row.confirmed_count),
            "reason": row.blocker_code or row.state,
        }
        for row in pending[:10]
    ]


def _coverage_item_payload(row: TaskAccountDailyCoverage, account: TgAccount) -> dict[str, object]:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "display_name": account.display_name,
        "username": account.username or "",
        "coverage_date": row.coverage_date,
        "target_count": row.target_count,
        "confirmed_count": row.confirmed_count,
        "state": row.state,
        "blocker_code": row.blocker_code,
        "blocker_detail": row.blocker_detail,
        "reserved_action_id": row.reserved_action_id,
        "last_success_action_id": row.last_success_action_id,
        "last_remote_message_id": row.last_remote_message_id,
        "next_eligible_at": row.next_eligible_at,
        "targeted_at": row.targeted_at,
        "completed_at": row.completed_at,
    }


def _task_coverage_target_count(task: Task) -> int:
    if task.type != "group_ai_chat":
        return 1
    config = _effective_coverage_config(task)
    if config.get("account_coverage_mode") != "all_accounts_daily":
        return 1
    return max(1, min(2, int(config.get("per_account_daily_min_messages") or 1)))


def _task_coverage_statuses(task: Task) -> tuple[str, ...]:
    if _effective_coverage_config(task).get("account_coverage_mode") == "all_accounts_daily":
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
        value = int(_effective_coverage_config(task).get("coverage_window_hours") or 24)
    except (TypeError, ValueError):
        value = 24
    return max(1, value)


def _effective_coverage_config(task: Task) -> dict[str, object]:
    return apply_group_ai_account_coverage_defaults(task.type, task.type_config or {}, task.account_config or {})


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


__all__ = ["list_task_account_coverage_page", "task_account_coverage"]
