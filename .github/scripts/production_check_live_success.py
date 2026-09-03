from __future__ import annotations

import json
from sqlalchemy import text
from app.database import SessionLocal


def main():
    with SessionLocal() as session:
        # Check success messages today for each group
        stats = list(
            session.execute(
                text("""
                    SELECT t.name AS task_name,
                           COUNT(CASE WHEN a.status IN ('success', 'confirmed') AND (a.scheduled_at >= CURRENT_DATE OR a.executed_at >= CURRENT_DATE) THEN 1 END) AS today_sent_count,
                           COUNT(CASE WHEN a.status = 'pending' AND a.payload->>'ai_generation_status' = 'ready' THEN 1 END) AS ready_pending,
                           MAX(CASE WHEN a.status IN ('success', 'confirmed') THEN a.executed_at END) AS latest_executed_at
                    FROM tasks AS t
                    LEFT JOIN actions AS a ON a.task_id = t.id AND a.action_type = 'send_message'
                    WHERE t.status = 'running' AND t.type = 'group_ai_chat'
                    GROUP BY t.id, t.name
                    ORDER BY today_sent_count DESC
                """)
            ).mappings()
        )
        print(f"LIVE_TASKS_SEND_BREAKDOWN={json.dumps([dict(r) for r in stats], ensure_ascii=False, default=str, indent=2)}")

        # Check latest 10 executed messages across the fleet
        recent_sends = list(
            session.execute(
                text("""
                    SELECT a.id, t.name AS task_name, a.account_id,
                           a.payload->>'message_text' AS message_text,
                           a.executed_at
                    FROM actions AS a
                    JOIN tasks AS t ON t.id = a.task_id
                    WHERE a.status IN ('success', 'confirmed')
                      AND a.action_type = 'send_message'
                      AND a.executed_at >= NOW() - INTERVAL '15 minutes'
                    ORDER BY a.executed_at DESC
                    LIMIT 15
                """)
            ).mappings()
        )
        print(f"RECENT_15M_EXECUTED_MESSAGES={json.dumps([dict(r) for r in recent_sends], ensure_ascii=False, default=str, indent=2)}")


if __name__ == "__main__":
    main()
