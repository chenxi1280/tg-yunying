"""Query exact generation error for Chengdu task."""

from __future__ import annotations
from sqlalchemy import text
from app.database import SessionLocal

def main():
    session = SessionLocal()
    try:
        sql = """
        SELECT id, status, payload ->> 'ai_generation_status' as gen_status,
               payload ->> 'generation_error_message' as gen_err,
               result, created_at
        FROM actions
        WHERE task_id = 'b6f0ebd6-880a-4d6e-9279-04709383486e'
        ORDER BY created_at DESC
        LIMIT 10;
        """
        rows = [dict(r) for r in session.execute(text(sql)).mappings()]
        print("=== CHENGDU ACTIONS LATEST ERRORS ===")
        for r in rows:
            print(f"Action: {r.get('id')}, Status: {r.get('status')}, GenStatus: {r.get('gen_status')}")
            print(f"  GenErr: {r.get('gen_err')}")
            print(f"  Result: {r.get('result')}")
            print(f"  Time: {r.get('created_at')}")
    finally:
        session.close()

if __name__ == "__main__":
    main()
