from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import Action, OperationTarget, Task, TgAccount, TgGroup, TgGroupAccount
from app.models.enums import AccountStatus


TENANT_ID = 1
TARGET_ID = 485
TASK_TYPE = "target_admission_retry"
PENDING_STATUSES = {"pending", "claiming"}
ACTIVE_STATUS = AccountStatus.ACTIVE.value


def now_value() -> datetime:
    return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)


def load_target_group(session):
    target = session.get(OperationTarget, TARGET_ID)
    if not target:
        raise RuntimeError(f"target {TARGET_ID} missing")
    group = session.scalar(
        select(TgGroup).where(TgGroup.tenant_id == TENANT_ID, TgGroup.tg_peer_id == target.tg_peer_id)
    )
    if not group:
        raise RuntimeError(f"target group {target.tg_peer_id} missing")
    return target, group


def remaining_account_ids(session, group: TgGroup) -> set[int]:
    return set(
        session.scalars(
            select(TgGroupAccount.account_id)
            .join(TgAccount, TgAccount.id == TgGroupAccount.account_id)
            .where(
                TgGroupAccount.tenant_id == TENANT_ID,
                TgGroupAccount.group_id == group.id,
                TgGroupAccount.can_send.is_(False),
                TgAccount.tenant_id == TENANT_ID,
                TgAccount.status == ACTIVE_STATUS,
                TgAccount.deleted_at.is_(None),
            )
        )
    )


def admission_tasks(session) -> list[Task]:
    tasks = session.scalars(
        select(Task)
        .where(Task.tenant_id == TENANT_ID, Task.type == TASK_TYPE, Task.status == "running")
        .order_by(Task.created_at.desc())
        .limit(20)
    )
    return [task for task in tasks if task_target_id(task) == TARGET_ID]


def task_target_id(task: Task) -> int:
    try:
        return int((task.type_config or {}).get("target_operation_target_id") or 0)
    except (TypeError, ValueError):
        return 0


def action_account_id(action: Action) -> int | None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    if action.action_type in {"ensure_target_membership", "ensure_channel_membership"}:
        return int(action.account_id) if action.account_id else None
    if action.action_type == "invite_group_account":
        value = payload.get("target_account_id") or payload.get("trigger_account_id")
        return int(value) if value else None
    return None


def cleanup_reason(action: Action, canonical_task_id: str, remaining_ids: set[int], seen: set[tuple[str, int]]) -> str:
    account_id = action_account_id(action)
    if action.task_id != canonical_task_id:
        return "older_duplicate_retry_task"
    if not account_id or account_id not in remaining_ids:
        return "target_account_already_joined_or_inactive"
    key = (str(action.action_type), account_id)
    if key in seen:
        return "duplicate_pending_action"
    seen.add(key)
    return ""


def skip_action(action: Action, reason: str, at: datetime) -> None:
    result = action.result if isinstance(action.result, dict) else {}
    action.status = "skipped"
    action.executed_at = at
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.lease_owner = ""
    action.lease_expires_at = None
    action.result = {
        **result,
        "error_code": "admission_retry_backlog_deduped",
        "error_message": reason,
        "cleanup_reason": reason,
        "cleanup_at": at.isoformat(),
    }


def pending_actions(session, tasks: list[Task]) -> list[Action]:
    if not tasks:
        return []
    task_ids = [task.id for task in tasks]
    return list(
        session.scalars(
            select(Action)
            .where(
                Action.tenant_id == TENANT_ID,
                Action.task_id.in_(task_ids),
                Action.status.in_(PENDING_STATUSES),
                Action.action_type.in_(("ensure_target_membership", "invite_group_account")),
            )
            .order_by(Action.task_id.asc(), Action.action_type.asc(), Action.scheduled_at.asc())
        )
    )


def close_empty_duplicate_tasks(session, tasks: list[Task], canonical_task_id: str) -> int:
    closed = 0
    for task in tasks:
        if task.id == canonical_task_id:
            continue
        active_count = session.scalar(
            select(func.count(Action.id)).where(
                Action.task_id == task.id,
                Action.status.in_(("pending", "claiming", "executing")),
            )
        )
        if int(active_count or 0) > 0:
            continue
        task.status = "completed"
        task.stats = {**(task.stats or {}), "completed_by_cleanup": True, "cleanup_reason": "duplicate_admission_retry_task"}
        closed += 1
    return closed


def main() -> None:
    at = now_value()
    with SessionLocal() as session:
        _target, group = load_target_group(session)
        remaining_ids = remaining_account_ids(session, group)
        tasks = admission_tasks(session)
        canonical = tasks[0] if tasks else None
        seen: set[tuple[str, int]] = set()
        skipped = Counter()
        if canonical:
            for action in pending_actions(session, tasks):
                reason = cleanup_reason(action, canonical.id, remaining_ids, seen)
                if not reason:
                    continue
                skip_action(action, reason, at)
                skipped[reason] += 1
            canonical.account_config = {**(canonical.account_config or {}), "account_ids": sorted(remaining_ids)}
            canonical.stats = {**(canonical.stats or {}), "queued_account_count": len(remaining_ids), "cleanup_at": at.isoformat()}
        closed_tasks = close_empty_duplicate_tasks(session, tasks, canonical.id if canonical else "")
        session.commit()
        summary = {
            "target_id": TARGET_ID,
            "canonical_task_id": canonical.id if canonical else "",
            "running_task_count_before": len(tasks),
            "remaining_account_count": len(remaining_ids),
            "skipped_actions": dict(skipped),
            "closed_duplicate_task_count": closed_tasks,
        }
        print("TIANJIN_ADMISSION_BACKLOG_CLEANUP=" + json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
