from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from sqlalchemy import text, select, update
from app.database import SessionLocal
from app.models import Task, TgGroup, TgAccount, Action, TaskAccountDailyCoverage, OperationTarget
from app.services.task_center.executors.group_ai_chat import (
    build_plan as build_group_ai_plan,
    _load_plan_facts,
    _load_plan_accounts,
    _load_context_plan,
    _load_turn_plan,
    _load_profile_plan,
    _load_generation_plan,
    PlanAbort,
)
from app.services.task_center.daily_coverage import release_terminal_coverage_reservations
from app.services._common import _now


def diagnose_and_rescue_task(session, task_id: str, is_tianjin: bool = False) -> dict:
    task = session.get(Task, task_id)
    if not task:
        return {"error": f"Task {task_id} not found"}

    now_ts = _now()
    log = []

    if is_tianjin:
        type_config = dict(task.type_config or {})
        target_group_id = 5999
        type_config["target_group_id"] = str(target_group_id)

        # Look up existing operation target for @luoyangpiaoch
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
        else:
            type_config.pop("target_operation_target_id", None)
            log.append("unlinked_old_operation_target_using_direct_group_5999")

        task.type_config = type_config
        
        group_5999 = session.get(TgGroup, target_group_id)
        if group_5999:
            group_5999.can_send = True
            group_5999.auth_status = "已授权运营"
            group_5999.listener_enabled = True
            log.append("authorized_group_5999")

        # Fast-forward pending membership actions to execute now
        advanced = session.execute(
            update(Action)
            .where(
                Action.task_id == task_id,
                Action.status == "pending",
                Action.action_type == "ensure_target_membership",
            )
            .values(scheduled_at=now_ts)
        ).rowcount
        log.append(f"advanced_{advanced}_membership_actions_to_now")

    # Release any broken reservations
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
    log.append(f"reset_{orphaned_reset}_orphaned_coverage_rows")
    session.flush()

    # Now execute full build_plan with retry for pacing lock
    created = 0
    for attempt in range(1, 5):
        try:
            created = build_group_ai_plan(session, task)
            log.append(f"build_plan_attempt_{attempt}_succeeded: created={created}")
            break
        except Exception as exc:
            log.append(f"build_plan_attempt_{attempt}_failed: {exc}")
            session.rollback()
            task = session.get(Task, task_id)
            time.sleep(1.0)

    session.commit()
    return {"task_name": task.name, "log": log, "created": created}


def main():
    with SessionLocal() as session:
        tianjin_res = diagnose_and_rescue_task(session, "7fd0bbb7-53dd-45ae-a7af-0c37bcc380d1", is_tianjin=True)
        print(f"RESCUE_TRACE_TIANJIN={json.dumps(tianjin_res, ensure_ascii=False)}")
        zhengda_res = diagnose_and_rescue_task(session, "a52e84f2-8663-4b00-bbbe-196fb626b28d", is_tianjin=False)
        print(f"RESCUE_TRACE_ZHENGDA={json.dumps(zhengda_res, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
