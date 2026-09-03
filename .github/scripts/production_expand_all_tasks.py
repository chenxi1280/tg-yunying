from __future__ import annotations

import json
import time
import traceback
from datetime import datetime
from sqlalchemy import text, select, update
from app.database import SessionLocal
from app.models import Task, TgGroup, TgAccount, Action, TaskAccountDailyCoverage
from app.services.task_center.executors.group_ai_chat import (
    build_plan as build_group_ai_plan,
)
from app.services._common import _now


def expand_all_running_tasks() -> list[dict]:
    results = []
    now_ts = _now()
    today = now_ts.date()

    with SessionLocal() as session:
        tasks = list(
            session.scalars(
                select(Task).where(
                    Task.status == "running",
                    Task.type == "group_ai_chat",
                ).order_by(Task.name)
            )
        )

        for task in tasks:
            task_id = task.id
            task_name = task.name
            
            # 1. Clean stale variation intents from failed actions for today
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

            # 2. Reset unknown / orphaned coverage rows
            orphaned_reset = session.execute(
                update(TaskAccountDailyCoverage)
                .where(
                    TaskAccountDailyCoverage.task_id == task_id,
                    TaskAccountDailyCoverage.coverage_date == today,
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
            session.commit()

            # 3. Generate 1~2 batches of actions for this task
            created_total = 0
            for r in range(2):
                task = session.get(Task, task_id)
                try:
                    created = build_group_ai_plan(session, task)
                    session.commit()
                    created_total += created
                    if created == 0:
                        break
                    time.sleep(0.5)
                except Exception as exc:
                    session.rollback()
                    break

            results.append({
                "task_name": task_name,
                "task_id": task_id,
                "deleted_intents": deleted_intents,
                "orphaned_reset": orphaned_reset,
                "created_actions": created_total,
            })

    return results


def main():
    res = expand_all_running_tasks()
    print(f"EXPAND_ALL_TASKS_RESULT={json.dumps(res, ensure_ascii=False, indent=2)}")


if __name__ == "__main__":
    main()
