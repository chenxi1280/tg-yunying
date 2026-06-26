from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.database import SessionLocal
from app.integrations.telegram.telethon_utils import resolve_telethon_target
from app.models import Action, OperationTarget, Task, Tenant, TgAccount, TgGroup, TgGroupAccount, WorkerHeartbeat
from app.models.enums import AccountStatus
from app.security import decrypt_session
from app.services.developer_apps import credentials_for_account
from app.telethon_lifecycle import TelethonClientLifecycle


TARGET_ID = 485
TENANT_ID = 1
TASK_TYPE = "target_admission_retry"
ACTIVE_STATUS = AccountStatus.ACTIVE.value
ACTION_LIMIT = 600
FAILED_SAMPLE_LIMIT = 80
RECENT_TASK_LIMIT = 12
REMOTE_ADMIN_LIMIT = 50


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


def account_group_link(session, group, account_id):
    return session.scalar(
        select(TgGroupAccount).where(
            TgGroupAccount.tenant_id == TENANT_ID,
            TgGroupAccount.group_id == group.id,
            TgGroupAccount.account_id == account_id,
        )
    )


def account_group_row(session, group, account):
    link = account_group_link(session, group, account.id)
    return account_row(account, link, missing_link=link is None)


def group_rescue_summary(session, group):
    tenant = session.get(Tenant, TENANT_ID)
    account = session.get(TgAccount, tenant.group_rescue_admin_account_id) if tenant and tenant.group_rescue_admin_account_id else None
    return {
        "enabled": bool(tenant and tenant.group_rescue_enabled),
        "admin_account_id": tenant.group_rescue_admin_account_id if tenant else None,
        "admin_account": account_group_row(session, group, account) if account else None,
    }


def explicit_rescue_candidate(session, group):
    account = session.scalar(
        select(TgAccount).where(
            TgAccount.tenant_id == TENANT_ID,
            TgAccount.deleted_at.is_(None),
            TgAccount.username == "settingbother",
        )
    )
    if not account:
        account = session.scalar(
            select(TgAccount).where(
                TgAccount.tenant_id == TENANT_ID,
                TgAccount.deleted_at.is_(None),
                TgAccount.display_name == "管理",
            )
        )
    return account_group_row(session, group, account) if account else None


def rescue_candidate_samples(session, group):
    rows = session.execute(
        select(TgAccount, TgGroupAccount)
        .join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id)
        .where(
            TgAccount.tenant_id == TENANT_ID,
            TgAccount.status == ACTIVE_STATUS,
            TgAccount.deleted_at.is_(None),
            TgAccount.session_ciphertext.is_not(None),
            TgGroupAccount.tenant_id == TENANT_ID,
            TgGroupAccount.group_id == group.id,
            TgGroupAccount.can_send.is_(True),
        )
        .order_by(TgAccount.id.asc())
        .limit(12)
    )
    return [account_row(account, link) for account, link in rows]


def remote_admin_summary(session, target, group):
    tenant = session.get(Tenant, TENANT_ID)
    admin = session.get(TgAccount, tenant.group_rescue_admin_account_id) if tenant and tenant.group_rescue_admin_account_id else None
    if not admin:
        return {"status": "skipped", "detail": "no_configured_rescue_admin", "admins": [], "matched_accounts": []}
    if not admin.session_ciphertext:
        return {"status": "skipped", "detail": "configured_rescue_admin_has_no_session", "admins": [], "matched_accounts": []}
    try:
        raw_session = decrypt_session(admin.session_ciphertext)
        credentials = credentials_for_account(session, admin)
        admins = TelethonClientLifecycle().run(_fetch_remote_admins(raw_session, credentials, target.tg_peer_id))
    except Exception as exc:
        return {"status": "failed", "detail": f"{exc.__class__.__name__}: {exc}", "admins": [], "matched_accounts": []}
    return {
        "status": "ok",
        "source_admin": account_group_row(session, group, admin),
        "admins": admins,
        "matched_accounts": matched_admin_accounts(session, group, admins),
    }


async def _fetch_remote_admins(raw_session, credentials, target_peer_id):
    if not raw_session:
        raise RuntimeError("configured rescue admin session decrypt failed")
    from telethon import types

    lifecycle = TelethonClientLifecycle()
    client = await lifecycle.get_or_create_client(credentials, raw_session)
    if not await client.is_user_authorized():
        raise RuntimeError("configured rescue admin session is unauthorized")
    target = await resolve_telethon_target(client, target_peer_id, group_id=0)
    admins = []
    async for participant in client.iter_participants(target, filter=types.ChannelParticipantsAdmins):
        admins.append(remote_admin_row(participant))
        if len(admins) >= REMOTE_ADMIN_LIMIT:
            break
    return admins


def remote_admin_row(participant):
    first_name = getattr(participant, "first_name", "") or ""
    last_name = getattr(participant, "last_name", "") or ""
    role = type(getattr(participant, "participant", None)).__name__
    return {
        "tg_user_id": str(getattr(participant, "id", "") or ""),
        "username": getattr(participant, "username", None),
        "display_name": f"{first_name} {last_name}".strip(),
        "role": role,
    }


def matched_admin_accounts(session, group, admins):
    usernames = {str(item.get("username") or "").lower() for item in admins if item.get("username")}
    if not usernames:
        return []
    rows = session.scalars(
        select(TgAccount)
        .where(
            TgAccount.tenant_id == TENANT_ID,
            TgAccount.deleted_at.is_(None),
            func.lower(TgAccount.username).in_(usernames),
        )
        .order_by(TgAccount.id.asc())
    )
    return [account_group_row(session, group, account) for account in rows]


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


def pending_action_window(session, task):
    row = session.execute(
        select(
            func.count(Action.id),
            func.min(Action.scheduled_at),
            func.max(Action.scheduled_at),
        ).where(Action.task_id == task.id, Action.status == "pending")
    ).one()
    count, earliest, latest = row
    now = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
    due_count = session.scalar(
        select(func.count(Action.id)).where(
            Action.task_id == task.id,
            Action.status == "pending",
            Action.scheduled_at <= now,
        )
    )
    return {
        "count": int(count or 0),
        "due_count": int(due_count or 0),
        "earliest_scheduled_at": iso(earliest),
        "latest_scheduled_at": iso(latest),
    }


def pending_action_samples(session, task):
    samples = []
    for action in session.scalars(
        select(Action)
        .where(Action.task_id == task.id, Action.status == "pending")
        .order_by(Action.scheduled_at.asc().nullsfirst(), Action.created_at.desc())
        .limit(8)
    ):
        result = action.result if isinstance(action.result, dict) else {}
        samples.append(
            {
                "action_id": action.id,
                "account_id": action.account_id,
                "scheduled_at": iso(action.scheduled_at),
                "error_code": result.get("error_code"),
                "membership_status": result.get("membership_status"),
                "membership_rate_limit_source": result.get("membership_rate_limit_source"),
                "retry_after_seconds": result.get("retry_after_seconds"),
                "next_retry_at": result.get("next_retry_at"),
            }
        )
    return samples


def invite_action_samples(session, task):
    samples = []
    for action in session.scalars(
        select(Action)
        .where(Action.task_id == task.id, Action.action_type == "invite_group_account")
        .order_by(Action.scheduled_at.asc().nullsfirst(), Action.created_at.desc())
        .limit(30)
    ):
        result = action.result if isinstance(action.result, dict) else {}
        payload = action.payload if isinstance(action.payload, dict) else {}
        samples.append(
            {
                "action_id": action.id,
                "status": action.status,
                "account_id": action.account_id,
                "target_account_id": payload.get("target_account_id"),
                "trigger_account_id": payload.get("trigger_account_id"),
                "scheduled_at": iso(action.scheduled_at),
                "executed_at": iso(action.executed_at),
                "error_code": result.get("error_code"),
                "error_message": str(result.get("error_message") or "")[:220],
                "account_policy_action": result.get("account_policy_action"),
                "rescue_status": result.get("rescue_status"),
                "rescue_detail": str(result.get("rescue_detail") or "")[:220],
                "claim_released_reason": result.get("claim_released_reason"),
                "runtime_resource_reason": result.get("runtime_resource_reason"),
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
        "membership_admin_rate_limited_until": stats.get("membership_admin_rate_limited_until"),
        "membership_admin_rate_limit_source": stats.get("membership_admin_rate_limit_source"),
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
            "latest_pending_window": pending_action_window(session, latest_task) if latest_task else {},
            "latest_pending_samples": pending_action_samples(session, latest_task) if latest_task else [],
            "latest_invite_group_account_samples": invite_action_samples(session, latest_task) if latest_task else [],
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
        summary["group_rescue"] = group_rescue_summary(session, group)
        summary["explicit_rescue_candidate"] = explicit_rescue_candidate(session, group)
        summary["rescue_candidate_samples"] = rescue_candidate_samples(session, group)
        summary["remote_admins"] = remote_admin_summary(session, target, group)
        print("TIANJIN_LIGHT_SUMMARY=" + json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
        print("TIANJIN_ADMISSION_RETRY_COMPACT=" + json.dumps(retry, ensure_ascii=False, sort_keys=True), flush=True)
        print("TIANJIN_FAILED_ACCOUNTS=" + json.dumps(failed_accounts, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
