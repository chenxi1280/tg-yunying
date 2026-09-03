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
                    SELECT id, status, scheduled_at, executed_at,
                           result->>'error_code' AS error_code,
                           result->>'error_type' AS error_type,
                           result->>'error_fingerprint' AS error_fingerprint,
                           payload->>'message_text' AS message_text,
                           payload->>'ai_generation_status' AS gen_status
                    FROM actions
                    WHERE task_id = :task_id
                      AND created_at >= NOW() - INTERVAL '15 minutes'
                    ORDER BY created_at DESC
                    LIMIT 10
                """),
                {"task_id": task_id},
            ).mappings()
        )
        print(f"ZHENGDA_LATEST_ACTIONS={json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str, indent=2)}")


if __name__ == "__main__":
    main()
