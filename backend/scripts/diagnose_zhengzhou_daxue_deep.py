"""Deep diagnosis for Zhengzhou Daxue task."""

from __future__ import annotations
import json
from sqlalchemy import text, select
from app.database import SessionLocal
from app.models import Task, TgGroup, TgGroupAccount, Action, TgAccount, TaskAiContentPolicyBinding, GenerationJob

def main():
    session = SessionLocal()
    try:
        t_id = "a52e84f2-8663-4b00-bbbe-196fb626b28d"
        t = session.get(Task, t_id)
        if not t:
            print("Task NOT FOUND!")
            return

        print("=== TASK INFO ===")
        print(f"ID: {t.id}, Name: {t.name}, Status: {t.status}, NextRunAt: {t.next_run_at}, LastError: {t.last_error}")
        print(f"Lifecycle Epoch: {t.task_lifecycle_epoch}, Config Revision: {t.config_revision}")
        cfg = t.type_config or {}
        gid = cfg.get("target_group_id")
        print(f"Target Group ID: {gid}")
        print(f"AI Content Route V2 Enabled: {cfg.get('ai_content_route_v2_enabled')}")
        print(f"AI Provider ID: {cfg.get('ai_provider_id')}")
        print(f"Policy Version ID: {cfg.get('ai_content_policy_version_id')}")
        print(f"Allowed Routes: {cfg.get('ai_content_allowed_routes')}")
        print(f"Attestation IDs: {cfg.get('ai_content_attestation_ids')}")

        # Check Policy Binding
        binding = session.scalar(
            select(TaskAiContentPolicyBinding).where(
                TaskAiContentPolicyBinding.task_id == t.id,
                TaskAiContentPolicyBinding.task_lifecycle_epoch == t.task_lifecycle_epoch,
                TaskAiContentPolicyBinding.task_config_revision == t.config_revision,
            )
        )
        if binding:
            print(f"Policy Binding: FOUND (status={binding.status}, routes={binding.allowed_routes}, attestations={binding.attestation_ids})")
        else:
            print("Policy Binding: NOT FOUND in database!")

        # Check Target Group
        if gid:
            grp = session.get(TgGroup, int(gid))
            if grp:
                print(f"
=== GROUP INFO (ID: {grp.id}) ===")
                print(f"Title: {grp.title}, PeerID: {grp.tg_peer_id}, Auth: {grp.auth_status}, MemberCount: {grp.member_count}, CanSend: {grp.can_send}")
                
                # Check accounts in group
                acc_rows = list(session.execute(
                    select(TgGroupAccount, TgAccount)
                    .join(TgAccount, TgAccount.id == TgGroupAccount.account_id)
                    .where(TgGroupAccount.group_id == int(gid))
                ).all())
                print(f"Accounts in group: {len(acc_rows)}")
                can_send_accs = [acc for ga, acc in acc_rows if ga.can_send and acc.status == "在线" and not acc.deleted_at]
                print(f"Active & CanSend accounts in group: {len(can_send_accs)}")
                if len(can_send_accs) > 0:
                    print(f"Sample accounts: {[a.id for a in can_send_accs[:5]]}")
            else:
                print(f"
Group {gid} NOT FOUND in database!")

        # Check Recent Actions
        actions = list(session.scalars(
            select(Action).where(Action.task_id == t.id).order_by(Action.created_at.desc()).limit(10)
        ).all())
        print(f"
=== RECENT ACTIONS ({len(actions)}) ===")
        for a in actions:
            print(f"- Action {a.id} ({a.action_type}): status={a.status}, gen_status={getattr(a, 'generation_status', None)}, err={a.result.get('error_code') if a.result else None}, created_at={a.created_at}")

        # Check Recent Generation Jobs
        jobs = list(session.scalars(
            select(GenerationJob).where(GenerationJob.task_id == t.id).order_by(GenerationJob.created_at.desc()).limit(10)
        ).all())
        print(f"
=== RECENT GENERATION JOBS ({len(jobs)}) ===")
        for j in jobs:
            print(f"- Job {j.id}: state={j.state}, error={j.error_message}, created_at={j.created_at}, finished_at={j.finished_at}")

    finally:
        session.close()

if __name__ == "__main__":
    main()
