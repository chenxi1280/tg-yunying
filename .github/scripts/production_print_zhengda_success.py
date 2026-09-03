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
                    SELECT a.id, a.account_id, a.status, a.executed_at,
                           a.payload->>'message_text' AS message_text,
                           a.result->>'telegram_msg_id' AS telegram_msg_id,
                           a.result->>'gateway_target_fingerprint' AS gateway_target_fingerprint
                    FROM actions AS a
                    WHERE a.task_id = :task_id
                      AND a.status = 'success'
                      AND (a.executed_at >= CURRENT_DATE OR a.scheduled_at >= CURRENT_DATE)
                    ORDER BY a.executed_at DESC
                    LIMIT 10
                """),
                {"task_id": task_id},
            ).mappings()
        )
        print(f"ZHENGDA_SUCCESS_REAL_MESSAGES={json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str, indent=2)}")


if __name__ == "__main__":
    main()
