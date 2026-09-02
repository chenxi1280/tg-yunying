"""Query exact generation error for Chengdu task."""

from __future__ import annotations
from sqlalchemy import text
from app.database import SessionLocal

def main():
    session = SessionLocal()
    try:
        sql = """
        SELECT id, task_id, state, error_code, error_message, created_at, updated_at
        FROM generation_jobs
        WHERE task_id = 'b6f0ebd6-880a-4d6e-9279-04709383486e'
        ORDER BY created_at DESC
        LIMIT 5;
        """
        rows = [dict(r) for r in session.execute(text(sql)).mappings()]
        print("=== CHENGDU GENERATION JOBS LATEST ERRORS ===")
        for r in rows:
            print(f"Job: {r.get('id')}, State: {r.get('state')}, ErrCode: {r.get('error_code')}, ErrMsg: {r.get('error_message')}, Time: {r.get('created_at')}")
            
        sql2 = """
        SELECT id, status, payload ->> 'ai_generation_status' as gen_status, last_error, created_at
        FROM actions
        WHERE task_id = 'b6f0ebd6-880a-4d6e-9279-04709383486e'
        ORDER BY created_at DESC
        LIMIT 5;
        """
        rows2 = [dict(r) for r in session.execute(text(sql2)).mappings()]
        print("\n=== CHENGDU ACTIONS LATEST ERRORS ===")
        for r in rows2:
            print(f"Action: {r.get('id')}, Status: {r.get('status')}, GenStatus: {r.get('gen_status')}, Err: {r.get('last_error')}, Time: {r.get('created_at')}")
    finally:
        session.close()

if __name__ == "__main__":
    main()
