from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import Action, OperationTarget, Task, TgAccount, TgGroup, TgGroupAccount, WorkerHeartbeat
from app.models.enums import AccountStatus


TARGET_ID = 485
TENANT_ID = 1
TASK_TYPE = "target_admission_retry"
ACTIVE_STATUS = AccountStatus.ACTIVE.value
ACTION_LIMIT = 600
FAILED_SAMPLE_LIMIT = 80
RECENT_TASK_LIMIT = 12


def iso(value):
    return value.isoformat() if value else None


def account_row(account, link=None, *, missing_link=False):
    return {
        "account_id": account.id,
        "display_name": account.display_name,
        "username": account.username,
        "phone_masked": account.phone_masked,
        "status": account.status,
        "has_session": bool(account.session_ciphertext),
        "can_send": bool(link.can_send) if link else False,
        "permission_label": link.permission_label if link else "",
        "missing_link": missing_link,
    }


def action_error_key(action):
    result = action.result if isinstance(action.result, dict) else {}
    detail = str(result.get("error_message") or result.get("detail") or result.get("error") or "")[:180]
    return action.status, result.get("error_code"), detail


def task_target_id(task):
    try:
        return int((task.type_config or {}).get("target_operation_target_id") or 0)
    except (TypeError, ValueError):
        return 0


def load_target_group(session):
    target = session.get(OperationTarget, TARGET_ID)
    if not target:
        raise RuntimeError(f"target {TARGET_ID} missing")
    group = session.scalar(
        select(TgGroup).where(
            TgGroup.tenant_id == TENANT_ID,
            TgGroup.tg_peer_id == target.tg_peer_id,
        )
    )
    if not group:
        raise RuntimeError(f"target group {target.tg_peer_id} missing")
    return target, group


def active_account_count(session):
    return session.scalar(
        select(func.count(TgAccount.id)).where(
            TgAccount.tenant_id == TENANT_ID,
            TgAccount.status == ACTIVE_STATUS,
            TgAccount.deleted_at.is_(None),
        )
    )


def link_counts(session, group):
    counts = Counter()
    rows = session.execute(
        select(TgAccount.status, TgGroupAccount.can_send, func.count(TgGroupAccount.id))
        .join(TgAccount, TgAccount.id == TgGroupAccount.account_id)
        .where(
            TgGroupAccount.tenant_id == TENANT_ID,
            TgGroupAccount.group_id == group.id,
            TgAccount.deleted_at.is_(None),
        )
        .group_by(TgAccount.status, TgGroupAccount.can_send)
    )
    for status, can_send, count in rows:
        counts[f"{status}:{bool(can_send)}"] = int(count)
    return dict(counts)


def missing_active_accounts(session, group):
    linked_account_ids = select(TgGroupAccount.account_id).where(
        TgGroupAccount.tenant_id == TENANT_ID,
        TgGroupAccount.group_id == group.id,
    )
    return list(
        session.scalars(
            select(TgAccount)
            .where(
                TgAccount.tenant_id == TENANT_ID,
                TgAccount.status == ACTIVE_STATUS,
                TgAccount.deleted_at.is_(None),
                TgAccount.id.not_in(linked_account_ids),
            )
            .order_by(TgAccount.id.asc())
            .limit(FAILED_SAMPLE_LIMIT)
        )
    )


def failed_link_rows(session, group):
    return list(
        session.execute(
            select(TgAccount, TgGroupAccount)
            .join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id)
            .where(
                TgAccount.tenant_id == TENANT_ID,
                TgAccount.status == ACTIVE_STATUS,
                TgAccount.deleted_at.is_(None),
                TgGroupAccount.tenant_id == TENANT_ID,
                TgGroupAccount.group_id == group.id,
                TgGroupAccount.can_send.is_(False),
            )
            .order_by(TgAccount.id.asc())
            .limit(FAILED_SAMPLE_LIMIT)
        ).all()
    )


def recent_admission_tasks(session):
    tasks = session.scalars(
        select(Task)
        .where(Task.tenant_id == TENANT_ID, Task.type == TASK_TYPE)
        .order_by(Task.created_at.desc())
        .limit(RECENT_TASK_LIMIT)
    )
    return [task for task in tasks if task_target_id(task) == TARGET_ID][:3]


def action_counts(session, task):
    return [
        {"action_type": action_type, "status": status, "count": int(count)}
        for action_type, status, count in session.execute(
            select(Action.action_type, Action.status, func.count(Action.id))
            .where(Action.task_id == task.id)
            .group_by(Action.action_type, Action.status)
            .order_by(Action.action_type.asc(), Action.status.asc())
        )
    ]


def action_error_counts(session, task):
    counter = Counter(
        action_error_key(action)
        for action in session.scalars(
            select(Action)
            .where(
                Action.task_id == task.id,
                Action.status.in_(("failed", "skipped", "unknown_after_send")),
            )
            .order_by(Action.executed_at.desc().nullslast(), Action.created_at.desc())
            .limit(ACTION_LIMIT)
        )
    )
    return [
        {"status": status, "error_code": error_code, "detail": detail, "count": int(count)}
        for (status, error_code, detail), count in counter.most_common()
    ]


def action_error_samples(session, task):
    samples = []
    for action in session.scalars(
        select(Action)
        .where(
            Action.task_id == task.id,
            Action.status.in_(("failed", "skipped", "unknown_after_send")),
        )
        .order_by(Action.executed_at.desc().nullslast(), Action.created_at.desc())
        .limit(20)
    ):
        result = action.result if isinstance(action.result, dict) else {}
        samples.append(
            {
                "action_id": action.id,
                "account_id": action.account_id,
                "status": action.status,
                "error_code": result.get("error_code"),
                "error_message": str(result.get("error_message") or "")[:220],
                "membership_peer_ref": result.get("membership_peer_ref"),
                "membership_fallback_ref": result.get("membership_fallback_ref"),
                "membership_attempted_refs": result.get("membership_attempted_refs") or [],
                "join_request_approval_detail": result.get("join_request_approval_detail"),
                "join_request_link_join_detail": result.get("join_request_link_join_detail"),
                "admin_restriction_lift_detail": result.get("admin_restriction_lift_detail"),
                "verification_task_id": result.get("verification_task_id"),
            }
        )
    return samples


def worker_counts(session, cutoff):
    return {
        str(process_type): int(count)
        for process_type, count in session.execute(
            select(WorkerHeartbeat.process_type, func.count(WorkerHeartbeat.worker_id))
            .where(WorkerHeartbeat.last_seen_at >= cutoff)
            .group_by(WorkerHeartbeat.process_type)
        )
    }


def task_summary(task):
    stats = task.stats or {}
    return {
        "id": task.id,
        "status": task.status,
        "created_at": iso(task.created_at),
        "updated_at": iso(task.updated_at),
        "queued_account_count": stats.get("queued_account_count"),
        "total_actions": stats.get("total_actions"),
        "pending_count": stats.get("pending_count"),
        "failed_count": stats.get("failed_count"),
        "success_count": stats.get("success_count"),
    }


def target_summary(target, group, metrics):
    return {
        "captured_at": metrics["captured_at"].isoformat(timespec="seconds"),
        "target": {
            "id": target.id,
            "title": target.title,
            "tg_peer_id": target.tg_peer_id,
            "username": target.username,
            "auth_status": target.auth_status,
            "can_send": bool(target.can_send),
        },
        "group": {
            "id": group.id,
            "title": group.title,
            "tg_peer_id": group.tg_peer_id,
            "auth_status": group.auth_status,
            "can_send": bool(group.can_send),
        },
        "active_accounts": int(metrics["active_accounts"] or 0),
        "active_link_counts": metrics["link_counts"],
        "active_failed_link_sample_count": metrics["failed_link_count"],
        "active_missing_link_sample_count": metrics["missing_link_count"],
        "worker_counts_5m": metrics["worker_counts"],
    }


def main():
    captured_at = datetime.now(timezone(timedelta(hours=8)))
    heartbeat_cutoff = captured_at.replace(tzinfo=None) - timedelta(minutes=5)
    with SessionLocal() as session:
        target, group = load_target_group(session)
        active_accounts = active_account_count(session)
        counts = link_counts(session, group)
        missing_accounts = missing_active_accounts(session, group)
        failed_rows = failed_link_rows(session, group)
        failed_accounts = [account_row(account, link) for account, link in failed_rows]
        failed_accounts.extend(account_row(account, missing_link=True) for account in missing_accounts)
        tasks = recent_admission_tasks(session)
        latest_task = tasks[0] if tasks else None
        retry = {
            "tasks": [task_summary(task) for task in tasks],
            "latest_action_counts": action_counts(session, latest_task) if latest_task else [],
            "latest_error_counts": action_error_counts(session, latest_task) if latest_task else [],
            "latest_error_samples": action_error_samples(session, latest_task) if latest_task else [],
        }
        metrics = {
            "captured_at": captured_at,
            "active_accounts": active_accounts,
            "link_counts": counts,
            "failed_link_count": len(failed_rows),
            "missing_link_count": len(missing_accounts),
            "worker_counts": worker_counts(session, heartbeat_cutoff),
        }
        summary = target_summary(target, group, metrics)
        print("TIANJIN_LIGHT_SUMMARY=" + json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
        print("TIANJIN_ADMISSION_RETRY_COMPACT=" + json.dumps(retry, ensure_ascii=False, sort_keys=True), flush=True)
        print("TIANJIN_FAILED_ACCOUNTS=" + json.dumps(failed_accounts, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
