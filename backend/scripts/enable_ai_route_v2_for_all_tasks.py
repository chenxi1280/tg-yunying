"""Enable AI Content Route V2 for all running AI group tasks."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from sqlalchemy import select
from app.database import SessionLocal
from app.models import Task, TgGroup
from app.services.task_center.daily_group_target import ensure_task_group_daily_target

def main():
    parser = argparse.ArgumentParser(description="Enable AI Content Route V2")
    parser.add_argument("--apply", action="store_true", help="Apply changes to production database")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        query = select(Task).where(
            Task.type == "group_ai_chat",
            Task.status == "running",
            Task.deleted_at.is_(None),
        )
        tasks = list(session.scalars(query).all())
        today = datetime.now(timezone.utc).date()
        
        print(f"=== ENABLE AI ROUTE V2 TOOL (Mode: {'APPLY' if args.apply else 'PREVIEW'}) ===")
        print(f"Found {len(tasks)} running AI group tasks.")

        for t in tasks:
            cfg = dict(t.type_config or {})
            old_v2 = cfg.get("ai_content_route_v2_enabled")
            old_provider = cfg.get("ai_provider_id")
            
            # 统一设为 true 和 provider 6
            new_v2 = "true"
            new_provider = old_provider if (old_provider and str(old_provider).strip()) else "6"
            
            print(f"\n- [{t.name}] (ID: {t.id})")
            print(f"  Route V2: {old_v2} -> {new_v2}")
            print(f"  Provider ID: {old_provider} -> {new_provider}")

            if args.apply:
                cfg["ai_content_route_v2_enabled"] = new_v2
                cfg["ai_provider_id"] = new_provider
                t.type_config = cfg
                t.updated_at = datetime.now(timezone.utc)
                
                gid = cfg.get("target_group_id")
                group = session.get(TgGroup, gid) if gid else None
                if group:
                    refreshed = ensure_task_group_daily_target(session, t, group, today)
                    print(f"  [APPLIED] Refreshed Ledger: effective={refreshed.effective_message_target}")
                session.flush()

        if args.apply:
            session.commit()
            print("\n>>> All tasks successfully switched to AI Route V2 in production database!")
        else:
            print("\n>>> Preview completed. Run with --apply to commit changes.")
    finally:
        session.close()

if __name__ == "__main__":
    main()
