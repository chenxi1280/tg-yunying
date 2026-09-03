from __future__ import annotations

import json
from sqlalchemy import text
from app.database import SessionLocal


def main():
    with SessionLocal() as session:
        rows = list(
            session.execute(
                text("""
                    SELECT a.id, t.name AS task_name, a.status, a.created_at, a.executed_at,
                           a.result,
                           a.payload->>'ai_generation_status' AS gen_status,
                           a.payload->>'ai_generation_error' AS gen_error
                    FROM actions AS a
                    JOIN tasks AS t ON t.id = a.task_id
                    WHERE a.result->>'error_code' = 'ai_generation_failed'
                      AND a.created_at >= NOW() - INTERVAL '3 hours'
                    ORDER BY a.created_at DESC
                    LIMIT 5
                """)
            ).mappings()
        )
        print(f"GENERATION_ERROR_DETAILS={json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str, indent=2)}")


if __name__ == "__main__":
    main()
