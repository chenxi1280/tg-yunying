"""Preview and safely apply updated daily message target for all running AI group tasks."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone

from sqlalchemy import select
from app.database import SessionLocal
from app.models.task_center import Task, TgGroup
from app.models.task_group_daily_target import TaskGroupDailyTarget
from app.services.task_center.daily_group_target import (
    ensure_task_group_daily_target,
    refresh_task_group_daily_target,
)


def main():
    parser = argparse.ArgumentParser(description="Update daily message target for AI group tasks")
    parser.add_argument("--target", type=int, default=4200, help="New daily message target")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default is preview only)")
    parser.add_argument("--task-id", type=str, default=None, help="Specific task ID to update")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        query = select(Task).where(
            Task.type == "group_ai_chat",
            Task.status == "running",
            Task.deleted_at.is_(None),
        )
        if args.task_id:
            query = query.where(Task.id == args.task_id)

        tasks = list(session.scalars(query).all())
        today = datetime.now(timezone.utc).date()
        print(f"=== AI GROUP DAILY TARGET TOOL (Mode: {'APPLY' if args.apply else 'PREVIEW'}) ===")
        print(f"Found {len(tasks)} running AI group tasks. Target: {args.target}")

        for t in tasks:
            cfg = dict(t.type_config or {})
            old_target = cfg.get("daily_message_target")
            gid = cfg.get("target_group_id")
            group = session.get(TgGroup, gid) if gid else None
            
            # 当前日账本
            dg_target = session.scalar(
                select(TaskGroupDailyTarget).where(
                    TaskGroupDailyTarget.tenant_id == t.tenant_id,
                    TaskGroupDailyTarget.task_id == t.id,
                    TaskGroupDailyTarget.target_date == today,
                )
            )
            old_effective = dg_target.effective_message_target if dg_target else None

            print(f"\n- [{t.name}] (ID: {t.id})")
            print(f"  Configured Target: {old_target} -> {args.target}")
            print(f"  Today Ledger Target: {old_effective} (Date: {today})")

            if args.apply:
                cfg["daily_message_target"] = args.target
                t.type_config = cfg
                t.updated_at = datetime.now(timezone.utc)
                if group:
                    refreshed = ensure_task_group_daily_target(session, t, group, today)
                    print(f"  [APPLIED] Refreshed Ledger: effective={refreshed.effective_message_target}, planned={refreshed.planned_daily_target}")
                session.flush()

        if args.apply:
            session.commit()
            print("\n>>> All task targets successfully committed to production database!")
        else:
            print("\n>>> Preview completed. Run with --apply to commit changes.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
