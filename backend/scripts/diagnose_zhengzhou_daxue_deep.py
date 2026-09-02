"""Inspect Zhengzhou Daxue task, group and accounts with retries."""

import time
from sqlalchemy import text, select
from app.database import SessionLocal
from app.models import Task, TgGroup, TgGroupAccount, TgAccount, Action

def run():
    session = SessionLocal()
    try:
        t = session.get(Task, "a52e84f2-8663-4b00-bbbe-196fb626b28d")
        print(f"=== TASK: {t.name} ({t.id}) ===")
        print(f"status: {t.status}, last_error: {t.last_error}, next_run_at: {t.next_run_at}")
        cfg = t.type_config or {}
        gid = cfg.get("target_group_id")
        print(f"target_group_id: {gid}")
        
        if gid:
            grp = session.get(TgGroup, int(gid))
            if grp:
                print(f"
=== GROUP {grp.id} ===")
                print(f"title: {grp.title}, tg_peer_id: {grp.tg_peer_id}, auth_status: {grp.auth_status}, can_send: {grp.can_send}, member_count: {grp.member_count}")
                
                # Check accounts
                cnt = session.scalar(select(text("count(*)")).select_from(TgGroupAccount).where(TgGroupAccount.group_id == int(gid)))
                can_send_cnt = session.scalar(select(text("count(*)")).select_from(TgGroupAccount).where(TgGroupAccount.group_id == int(gid), TgGroupAccount.can_send.is_(True)))
                print(f"GroupAccounts in DB: total={cnt}, can_send={can_send_cnt}")

        # Check latest 5 actions
        actions = list(session.scalars(
            select(Action).where(Action.task_id == t.id).order_by(Action.created_at.desc()).limit(5)
        ).all())
        print(f"
=== LATEST ACTIONS ({len(actions)}) ===")
        for a in actions:
            print(f"- id={a.id}, type={a.action_type}, status={a.status}, gen_status={getattr(a, 'generation_status', None)}, result={a.result}, created_at={a.created_at}")

    finally:
        session.close()

for i in range(3):
    try:
        run()
        break
    except Exception as e:
        print(f"Attempt {i+1} failed: {e}")
        time.sleep(2)
