from __future__ import annotations

import json
from datetime import datetime
from sqlalchemy import select, update, text
from app.database import SessionLocal
from app.models import Task, TgGroup
from app.models.task_group_daily_target import TaskGroupDailyTarget
from app.models.ai_content_policy import TaskAiContentPolicyBinding, AiContentPolicyVersion
from app.services._common import _now


def main():
    target_value = 2000
    now_ts = _now()
    today = now_ts.date()
    results = []

    with SessionLocal() as session:
        tasks = list(
            session.scalars(
                select(Task).where(
                    Task.type == "group_ai_chat",
                    Task.status == "running",
                ).order_by(Task.name)
            )
        )

        for task in tasks:
            tc = dict(task.type_config or {})
            pc = dict(task.pacing_config or {})

            old_tc_target = tc.get("daily_message_target")
            old_pc_target = pc.get("daily_message_target")

            tc["daily_message_target"] = target_value
            pc["daily_message_target"] = target_value
            if "target_daily_messages" in pc:
                pc["target_daily_messages"] = target_value

            task.type_config = tc
            task.pacing_config = pc
            session.add(task)

            # Update TaskGroupDailyTarget records for today
            daily_targets = list(
                session.scalars(
                    select(TaskGroupDailyTarget).where(
                        TaskGroupDailyTarget.tenant_id == task.tenant_id,
                        TaskGroupDailyTarget.task_id == task.id,
                        TaskGroupDailyTarget.target_date == today,
                    )
                )
            )

            group_updates = []
            if not daily_targets:
                # If no record for today, search recent records
                daily_targets = list(
                    session.scalars(
                        select(TaskGroupDailyTarget).where(
                            TaskGroupDailyTarget.tenant_id == task.tenant_id,
                            TaskGroupDailyTarget.task_id == task.id,
                        ).order_by(TaskGroupDailyTarget.target_date.desc()).limit(1)
                    )
                )

            for dt in daily_targets:
                old_configured = dt.configured_message_target
                old_effective = dt.effective_message_target
                dt.configured_message_target = target_value
                dt.effective_message_target = max(target_value, dt.frozen_account_count or 0)
                dt.planned_daily_target = dt.effective_message_target
                dt.target_change_reason = "user_updated_per_group_daily_target_2000"
                dt.target_changed_at = now_ts
                session.add(dt)
                group_updates.append({
                    "target_date": str(dt.target_date),
                    "old_configured": old_configured,
                    "old_effective": old_effective,
                    "new_configured": dt.configured_message_target,
                    "new_effective": dt.effective_message_target,
                })

            # Ensure TaskAiContentPolicyBinding exists for current revision
            curr_epoch = task.task_lifecycle_epoch or 1
            curr_rev = task.config_revision or 1
            curr_binding = session.scalar(
                select(TaskAiContentPolicyBinding).where(
                    TaskAiContentPolicyBinding.task_id == task.id,
                    TaskAiContentPolicyBinding.task_lifecycle_epoch == curr_epoch,
                    TaskAiContentPolicyBinding.task_config_revision == curr_rev,
                )
            )
            if not curr_binding:
                ref_binding = session.scalar(
                    select(TaskAiContentPolicyBinding).order_by(TaskAiContentPolicyBinding.created_at.desc())
                )
                default_policy = session.scalar(
                    select(AiContentPolicyVersion).where(AiContentPolicyVersion.status == "active").order_by(AiContentPolicyVersion.created_at.desc())
                )
                policy_id = ref_binding.policy_version_id if ref_binding else (default_policy.id if default_policy else None)
                if policy_id:
                    session.add(
                        TaskAiContentPolicyBinding(
                            tenant_id=task.tenant_id,
                            task_id=task.id,
                            task_lifecycle_epoch=curr_epoch,
                            task_config_revision=curr_rev,
                            policy_version_id=policy_id,
                            allowed_routes=ref_binding.allowed_routes if ref_binding else ["general_chat", "discussion", "reply", "campus_life"],
                            attestation_ids=[],
                            evidence_hash="target_update_bound",
                            approved_by="target_update_auto",
                        )
                    )

            results.append({
                "task_name": task.name,
                "task_id": task.id,
                "old_type_config_target": old_tc_target,
                "old_pacing_config_target": old_pc_target,
                "new_target": target_value,
                "daily_targets_updated": group_updates,
            })

        session.commit()

    print(f"ALL_GROUP_TARGETS_UPDATED_TO_2000={json.dumps(results, ensure_ascii=False, indent=2)}")


if __name__ == "__main__":
    main()
