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

    # 1. Clean up stale variation intents from failed actions
    deleted_intents = session.execute(
        text("""
            DELETE FROM ai_coverage_variation_intents
            WHERE coverage_ledger_id IN (
                SELECT id FROM task_account_daily_coverage
                WHERE task_id = :task_id AND coverage_date = CURRENT_DATE
            )
            AND (action_id IS NULL OR action_id IN (
                SELECT id FROM actions WHERE task_id = :task_id AND status = 'failed'
            ))
        """),
        {"task_id": task_id},
    ).rowcount
    log.append(f"deleted_{deleted_intents}_stale_variation_intents")

    # 2. Release any broken reservations
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

    blueprint = _prepare_plan_blueprint(session, task)
    if isinstance(blueprint, PlanAbort):
        log.append(f"blueprint_aborted: created={blueprint.created}, last_error={task.last_error}")
        session.commit()
        return {"task_name": task.name, "log": log, "created": 0}

    log.append(f"blueprint_ready: turn_count={blueprint.turn.turn_count}, requested_reply={blueprint.generation.requested_reply_count}")

    prepared = _prepare_action_slots(session, task, blueprint, None)
    prepared_reply_count = sum(1 for slot in prepared.slots if slot.payload.reply_to_message_id)
    log.append(f"prepared_ready: slot_count={len(prepared.slots)}, prepared_reply_count={prepared_reply_count}")

    is_blocked = _prepared_plan_is_blocked(task, blueprint, prepared, prepared_reply_count=prepared_reply_count)
    log.append(f"is_blocked={is_blocked}, last_error={task.last_error}")

    created = 0
    if not is_blocked:
        created = _create_reserved_actions(session, task, blueprint=blueprint, prepared=prepared)
        log.append(f"create_reserved_actions: created={created}")

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
