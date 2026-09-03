from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from sqlalchemy import text, select, update
from app.database import SessionLocal
from app.models import Task, TgGroup, TgAccount, Action, TaskAccountDailyCoverage
from app.services.task_center.executors.group_ai_chat import build_plan as build_group_ai_plan
from app.services.task_center.daily_coverage import release_terminal_coverage_reservations
from app.services._common import _now


def rescue_tianjin_music(session) -> dict:
    task_id = "7fd0bbb7-53dd-45ae-a7af-0c37bcc380d1"
    task = session.get(Task, task_id)
    if not task:
        return {"error": "Tianjin music task not found"}
    
    # 1. Update target_group_id to 5999 (@luoyangpiaoch) if needed
    type_config = dict(task.type_config or {})
    current_group_id = type_config.get("target_group_id")
    target_group_id = 5999
    
    if str(current_group_id) != str(target_group_id):
        type_config["target_group_id"] = str(target_group_id)
        task.type_config = type_config
    
    # Ensure group 5999 has can_send and listener_enabled
    group_5999 = session.get(TgGroup, target_group_id)
    if group_5999:
        group_5999.can_send = True
        group_5999.auth_status = "已授权运营"
        group_5999.listener_enabled = True
    
    # 2. Advance scheduled_at for pending membership actions so they execute immediately
    advanced_membership_actions = session.execute(
        update(Action)
        .where(
            Action.task_id == task_id,
            Action.status == "pending",
            Action.action_type == "ensure_target_membership",
        )
        .values(scheduled_at=_now())
    ).rowcount

    session.flush()

    # 3. Trigger build_plan to create initial generic warmup actions
    created_chat_actions = 0
    try:
        created_chat_actions = build_group_ai_plan(session, task)
    except Exception as exc:
        pass

    session.commit()
    return {
        "task_name": task.name,
        "configured_group_id": task.type_config.get("target_group_id"),
        "advanced_membership_actions": advanced_membership_actions,
        "created_chat_actions": created_chat_actions,
    }


def rescue_zhengda(session) -> dict:
    task_id = "a52e84f2-8663-4b00-bbbe-196fb626b28d"
    task = session.get(Task, task_id)
    if not task:
        return {"error": "Zhengda task not found"}

    now_ts = _now()
    # 1. Release terminal coverage reservations from morning failures
    released_coverage = release_terminal_coverage_reservations(session, task, now_ts.date())

    # 2. Reset coverage state to ready for orphaned items
    orphaned_reset = session.execute(
        update(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.task_id == task_id,
            TaskAccountDailyCoverage.coverage_date == now_ts.date(),
            TaskAccountDailyCoverage.state.in_(("unknown", "reserved")),
            TaskAccountDailyCoverage.confirmed_count < TaskAccountDailyCoverage.target_count,
        )
        .values(
            state="ready",
            reserved_action_id=None,
            reservation_token=None,
            blocker_code="",
            updated_at=now_ts,
        )
    ).rowcount

    session.flush()

    # 3. Trigger build_plan to generate afternoon/evening actions
    created_chat_actions = 0
    try:
        created_chat_actions = build_group_ai_plan(session, task)
    except Exception as exc:
        pass

    session.commit()
    return {
        "task_name": task.name,
        "released_coverage": released_coverage,
        "orphaned_reset": orphaned_reset,
        "created_chat_actions": created_chat_actions,
    }


def main():
    with SessionLocal() as session:
        tianjin_res = rescue_tianjin_music(session)
        print(f"RESCUE_TIANJIN_MUSIC={json.dumps(tianjin_res, ensure_ascii=False)}")
        zhengda_res = rescue_zhengda(session)
        print(f"RESCUE_ZHENGDA={json.dumps(zhengda_res, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
