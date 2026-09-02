"""Inspect Zhengzhou Daxue task, group and accounts directly."""

from __future__ import annotations
import sys
from sqlalchemy import text, select
from app.database import SessionLocal
from app.models import Task, TgGroup, TgGroupAccount, TgAccount, Action

def main():
    print("=== STARTING DIAGNOSE ZHENGZHOU DAXUE ===", flush=True)
    session = SessionLocal()
    try:
        t = session.get(Task, "a52e84f2-8663-4b00-bbbe-196fb626b28d")
        if not t:
            print("Task NOT FOUND!", flush=True)
            return

        print(f"TASK_NAME: {t.name}, STATUS: {t.status}, LAST_ERROR: {t.last_error}, NEXT_RUN: {t.next_run_at}", flush=True)
        cfg = dict(t.type_config or {})
        gid = cfg.get("target_group_id")
        print(f"TARGET_GROUP_ID: {gid}", flush=True)
        print(f"AI_ROUTE_V2: {cfg.get('ai_content_route_v2_enabled')}, PROVIDER: {cfg.get('ai_provider_id')}", flush=True)
        print(f"ALLOWED_ROUTES: {cfg.get('ai_content_allowed_routes')}", flush=True)
        print(f"ATTESTATION_IDS: {cfg.get('ai_content_attestation_ids')}", flush=True)

        if gid:
            grp = session.get(TgGroup, int(gid))
            if grp:
                print(f"GROUP_INFO: id={grp.id}, title={grp.title}, peer_id={grp.tg_peer_id}, auth={grp.auth_status}, can_send={grp.can_send}, members={grp.member_count}", flush=True)
                total_acc = session.scalar(select(text("count(*)")).select_from(TgGroupAccount).where(TgGroupAccount.group_id == int(gid)))
                can_send_acc = session.scalar(select(text("count(*)")).select_from(TgGroupAccount).where(TgGroupAccount.group_id == int(gid), TgGroupAccount.can_send.is_(True)))
                print(f"GROUP_ACCOUNTS: total={total_acc}, can_send={can_send_acc}", flush=True)
            else:
                print(f"GROUP_INFO: NOT FOUND for id={gid}", flush=True)

        actions = list(session.scalars(
            select(Action).where(Action.task_id == t.id).order_by(Action.created_at.desc()).limit(3)
        ).all())
        print(f"LATEST_ACTIONS_COUNT: {len(actions)}", flush=True)
        for a in actions:
            print(f"ACTION: id={a.id}, type={a.action_type}, status={a.status}, gen_status={getattr(a, 'generation_status', None)}, err={a.result.get('error_code') if a.result else None}, time={a.created_at}", flush=True)

    finally:
        session.close()
    print("=== END DIAGNOSE ZHENGZHOU DAXUE ===", flush=True)

if __name__ == "__main__":
    main()

import sys
sys.exit(0)
