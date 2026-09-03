from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from sqlalchemy import text, select, update, delete
from app.database import SessionLocal
from app.models import Task, TgGroup, TgAccount, Action, TaskAccountDailyCoverage, OperationTarget
from app.services.task_center.executors.group_ai_chat import (
    build_plan as build_group_ai_plan,
    _prepare_plan_blueprint,
    _prepare_action_slots,
    _prepared_plan_is_blocked,
    _create_reserved_actions,
    PlanAbort,
)
from app.services.task_center.account_scope import bootstrap_missing_all_account_task_scope
from app.services.task_center.membership_admission import plan_membership_admission_actions
from app.services._common import _now


def rescue_tianjin_music(session) -> dict:
    task_id = "7fd0bbb7-53dd-45ae-a7af-0c37bcc380d1"
    task = session.get(Task, task_id)
    if not task:
        return {"error": "Tianjin task not found"}

    now_ts = _now()
    log = []
    target_group_id = 5999

    type_config = dict(task.type_config or {})
    type_config["target_group_id"] = str(target_group_id)

    existing_op = session.scalar(
        select(OperationTarget).where(
            OperationTarget.tenant_id == task.tenant_id,
            OperationTarget.tg_peer_id == "@luoyangpiaoch",
        )
    )
    if existing_op:
        type_config["target_operation_target_id"] = str(existing_op.id)
        existing_op.auth_status = "已授权运营"
        existing_op.can_send = True
        log.append(f"bound_to_existing_operation_target_{existing_op.id}")

    task.type_config = type_config
    
    group_5999 = session.get(TgGroup, target_group_id)
    if group_5999:
        group_5999.can_send = True
        group_5999.auth_status = "已授权运营"
        group_5999.listener_enabled = True
        log.append("authorized_group_5999")

    # Clean up old actions with stale group_id
    deleted_old_actions = session.execute(
        text("""
            DELETE FROM actions
            WHERE task_id = :task_id
              AND status = 'pending'
              AND action_type = 'ensure_target_membership'
              AND (payload->>'target_group_id' != '5999' OR payload->>'target_group_id' IS NULL)
        """),
        {"task_id": task_id},
    ).rowcount
    log.append(f"deleted_{deleted_old_actions}_stale_group_actions")

    # Bootstrap all account task scope for group 5999
    bootstrap_missing_all_account_task_scope(session, task, now=now_ts)
    log.append("bootstrapped_account_task_scope")

    session.flush()

    # Create new membership admission actions for group 5999
    created_admission = plan_membership_admission_actions(session, task, now=now_ts, limit=50)
    log.append(f"created_{len(created_admission)}_fresh_admission_actions_for_5999")

    # Fast forward all pending admission actions to execute now
    advanced = session.execute(
        update(Action)
        .where(
            Action.task_id == task_id,
            Action.status == "pending",
            Action.action_type == "ensure_target_membership",
        )
        .values(scheduled_at=now_ts)
    ).rowcount
    log.append(f"advanced_{advanced}_admission_actions_to_now")

    session.commit()
    return {"task_name": task.name, "log": log}


def expand_zhengda_plan(session) -> dict:
    task_id = "a52e84f2-8663-4b00-bbbe-196fb626b28d"
    task = session.get(Task, task_id)
    if not task:
        return {"error": "Zhengda task not found"}

    total_created = 0
    rounds_executed = 0

    for r in range(5):
        try:
            created = build_group_ai_plan(session, task)
            total_created += created
            rounds_executed += 1
            if created == 0:
                break
        except Exception as exc:
            session.rollback()
            task = session.get(Task, task_id)
            break

    session.commit()
    return {
        "task_name": task.name,
        "total_created_chat_actions": total_created,
        "rounds_executed": rounds_executed,
    }


def main():
    with SessionLocal() as session:
        tianjin_res = rescue_tianjin_music(session)
        print(f"RESCUE_TIANJIN_MUSIC_FINAL={json.dumps(tianjin_res, ensure_ascii=False)}")
        zhengda_res = expand_zhengda_plan(session)
        print(f"EXPAND_ZHENGDA_FINAL={json.dumps(zhengda_res, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
