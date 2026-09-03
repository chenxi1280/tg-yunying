from __future__ import annotations

import json
from sqlalchemy import text
from app.database import SessionLocal


def main():
    task_id = "a52e84f2-8663-4b00-bbbe-196fb626b28d"
    with SessionLocal() as session:
        rows = list(
            session.execute(
                text("""
                    SELECT a.id, a.account_id, a.status, a.scheduled_at, a.executed_at,
                           a.payload->>'message_text' AS message_text,
                           a.result->>'error' AS result_error,
                           a.result->>'failure_type' AS failure_type,
                           a.result->>'error_code' AS error_code,
                           a.result->>'telegram_msg_id' AS telegram_msg_id,
                           a.result->>'success' AS result_success
                    FROM actions AS a
                    WHERE a.task_id = :task_id
                      AND a.action_type = 'send_message'
                      AND a.created_at >= NOW() - INTERVAL '15 minutes'
                    ORDER BY a.created_at DESC, a.scheduled_at ASC
                    LIMIT 10
                """),
                {"task_id": task_id},
            ).mappings()
        )
        print(f"ZHENGDA_1455_ACTIONS_RESULT={json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str, indent=2)}")


if __name__ == "__main__":
    main()
