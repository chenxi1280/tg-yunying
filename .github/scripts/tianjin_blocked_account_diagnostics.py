from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Action, OperationTarget, TargetMembershipChallengeAttempt, TgAccount, TgGroup, TgGroupAccount, VerificationTask
from app.services._common import gateway
from app.services.developer_apps import credentials_for_account


TENANT_ID = 1
TARGET_ID = 485
ACCOUNT_ID = 84
ACTION_LIMIT = 12
TASK_LIMIT = 8


def iso(value):
    return value.isoformat() if value else None


def target_group(session, target):
    return session.scalar(select(TgGroup).where(TgGroup.tenant_id == TENANT_ID, TgGroup.tg_peer_id == target.tg_peer_id))


def group_link(session, group_id, account_id):
    return session.scalar(
        select(TgGroupAccount).where(
            TgGroupAccount.tenant_id == TENANT_ID,
            TgGroupAccount.group_id == group_id,
            TgGroupAccount.account_id == account_id,
        )
    )


def account_summary(account, link):
    return {
        "account_id": account.id,
        "display_name": account.display_name,
        "username": account.username,
        "status": account.status,
        "has_session": bool(account.session_ciphertext),
        "link_can_send": bool(link.can_send) if link else False,
        "link_permission_label": link.permission_label if link else "",
        "missing_link": link is None,
    }


def action_rows(session, target_id):
    rows = []
    actions = session.scalars(
        select(Action)
        .where(Action.tenant_id == TENANT_ID, Action.account_id == ACCOUNT_ID)
        .order_by(Action.created_at.desc())
        .limit(80)
    )
    for action in actions:
        payload = action.payload if isinstance(action.payload, dict) else {}
        if int(payload.get("channel_target_id") or payload.get("target_operation_target_id") or 0) != target_id:
            continue
        result = action.result if isinstance(action.result, dict) else {}
        rows.append(
            {
                "action_id": action.id,
                "task_id": action.task_id,
                "task_type": action.task_type,
                "action_type": action.action_type,
                "status": action.status,
                "scheduled_at": iso(action.scheduled_at),
                "executed_at": iso(action.executed_at),
                "error_code": result.get("error_code"),
                "membership_status": result.get("membership_status"),
                "error_message": str(result.get("error_message") or result.get("detail") or "")[:500],
                "verification_task_id": result.get("verification_task_id"),
            }
        )
        if len(rows) >= ACTION_LIMIT:
            break
    return rows


def verification_rows(session, group):
    tasks = session.scalars(
        select(VerificationTask)
        .where(
            VerificationTask.tenant_id == TENANT_ID,
            VerificationTask.account_id == ACCOUNT_ID,
            VerificationTask.group_id == group.id,
        )
        .order_by(VerificationTask.created_at.desc())
        .limit(TASK_LIMIT)
    )
    return [
        {
            "id": task.id,
            "status": task.status,
            "verification_type": task.verification_type,
            "suggested_action": task.suggested_action,
            "detected_reason": task.detected_reason[:500],
            "failure_detail": task.failure_detail[:500],
            "target_peer_id": task.target_peer_id,
            "created_at": iso(task.created_at),
            "handled_at": iso(task.handled_at),
        }
        for task in tasks
    ]


def challenge_rows(session, group):
    rows = session.scalars(
        select(TargetMembershipChallengeAttempt)
        .where(
            TargetMembershipChallengeAttempt.tenant_id == TENANT_ID,
            TargetMembershipChallengeAttempt.account_id == ACCOUNT_ID,
            TargetMembershipChallengeAttempt.group_id == group.id,
        )
        .order_by(TargetMembershipChallengeAttempt.created_at.desc())
        .limit(TASK_LIMIT)
    )
    return [
        {
            "id": row.id,
            "verification_task_id": row.verification_task_id,
            "challenge_type": row.challenge_type,
            "status": row.status,
            "answer_source": row.answer_source,
            "answer_text": row.answer_text,
            "confidence": row.confidence,
            "context_failure_detail": row.context_failure_detail[:500],
            "created_at": iso(row.created_at),
        }
        for row in rows
    ]


def local_tianjin_links(session):
    rows = session.execute(
        select(TgGroup, TgGroupAccount)
        .join(TgGroupAccount, TgGroupAccount.group_id == TgGroup.id)
        .where(
            TgGroup.tenant_id == TENANT_ID,
            TgGroupAccount.account_id == ACCOUNT_ID,
            TgGroup.title.ilike("%天津%"),
        )
        .order_by(TgGroup.id.asc())
    )
    return [
        {
            "group_id": group.id,
            "title": group.title,
            "tg_peer_id": group.tg_peer_id,
            "can_send": bool(link.can_send),
            "permission_label": link.permission_label,
        }
        for group, link in rows
    ]


def live_probe(session, account, target):
    if not account.session_ciphertext:
        return {"ok": False, "failure_type": "ACCOUNT_UNAVAILABLE", "detail": "no_session"}
    try:
        credentials = credentials_for_account(session, account)
        result = gateway.probe_target_capabilities(account.id, target.tg_peer_id, target.target_type, account.session_ciphertext, credentials)
        return {"ok": bool(result.ok), "status": result.status, "failure_type": result.failure_type, "detail": result.detail[:800]}
    except Exception as exc:
        return {"ok": False, "failure_type": exc.__class__.__name__, "detail": str(exc)[:800]}


def main():
    captured_at = datetime.now(timezone(timedelta(hours=8)))
    with SessionLocal() as session:
        target = session.get(OperationTarget, TARGET_ID)
        account = session.get(TgAccount, ACCOUNT_ID)
        group = target_group(session, target) if target else None
        if not target or not account or not group:
            raise RuntimeError("target, account, or group missing")
        link = group_link(session, group.id, account.id)
        payload = {
            "captured_at": captured_at.isoformat(timespec="seconds"),
            "target": {"id": target.id, "title": target.title, "tg_peer_id": target.tg_peer_id, "can_send": bool(target.can_send)},
            "account": account_summary(account, link),
            "live_probe": live_probe(session, account, target),
            "recent_actions": action_rows(session, target.id),
            "verification_tasks": verification_rows(session, group),
            "challenge_attempts": challenge_rows(session, group),
            "local_tianjin_links": local_tianjin_links(session),
        }
        print("TIANJIN_BLOCKED_ACCOUNT_DIAGNOSTICS=" + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
