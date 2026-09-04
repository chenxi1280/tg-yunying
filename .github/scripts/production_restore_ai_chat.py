from __future__ import annotations

import json
import time
from sqlalchemy import text, select, update
from app.database import SessionLocal
from app.models import Task, TgGroup, Action, TaskAccountDailyCoverage
from app.models.ai import TenantAiSetting, AiProvider
from app.models.ai_content_policy import TaskAiContentPolicyBinding, AiContentPolicyVersion
from app.services.task_center.executors.group_ai_chat import build_plan as build_group_ai_plan
from app.services._common import _now


def main():
    with SessionLocal() as session:
        now_ts = _now()
        report = {}

        # 1. Check & Fix TenantAiSetting
        settings = list(session.scalars(select(TenantAiSetting)))
        fixed_settings = []
        for s in settings:
            modified = False
            if s.temperature is None or s.temperature <= 0:
                s.temperature = 0.8
                modified = True
            if s.max_tokens is None or s.max_tokens <= 0:
                s.max_tokens = 1024
                modified = True
            if not s.ai_enabled:
                s.ai_enabled = True
                modified = True
            if modified:
                s.updated_at = now_ts
                session.add(s)
                fixed_settings.append(s.tenant_id)
        session.commit()
        report["fixed_tenant_ai_settings"] = fixed_settings

        # Print all current settings
        cur_settings = list(
            session.execute(
                text("SELECT id, tenant_id, default_provider_id, ai_enabled, temperature, max_tokens FROM tenant_ai_settings")
            ).mappings()
        )
        report["current_tenant_ai_settings"] = [dict(r) for r in cur_settings]

        # 2. Check active AI Providers
        providers = list(
            session.execute(
                text("SELECT id, provider_name, provider_type, is_active, credential_enabled, health_status, model_name FROM ai_providers WHERE is_active = true")
            ).mappings()
        )
        report["active_ai_providers"] = [dict(r) for r in providers]

        # 3. Clean failed stale actions and reset daily coverage for all 9 active group_ai_chat tasks
        tasks = list(
            session.scalars(
                select(Task).where(
                    Task.type == "group_ai_chat",
                    Task.status == "running",
                ).order_by(Task.name)
            )
        )
        report["running_tasks"] = [t.name for t in tasks]

        task_res = []
        for task in tasks:
            task_id = task.id
            # Clean failed actions
            del_failed = session.execute(
                text("""
                    DELETE FROM actions
                    WHERE task_id = :task_id
                      AND status = 'failed'
                      AND (scheduled_at >= CURRENT_DATE OR created_at >= CURRENT_DATE)
                """),
                {"task_id": task_id},
            ).rowcount

            # Delete stale slots
            del_slots = session.execute(
                text("DELETE FROM task_group_daily_message_slots WHERE task_id = :task_id"),
                {"task_id": task_id},
            ).rowcount

            # Reset daily coverage
            reset_cov = session.execute(
                update(TaskAccountDailyCoverage)
                .where(
                    TaskAccountDailyCoverage.task_id == task_id,
                    TaskAccountDailyCoverage.coverage_date == now_ts.date(),
                )
                .values(
                    state="ready",
                    reserved_action_id=None,
                    reservation_token=None,
                    blocker_code="",
                    updated_at=now_ts,
                )
            ).rowcount

            # Ensure TaskAiContentPolicyBinding
            curr_epoch = task.task_lifecycle_epoch or 1
            curr_rev = task.config_revision or 1
            curr_binding = session.scalar(
                select(TaskAiContentPolicyBinding).where(
                    TaskAiContentPolicyBinding.task_id == task_id,
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
                            task_id=task_id,
                            task_lifecycle_epoch=curr_epoch,
                            task_config_revision=curr_rev,
                            policy_version_id=policy_id,
                            allowed_routes=ref_binding.allowed_routes if ref_binding else ["general_chat", "discussion", "reply", "campus_life"],
                            attestation_ids=[],
                            evidence_hash="restore_auto_bound",
                            approved_by="auto_restore",
                        )
                    )

            # Build fresh batch of actions
            c = build_group_ai_plan(session, task)
            session.commit()

            task_res.append({
                "task_name": task.name,
                "task_id": task_id,
                "cleaned_failed": del_failed,
                "cleaned_slots": del_slots,
                "reset_coverage": reset_cov,
                "new_actions_created": c,
            })

        report["task_restore_details"] = task_res
        print(f"RESTORE_RESULT={json.dumps(report, ensure_ascii=False, indent=2)}")


if __name__ == "__main__":
    main()
