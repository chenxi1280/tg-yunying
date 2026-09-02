"""Diagnose zero confirmed tasks: Chengdu, Zhengzhou University, Tianjin Music."""

from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import text
from app.database import SessionLocal
from app.models import Task, TgGroup

ZERO_TASK_IDS = [
    ("b6f0ebd6-880a-4d6e-9279-04709383486e", "成都怡红院"),
    ("a52e84f2-8663-4b00-bbbe-196fb626b28d", "郑州大学"),
    ("7fd0bbb7-53dd-45ae-a7af-0c37bcc380d1", "天津音乐"),
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

            # Check group membership actions
            sql_membership = f"""
            SELECT status, count(*), max(created_at) as latest_created, max(executed_at) as latest_exec
            FROM actions
            WHERE task_id = '{tid}' AND action_type IN ('ensure_target_membership', 'join_group')
            GROUP BY status;
            """
            m_rows = [dict(r) for r in session.execute(text(sql_membership)).mappings()]
            print("\nMembership Actions Breakdown:")
            for r in m_rows:
                print(f"  - status: {r.get('status')}, count: {r.get('count')}, latest_exec: {r.get('latest_exec')}")

    finally:
        session.close()

if __name__ == "__main__":
    main()
