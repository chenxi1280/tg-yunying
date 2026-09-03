from __future__ import annotations

import json
from datetime import datetime
from sqlalchemy import select, update
from app.database import SessionLocal
from app.models import Task, TaskGroupDailyTarget, TgGroup
from app.services._common import _now
from app.services.task_center.daily_group_target import refresh_task_group_daily_target


def update_all_group_daily_targets(target_value: int = 4200) -> list[dict]:
    results = []
    now_ts = _now()
    today = now_ts.date()

    with SessionLocal() as session:
        tasks = list(
            session.scalars(
                select(Task).where(
                    Task.status == "running",
                    Task.type == "group_ai_chat",
                )
            )
        )

        for task in tasks:
            type_config = dict(task.type_config or {})
            pacing_config = dict(task.pacing_config or {})
            prev_type_target = type_config.get("daily_message_target")
            prev_pacing_target = pacing_config.get("daily_message_target")

            type_config["daily_message_target"] = target_value
            pacing_config["daily_message_target"] = target_value
            task.type_config = type_config
            task.pacing_config = pacing_config

            # Update or refresh TaskGroupDailyTarget
            group_id = int(type_config.get("target_group_id") or 0)
            daily_target = session.scalar(
                select(TaskGroupDailyTarget).where(
                    TaskGroupDailyTarget.tenant_id == task.tenant_id,
                    TaskGroupDailyTarget.task_id == task.id,
                    TaskGroupDailyTarget.target_date == today,
                )
            )

            prev_group_configured = None
            prev_group_effective = None
            if daily_target:
                prev_group_configured = daily_target.configured_message_target
                prev_group_effective = daily_target.effective_message_target
                daily_target.configured_message_target = target_value
                daily_target.effective_message_target = max(target_value, daily_target.frozen_account_count or 0)
                daily_target.planned_daily_target = daily_target.effective_message_target
                daily_target.target_change_reason = "user_updated_per_group_daily_target_4200"
                daily_target.target_changed_at = now_ts

            results.append({
                "task_id": task.id,
                "task_name": task.name,
                "group_id": group_id,
                "prev_type_target": prev_type_target,
                "prev_group_configured": prev_group_configured,
                "new_target_per_group": target_value,
                "effective_target": daily_target.effective_message_target if daily_target else target_value,
            })

        session.commit()
    return results


def main():
    updated = update_all_group_daily_targets(4200)
    print(f"UPDATED_GROUP_TARGETS={json.dumps(updated, ensure_ascii=False, indent=2)}")


if __name__ == "__main__":
    main()
