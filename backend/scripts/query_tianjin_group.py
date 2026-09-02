"""Query Tianjin group by tjlove666 or 逃学威龙."""

from __future__ import annotations
from sqlalchemy import text, select
from app.database import SessionLocal
from app.models import TgGroup, TgGroupAccount, Task

def main():
    session = SessionLocal()
    try:
        sql = """
        SELECT id, tg_peer_id, title, auth_status, member_count, can_send, active_window, created_at
        FROM tg_groups
        WHERE tg_peer_id ILIKE '%tjlove666%' OR title ILIKE '%逃学威龙%' OR title ILIKE '%天津%音乐%' OR tg_peer_id ILIKE '%luoyang%';
        """
        rows = [dict(r) for r in session.execute(text(sql)).mappings()]
        print(f"=== FOUND {len(rows)} MATCHING GROUPS ===")
        for r in rows:
            gid = r.get("id")
            cnt = session.scalar(select(text("count(*)")).select_from(TgGroupAccount).where(TgGroupAccount.group_id == gid))
            print(f"ID: {gid}, Title: {r.get('title')}, PeerID: {r.get('tg_peer_id')}, Auth: {r.get('auth_status')}, AccountsInDB: {cnt}")

        # Check current task 7fd0bbb7
        t = session.get(Task, "7fd0bbb7-53dd-45ae-a7af-0c37bcc380d1")
        if t:
            print(f"\nTask [7fd0bbb7] Name: {t.name}, Status: {t.status}")
            print(f"Type Config: {t.type_config}")
    finally:
        session.close()

if __name__ == "__main__":
    main()
