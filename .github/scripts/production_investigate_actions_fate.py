from __future__ import annotations

import json
from sqlalchemy import text
from app.database import SessionLocal


def main():
    with SessionLocal() as session:
        rows = list(
            session.execute(
                text("""
                    SELECT a.id, a.task_id, t.name AS task_name, a.account_id,
                           a.status, a.scheduled_at, a.executed_at,
                           a.result->>'error' AS result_error,
                           a.result->>'failure_type' AS failure_type,
                           a.result->>'reason' AS result_reason,
                           a.payload->>'message_text' AS message_text,
                           a.payload->>'ai_generation_status' AS gen_status
                    FROM actions AS a
                    JOIN tasks AS t ON t.id = a.task_id
                    WHERE a.task_id IN ('a52e84f2-8663-4b00-bbbe-196fb626b28d', '7fd0bbb7-53dd-45ae-a7af-0c37bcc380d1')
                      AND a.created_at >= NOW() - INTERVAL '40 minutes'
                    ORDER BY a.created_at DESC, a.scheduled_at ASC
                    LIMIT 40
                """)
            ).mappings()
        )
        print(f"ACTIONS_FATE={json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str, indent=2)}")


if __name__ == "__main__":
    main()
