from __future__ import annotations

import json
from sqlalchemy import text, select
from app.database import SessionLocal
from app.models import Task, TgGroup, Action


def deep_diagnostics():
    with SessionLocal() as session:
        tasks = list(
            session.scalars(
                select(Task).where(
                    Task.status == "running",
                    Task.type == "group_ai_chat",
                ).order_by(Task.name)
            )
        )
        
        task_configs = []
        for t in tasks:
            task_configs.append({
                "task_id": t.id,
                "task_name": t.name,
                "epoch": t.task_lifecycle_epoch,
                "pacing_config": t.pacing_config,
                "type_config_keys": list((t.type_config or {}).keys()),
                "messages_per_round": (t.type_config or {}).get("messages_per_round"),
                "round_interval": (t.type_config or {}).get("round_interval"),
                "round_interval_minutes": (t.type_config or {}).get("round_interval_minutes"),
                "ai_reply_ratio": (t.type_config or {}).get("ai_reply_ratio"),
                "pending_actions": session.scalar(
                    select(text("COUNT(*)")).select_from(Action).where(
                        Action.task_id == t.id, Action.status == "pending"
                    )
                ),
                "today_success": session.scalar(
                    select(text("COUNT(*)")).select_from(Action).where(
                        Action.task_id == t.id,
                        Action.status.in_(("success", "confirmed")),
                        Action.executed_at >= text("CURRENT_DATE"),
                    )
                ),
            })
        print(f"ALL_TASKS_CONFIG_DEEP_DIVE={json.dumps(task_configs, ensure_ascii=False, indent=2)}")

        # Check Zhengda actions statuses today
        zhengda_stats = list(
            session.execute(
                text("""
                    SELECT status, action_type, payload->>'ai_generation_status' AS gen_status, COUNT(*) AS cnt
                    FROM actions
                    WHERE task_id = 'a52e84f2-8663-4b00-bbbe-196fb626b28d'
                      AND (scheduled_at >= CURRENT_DATE OR executed_at >= CURRENT_DATE)
                    GROUP BY status, action_type, payload->>'ai_generation_status'
                    ORDER BY cnt DESC
                """)
            ).mappings()
        )
        print(f"ZHENGDA_TODAY_ACTIONS={json.dumps([dict(r) for r in zhengda_stats], ensure_ascii=False, indent=2)}")

        # Check Tianjin Music recent actions
        tianjin_stats = list(
            session.execute(
                text("""
                    SELECT status, action_type, COUNT(*) AS cnt,
                           MIN(scheduled_at) AS min_sched, MAX(scheduled_at) AS max_sched
                    FROM actions
                    WHERE task_id = '7fd0bbb7-53dd-45ae-a7af-0c37bcc380d1'
                      AND created_at >= NOW() - INTERVAL '2 hours'
                    GROUP BY status, action_type
                """)
            ).mappings()
        )
        print(f"TIANJIN_RECENT_ACTIONS={json.dumps([dict(r) for r in tianjin_stats], ensure_ascii=False, default=str, indent=2)}")


if __name__ == "__main__":
    deep_diagnostics()
