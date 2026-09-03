from __future__ import annotations

import json
from datetime import datetime, timezone
from sqlalchemy import text, select
from app.database import SessionLocal
from app.models import Task, Action
from app.services._common import _now


def main():
    with SessionLocal() as session:
        # 1. Task status & pacing config
        tasks = list(
            session.scalars(
                select(Task).where(
                    Task.type == "group_ai_chat",
                    Task.status == "running",
                ).order_by(Task.name)
            )
        )
        task_info = []
        for t in tasks:
            task_info.append({
                "name": t.name,
                "id": t.id,
                "status": t.status,
                "daily_message_target": getattr(t, "daily_message_target", None),
                "pacing_template": (t.pacing_config or {}).get("template"),
                "max_hourly": (t.pacing_config or {}).get("max_actions_per_hour"),
                "config_revision": t.config_revision,
                "epoch": t.task_lifecycle_epoch,
            })
        print(f"EVENING_TASKS_STATUS={json.dumps(task_info, ensure_ascii=False, indent=2)}")

        # 2. Total actions created today per task grouped by status
        action_counts = list(
            session.execute(
                text("""
                    SELECT t.name AS task_name, a.status, COUNT(*) AS count
                    FROM actions AS a
                    JOIN tasks AS t ON t.id = a.task_id
                    WHERE t.type = 'group_ai_chat'
                      AND (a.created_at >= CURRENT_DATE OR a.scheduled_at >= CURRENT_DATE)
                    GROUP BY t.name, a.status
                    ORDER BY t.name, count DESC
                """)
            ).mappings()
        )
        print(f"TODAY_ACTIONS_BY_STATUS={json.dumps([dict(r) for r in action_counts], ensure_ascii=False, indent=2)}")

        # 3. Latest created action across all tasks
        latest_created = list(
            session.execute(
                text("""
                    SELECT a.id, t.name AS task_name, a.status, a.action_type,
                           a.created_at, a.scheduled_at, a.executed_at,
                           a.payload->>'ai_generation_status' AS gen_status,
                           a.result->>'error_code' AS error_code
                    FROM actions AS a
                    JOIN tasks AS t ON t.id = a.task_id
                    WHERE t.type = 'group_ai_chat'
                    ORDER BY a.created_at DESC
                    LIMIT 10
                """)
            ).mappings()
        )
        print(f"LATEST_CREATED_ACTIONS={json.dumps([dict(r) for r in latest_created], ensure_ascii=False, default=str, indent=2)}")


if __name__ == "__main__":
    main()
