"""Rescue and Wake up Zhengzhou Daxue task immediately."""

from __future__ import annotations
import argparse
from datetime import datetime, timezone
from sqlalchemy import text, select, update
from app.database import SessionLocal
from app.models import Task, TgGroup, TgGroupAccount, Action, GenerationJob
from app.services.task_center.daily_group_target import ensure_task_group_daily_target

TASK_ID = "a52e84f2-8663-4b00-bbbe-196fb626b28d"

def main():
    parser = argparse.ArgumentParser(description="Wake up Zhengzhou Daxue task")
    parser.add_argument("--apply", action="store_true", help="Apply changes to DB")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        t = session.get(Task, TASK_ID)
        if not t:
            print("Task NOT FOUND!")
            return

        now = datetime.now(timezone.utc)
        today = now.date()

        print(f"=== ZHENGZHOU DAXUE RESCUE (Mode: {'APPLY' if args.apply else 'PREVIEW'}) ===")
        print(f"Task: {t.name} ({t.id})")
        print(f"Current Status: {t.status}, NextRunAt: {t.next_run_at}, LastError: {t.last_error}")

        cfg = dict(t.type_config or {})
        gid = cfg.get("target_group_id")
        
        # 1. Reset pending actions scheduled in the future
        pending_future_actions = list(session.scalars(
            select(Action).where(
                Action.task_id == t.id,
                Action.status == "pending",
                Action.scheduled_at > now,
            )
        ).all())
        print(f"\nFound {len(pending_future_actions)} pending actions delayed into future:")
        for a in pending_future_actions:
            print(f"  - Action {a.id} ({a.action_type}): old_scheduled_at={a.scheduled_at}")

        # 2. Reset failed stale actions from today morning to allow fresh generation
        stale_failed_cnt = session.scalar(
            select(text("count(*)")).select_from(Action).where(
                Action.task_id == t.id,
                Action.status == "failed",
                Action.created_at >= datetime(today.year, today.month, today.day, 0, 0, 0, tzinfo=timezone.utc),
            )
        )
        print(f"Today Failed Actions Count: {stale_failed_cnt}")

        if args.apply:
            # 1. Wake up task
            t.status = "running"
            t.last_error = ""
            t.next_run_at = now
            t.updated_at = now

            # 2. Pull all delayed pending actions to NOW
            for a in pending_future_actions:
                a.scheduled_at = now
                a.lease_owner = None
                a.lease_expires_at = None
                a.updated_at = now

            # 3. Refresh Group & GroupAccount sendability
            if gid:
                grp = session.get(TgGroup, int(gid))
                if grp:
                    grp.can_send = True
                    grp.auth_status = "已授权运营"
                    refreshed = ensure_task_group_daily_target(session, t, grp, today)
                    print(f"Refreshed daily ledger target: {refreshed.effective_message_target}")

                session.execute(
                    update(TgGroupAccount)
                    .where(TgGroupAccount.group_id == int(gid))
                    .values(can_send=True, send_cooldown_expires_at=None)
                )

            session.commit()
            print("\n>>> Task successfully woken up and scheduled actions pulled forward!")
        else:
            print("\n>>> Preview finished. Pass --apply to execute.")

    finally:
        session.close()

if __name__ == "__main__":
    main()
