"""Sync AI content policy binding from Loufeng to all running AI group tasks."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from sqlalchemy import select
from app.database import SessionLocal
from app.models import Task, TgGroup, TaskAiContentPolicyBinding, AiContentPolicyVersion
from app.services.task_center.daily_group_target import ensure_task_group_daily_target

LOUFENG_TASK_ID = "6407d98f-e6af-4df8-a10b-806135bf24ff"

def main():
    parser = argparse.ArgumentParser(description="Sync AI Policy Binding from Loufeng")
    parser.add_argument("--apply", action="store_true", help="Apply changes to production database")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        loufeng = session.get(Task, LOUFENG_TASK_ID)
        if not loufeng:
            print("ERROR: Loufeng task not found!")
            return

        lf_binding = session.scalar(
            select(TaskAiContentPolicyBinding).where(
                TaskAiContentPolicyBinding.task_id == loufeng.id,
                TaskAiContentPolicyBinding.task_lifecycle_epoch == loufeng.task_lifecycle_epoch,
                TaskAiContentPolicyBinding.task_config_revision == loufeng.config_revision,
            )
        )
        if not lf_binding:
            # fallback to latest binding
            lf_binding = session.scalar(
                select(TaskAiContentPolicyBinding).where(
                    TaskAiContentPolicyBinding.task_id == loufeng.id
                ).order_by(TaskAiContentPolicyBinding.id.desc())
            )
        
        print(f"=== LOUFENG TEMPLATE BINDING FOUND ===")
        print(f"Policy Version ID: {lf_binding.policy_version_id}")
        print(f"Allowed Routes: {lf_binding.allowed_routes}")
        print(f"Attestations: {lf_binding.attestation_ids}")

        tasks = list(session.scalars(
            select(Task).where(
                Task.type == "group_ai_chat",
                Task.status == "running",
                Task.deleted_at.is_(None),
                Task.id != LOUFENG_TASK_ID,
            )
        ).all())

        today = datetime.now(timezone.utc).date()
        print(f"
Found {len(tasks)} other tasks to sync policy binding.")

        for t in tasks:
            cfg = dict(t.type_config or {})
            cfg["ai_content_route_v2_enabled"] = "true"
            cfg["ai_provider_id"] = "6"
            cfg["ai_content_policy_version_id"] = lf_binding.policy_version_id
            cfg["ai_content_allowed_routes"] = lf_binding.allowed_routes
            cfg["ai_content_attestation_ids"] = lf_binding.attestation_ids
            
            t.type_config = cfg
            t.updated_at = datetime.now(timezone.utc)
            
            print(f"- [{t.name}] (ID: {t.id})")
            
            if args.apply:
                # 检查或创建 TaskAiContentPolicyBinding
                existing = session.scalar(
                    select(TaskAiContentPolicyBinding).where(
                        TaskAiContentPolicyBinding.task_id == t.id,
                        TaskAiContentPolicyBinding.task_lifecycle_epoch == t.task_lifecycle_epoch,
                        TaskAiContentPolicyBinding.task_config_revision == t.config_revision,
                    )
                )
                if not existing:
                    new_binding = TaskAiContentPolicyBinding(
                        tenant_id=t.tenant_id,
                        task_id=t.id,
                        task_lifecycle_epoch=t.task_lifecycle_epoch,
                        task_config_revision=t.config_revision,
                        policy_version_id=lf_binding.policy_version_id,
                        allowed_routes=lf_binding.allowed_routes,
                        attestation_ids=lf_binding.attestation_ids,
                        scope_refs=lf_binding.scope_refs,
                        approved_by=lf_binding.approved_by,
                        evidence_hash=lf_binding.evidence_hash,
                    )
                    session.add(new_binding)
                    print(f"  [APPLIED] Created TaskAiContentPolicyBinding for epoch={t.task_lifecycle_epoch}, rev={t.config_revision}")
                else:
                    print(f"  [EXISTS] TaskAiContentPolicyBinding already exists")

                gid = cfg.get("target_group_id")
                group = session.get(TgGroup, gid) if gid else None
                if group:
                    refreshed = ensure_task_group_daily_target(session, t, group, today)
                    print(f"  [APPLIED] Refreshed Ledger: effective={refreshed.effective_message_target}")
                session.flush()

        if args.apply:
            session.commit()
            print("
>>> All tasks successfully bound with AI Policy and synced with Loufeng template!")
        else:
            print("
>>> Preview completed. Run with --apply to commit.")
    finally:
        session.close()

if __name__ == "__main__":
    main()
