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
)
from app.services._common import _now


def expand_zhengda_plan_safe(session) -> dict:
    task_id = "a52e84f2-8663-4b00-bbbe-196fb626b28d"
    task = session.get(Task, task_id)
    if not task:
        return {"error": "Zhengda task not found"}

    now_ts = _now()
    log = []

    # 1. Clean stale variation intents from failed actions
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
    log.append(f"deleted_{deleted_intents}_stale_intents")

    # 2. Reset coverage state to ready
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
    log.append(f"reset_{orphaned_reset}_coverage_rows")
    session.commit()

    total_created = 0
    rounds_executed = 0

    for r in range(1, 4):
        # re-get task in fresh session transaction
        task = session.get(Task, task_id)
        try:
            created = build_group_ai_plan(session, task)
            session.commit()
            total_created += created
            rounds_executed += 1
            log.append(f"round_{r}_created_{created}")
            if created == 0:
                break
            time.sleep(1.0)
        except Exception as exc:
            session.rollback()
            log.append(f"round_{r}_exception: {exc}")
            time.sleep(1.0)

    return {
        "task_name": task.name,
        "total_created_chat_actions": total_created,
        "rounds_executed": rounds_executed,
        "log": log,
    }


def query_zhengda_recent_actions(session) -> list[dict]:
    task_id = "a52e84f2-8663-4b00-bbbe-196fb626b28d"
    actions = list(
        session.execute(
            text("""
                SELECT id, account_id, status, scheduled_at, created_at,
                       payload->>'ai_generation_status' AS gen_status,
                       payload->>'message_text' AS message_text,
                       payload->>'act_type' AS act_type
                FROM actions
                WHERE task_id = :task_id
                  AND status = 'pending'
                ORDER BY scheduled_at ASC
                LIMIT 25
            """),
            {"task_id": task_id},
        ).mappings()
    )
    return [dict(a) for a in actions]


def main():
    with SessionLocal() as session:
        zhengda_res = expand_zhengda_plan_safe(session)
        print(f"ZHENGDA_EXPAND_RESULT={json.dumps(zhengda_res, ensure_ascii=False)}")
        actions_res = query_zhengda_recent_actions(session)
        print(f"ZHENGDA_PENDING_SCHEDULE={json.dumps(actions_res, ensure_ascii=False, default=str)}")


if __name__ == "__main__":
    main()
