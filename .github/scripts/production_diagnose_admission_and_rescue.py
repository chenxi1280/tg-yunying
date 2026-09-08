from __future__ import annotations

import json
from sqlalchemy import func, select, text

from app.database import SessionLocal
from app.models import (
    Action,
    ExecutionAttempt,
    Task,
    TaskAccountDailyCoverage,
    TaskMembershipAdmissionItem,
    Tenant,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)


def diagnose_tasks_sync() -> dict:
    with SessionLocal() as session:
        tenant = session.get(Tenant, 1)
        tenant_info = {
            "id": tenant.id if tenant else None,
            "group_rescue_enabled": tenant.group_rescue_enabled if tenant else None,
            "group_rescue_admin_account_id": tenant.group_rescue_admin_account_id if tenant else None,
        }
        admin_acc = session.get(TgAccount, tenant.group_rescue_admin_account_id) if tenant and tenant.group_rescue_admin_account_id else None
        if admin_acc:
            tenant_info["admin_account"] = {
                "id": admin_acc.id,
                "phone_masked": admin_acc.phone_masked,
                "status": admin_acc.status,
                "has_session": bool(admin_acc.session_ciphertext),
                "display_name": admin_acc.display_name,
                "username": admin_acc.username,
            }
        else:
            tenant_info["admin_account"] = None

        target_keywords = ["郑州大学", "西安天上人间", "三亚", "天津一品楼"]
        tasks_report = []

        for kw in target_keywords:
            task = session.scalar(
                select(Task).where(
                    Task.name.like(f"%{kw}%"),
                    Task.status == "running",
                    Task.deleted_at.is_(None),
                )
            )
            if not task:
                tasks_report.append({"keyword": kw, "error": "Task not found"})
                continue

            config = task.type_config or {}
            target_group_id = config.get("target_group_id") or config.get("group_id")
            group = session.get(TgGroup, target_group_id) if target_group_id else None

            # 1. Group info
            group_info = None
            if group:
                group_info = {
                    "id": group.id,
                    "title": group.title,
                    "group_type": group.group_type,
                    "tg_peer_id": group.tg_peer_id,
                    "can_send": group.can_send,
                    "auth_status": group.auth_status,
                }

            # 2. Admission items breakdown
            adm_breakdown = list(
                session.execute(
                    text("""
                        SELECT phase, failure_type, rescue_status, COUNT(*) as cnt
                        FROM task_membership_admission_items
                        WHERE task_id = :task_id
                        GROUP BY phase, failure_type, rescue_status
                        ORDER BY cnt DESC
                    """),
                    {"task_id": task.id},
                ).mappings()
            )

            # 3. tg_group_accounts breakdown
            group_acc_breakdown = None
            if group:
                group_acc_breakdown = list(
                    session.execute(
                        text("""
                            SELECT can_send, permission_label, COUNT(*) as cnt
                            FROM tg_group_accounts
                            WHERE group_id = :group_id
                            GROUP BY can_send, permission_label
                            ORDER BY cnt DESC
                        """),
                        {"group_id": group.id},
                    ).mappings()
                )

            # 4. Ready pending send_message actions and sender admission status
            ready_actions = list(
                session.execute(
                    text("""
                        SELECT a.id, a.account_id, a.scheduled_at,
                               adm.phase AS admission_phase,
                               adm.failure_type AS admission_failure_type,
                               tga.can_send AS tga_can_send,
                               tga.permission_label AS tga_permission_label
                        FROM actions a
                        LEFT JOIN task_membership_admission_items adm
                               ON adm.task_id = a.task_id AND adm.account_id = a.account_id
                        LEFT JOIN tg_group_accounts tga
                               ON tga.group_id = :group_id AND tga.account_id = a.account_id
                        WHERE a.task_id = :task_id
                          AND a.action_type = 'send_message'
                          AND a.status = 'pending'
                          AND a.payload->>'ai_generation_status' = 'ready'
                        ORDER BY a.scheduled_at ASC
                        LIMIT 10
                    """),
                    {"task_id": task.id, "group_id": group.id if group else 0},
                ).mappings()
            )

            # 5. Rescue actions for this task
            rescue_actions = list(
                session.execute(
                    text("""
                        SELECT id, action_type, account_id, status, scheduled_at, executed_at,
                               payload->>'trigger_account_id' as trigger_acc,
                               payload->>'trigger_reason' as trigger_reason,
                               result->>'rescue_status' as rescue_status,
                               result->>'error_message' as error_message,
                               result->>'detail' as detail
                        FROM actions
                        WHERE task_id = :task_id
                          AND action_type IN ('invite_group_account', 'invite_group_bot')
                        ORDER BY created_at DESC
                        LIMIT 10
                    """),
                    {"task_id": task.id},
                ).mappings()
            )

            # 6. Recent failed or unknown attempts for send_message
            recent_failed_attempts = list(
                session.execute(
                    text("""
                        SELECT att.id, att.action_id, att.account_id, att.status,
                               att.failure_type, att.detail, att.created_at
                        FROM execution_attempts att
                        JOIN actions a ON a.id = att.action_id
                        WHERE a.task_id = :task_id
                          AND a.action_type = 'send_message'
                          AND att.created_at >= NOW() - INTERVAL '2 hours'
                        ORDER BY att.created_at DESC
                        LIMIT 10
                    """),
                    {"task_id": task.id},
                ).mappings()
            )

            tasks_report.append({
                "task_id": task.id,
                "task_name": task.name,
                "epoch": task.task_lifecycle_epoch,
                "contract_version": task.fulfillment_contract_version,
                "group_bot_admission_required": config.get("group_bot_admission_required"),
                "task_rescue_admin": config.get("group_rescue_admin_account_id"),
                "group": group_info,
                "admission_items_breakdown": [dict(r) for r in adm_breakdown],
                "tg_group_accounts_breakdown": [dict(r) for r in group_acc_breakdown] if group_acc_breakdown else None,
                "sample_ready_actions_status": [dict(r) for r in ready_actions],
                "rescue_actions": [dict(r) for r in rescue_actions],
                "recent_failed_attempts": [dict(r) for r in recent_failed_attempts],
            })

        return {
            "tenant_info": tenant_info,
            "tasks_report": tasks_report,
        }


def main():
    report = diagnose_tasks_sync()
    print("=" * 80)
    print("【群入群检查与管理账号处理深度诊断报告】")
    print("=" * 80)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
