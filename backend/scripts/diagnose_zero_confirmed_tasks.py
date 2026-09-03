"""Diagnose zero confirmed tasks: Chengdu, Zhengzhou University, Tianjin Music."""

from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import text
from app.database import SessionLocal
from app.models import Task, TgGroup

ZERO_TASK_IDS = [
    ("a52e84f2-8663-4b00-bbbe-196fb626b28d", "郑州大学"),
    ("b6f0ebd6-880a-4d6e-9279-04709383486e", "成都怡红院"),
    ("6407d98f-e6af-4df8-a10b-806135bf24ff", "郑州楼凤"),
    ("cb862a03-0dd1-432e-8854-7f89946bcf06", "西安天上人间"),
    ("e8152470-c696-4ff0-82d5-650ae38c5bc7", "三亚"),
    ("f77ebe14-9f9d-451a-9ca9-5c6292598bd7", "天津一品楼"),
    ("f283***60-c7ef-4ec2-8055-8ff5262b4734", "郑州学生会"),
    ("0361a7ac-ae51-48ae-aea4-ccea43df5f17", "郑州师范"),
]

def main():
    session = SessionLocal()
    try:
        for tid, name in ZERO_TASK_IDS:
            print(f"==================================================")
            print(f"DIAGNOSING: [{name}] (ID: {tid})")
            print(f"==================================================")
            t = session.get(Task, tid)
            if not t:
                print("Task NOT FOUND!")
                continue
            cfg = dict(t.type_config or {})
            gid = cfg.get("target_group_id")
            print(f"Target Group ID: {gid}")
            print(f"Task Status: {t.status}, Epoch: {t.task_lifecycle_epoch}, Rev: {t.config_revision}")
            print(f"Task Last Error: {t.last_error}")
            
            if gid:
                grp = session.get(TgGroup, gid)
                if grp:
                    print(f"Group Info: title={grp.title}, auth={grp.auth_status}, can_send={grp.can_send}, member_count={grp.member_count}, active_window={grp.active_window}")
                else:
                    print("Group NOT FOUND in tg_groups!")

            # Check actions summary
            sql_actions = f"""
            SELECT status, action_type, count(*) as cnt,
                   max(created_at) as latest_created_at,
                   max(executed_at) as latest_executed_at
            FROM actions
            WHERE task_id = '{tid}'
            GROUP BY status, action_type
            ORDER BY count(*) DESC;
            """
            rows = [dict(r) for r in session.execute(text(sql_actions)).mappings()]
            print("\nActions Breakdown:")
            for r in rows:
                print(f"  - status: {r.get('status')}, type: {r.get('action_type')}, cnt: {r.get('cnt')}, latest_created: {r.get('latest_created_at')}, latest_exec: {r.get('latest_executed_at')}")

            # Check latest 3 failed actions
            sql_failed_actions = f"""
            SELECT id, action_type, status, payload ->> 'ai_generation_status' as gen_status,
                   payload ->> 'group_id' as group_id,
                   result, created_at, executed_at
            FROM actions
            WHERE task_id = '{tid}' AND status = 'failed'
            ORDER BY created_at DESC
            LIMIT 3;
            """
            failed_rows = [dict(r) for r in session.execute(text(sql_failed_actions)).mappings()]
            print("\nRecent Failed Actions:")
            for r in failed_rows:
                print(f"  Action ID: {r.get('id')}, Type: {r.get('action_type')}, GenStatus: {r.get('gen_status')}")
                print(f"    Result: {r.get('result')}")
                print(f"    Created: {r.get('created_at')}, Exec: {r.get('executed_at')}")

            # Check latest 3 pending actions
            sql_pending_actions = f"""
            SELECT id, action_type, status, scheduled_at, lease_owner, lease_expires_at, created_at
            FROM actions
            WHERE task_id = '{tid}' AND status = 'pending'
            ORDER BY scheduled_at ASC
            LIMIT 3;
            """
            pending_rows = [dict(r) for r in session.execute(text(sql_pending_actions)).mappings()]
            print("\nEarliest Pending Actions:")
            for r in pending_rows:
                print(f"  Action ID: {r.get('id')}, Type: {r.get('action_type')}, Scheduled: {r.get('scheduled_at')}, Lease: {r.get('lease_owner')}")

            # Check generation jobs
            sql_jobs = f"""
            SELECT state, count(*), max(created_at) as latest_created
            FROM generation_jobs
            WHERE task_id = '{tid}'
            GROUP BY state;
            """
            job_rows = [dict(r) for r in session.execute(text(sql_jobs)).mappings()]
            print("\nGeneration Jobs Breakdown:")
            for r in job_rows:
                print(f"  - state: {r.get('state')}, count: {r.get('count')}, latest: {r.get('latest_created')}")

            # Check pending actions scheduled time distribution
            sql_sched_dist = f"""
            SELECT count(*) as total_pending,
                   min(scheduled_at) as min_sched,
                   max(scheduled_at) as max_sched,
                   count(CASE WHEN scheduled_at <= NOW() THEN 1 END) as due_now,
                   count(CASE WHEN scheduled_at <= NOW() + interval '30 minutes' THEN 1 END) as due_in_30m
            FROM actions
            WHERE task_id = '{tid}' AND status = 'pending';
            """
            s_dist = [dict(r) for r in session.execute(text(sql_sched_dist)).mappings()]
            print(f"Pending Sched Distribution: {s_dist}")

        # Global diagnostics
        print("\n==================================================")
        print("GLOBAL SYSTEM DIAGNOSTICS")
        print("==================================================")
        print("Current DB NOW():", session.execute(text("SELECT NOW();")).scalar())
        
        # Worker heartbeats
        hb_rows = [dict(r) for r in session.execute(text("SELECT worker_role, worker_id, hostname, last_heartbeat_at, is_alive FROM worker_heartbeats ORDER BY last_heartbeat_at DESC;")).mappings()]
        print("\nWorker Heartbeats:")
        for hb in hb_rows:
            print(f"  {hb}")

        # AI Provider state
        try:
            prov_rows = [dict(r) for r in session.execute(text("SELECT * FROM ai_provider_admissions;")).mappings()]
            print(f"\nAI Provider Admissions: {prov_rows}")
        except Exception as e:
            print(f"\nAI Provider Admissions query: {e}")

    finally:
        session.close()

if __name__ == "__main__":
    main()
