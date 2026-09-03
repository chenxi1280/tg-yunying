from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.database import SessionLocal
from app.models import Task, TaskDayLedger
from app.services._common import _now
from app.services.task_center.executors.channel_view import build_plan, effective_channel_view_config
from app.services.task_center.executors.channel_view_pacing import effective_channel_view_pacing_config

BEIJING = ZoneInfo("Asia/Shanghai")


def update_and_replan_channel_view_tasks() -> list[dict]:
    results = []
    now = datetime.now(BEIJING)

    with SessionLocal() as session:
        tasks = list(
            session.scalars(
                select(Task).where(
                    Task.status == "running",
                    Task.type == "channel_view",
                    Task.deleted_at.is_(None),
                )
            )
        )

        for task in tasks:
            type_config = dict(task.type_config or {})
            pacing_config = dict(task.pacing_config or {})

            prev_active_days = type_config.get("message_active_days")
            type_config["message_active_days"] = 7
            task.type_config = type_config

            # Set 24h uniform curve
            profile = dict(pacing_config.get("operation_profile") or {})
            prev_curve = profile.get("hourly_activity_curve")
            profile["hourly_activity_curve"] = [1] * 24
            pacing_config["operation_profile"] = profile
            task.pacing_config = pacing_config

            actions_created = 0
            try:
                actions_created = build_plan(session, task)
            except Exception as e:
                actions_created = f"error: {e}"

            session.commit()

            results.append({
                "task_id": task.id,
                "task_name": task.name,
                "prev_active_days": prev_active_days,
                "new_active_days": 7,
                "actions_created": actions_created,
            })

    return results


if __name__ == "__main__":
    res = update_and_replan_channel_view_tasks()
    print("PRODUCTION_CHANNEL_VIEW_UPDATE=" + json.dumps(res, ensure_ascii=False, indent=2))
