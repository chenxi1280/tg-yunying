"""Query Zhengzhou Daxue task details."""

from __future__ import annotations
from sqlalchemy import text, select
from app.database import SessionLocal
from app.models import Task, TgGroup, TgGroupAccount, Action

def main():
    session = SessionLocal()
    try:
        t = session.get(Task, "a52e84f2-8663-4b00-bbbe-196fb626b28d")
        print(f"Task [郑州大学]: status={t.status}, next_run_at={t.next_run_at}, last_error={t.last_error}")
        cfg = t.type_config or {}
        gid = cfg.get("target_group_id")
        print(f"target_group_id: {gid}")
        
        if gid:
            grp = session.get(TgGroup, int(gid))
            if grp:
                print(f"Group: id={grp.id}, title={grp.title}, peer_id={grp.tg_peer_id}, member_count={grp.member_count}, can_send={grp.can_send}")
                acc_cnt = session.scalar(select(text("count(*)")).select_from(TgGroupAccount).where(TgGroupAccount.group_id == int(gid)))
                can_send_cnt = session.scalar(select(text("count(*)")).select_from(TgGroupAccount).where(TgGroupAccount.group_id == int(gid), TgGroupAccount.can_send.is_(True)))
                print(f"Accounts in DB: total={acc_cnt}, can_send={can_send_cnt}")

        # Recent actions
        actions = list(session.scalars(
            select(Action).where(Action.task_id == t.id).order_by(Action.created_at.desc()).limit(5)
        ).all())
        print(f"
Recent Actions ({len(actions)}):")
        for a in actions:
            print(f"  - id={a.id}, type={a.action_type}, status={a.status}, result={a.result}, time={a.created_at}")

    finally:
        session.close()

if __name__ == "__main__":
    main()
