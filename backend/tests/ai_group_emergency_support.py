"""Original quantity/coverage owners for emergency publication regressions."""
from datetime import timedelta, timezone
from types import SimpleNamespace

from app.models import GenerationJob, OperationTarget, Task, TaskDayLedger, TaskGroupDailyMessageSlot
from app.services._common import _now
from app.timezone import as_beijing
from tests.ai_generation_phase_test_support import seed_reserved_normal_batch


def seed_emergency_batch(session):
    now = _now()
    actions, coverages = seed_reserved_normal_batch(session, now)
    task = session.get(Task, actions[0].task_id)
    task.type_config = {**dict(task.type_config or {}), "engagement_contract_version": "unified_engagement_v1",
                        "ai_content_route_v2_enabled": True, "ai_model": "explicit-model"}
    target = OperationTarget(id=71, tenant_id=1, target_type="group", tg_peer_id="-1007", title="target")
    ledger = TaskDayLedger(id="emergency-ledger", tenant_id=1, task_id=task.id,
        timezone_snapshot="Asia/Shanghai", timezone_revision=1, obligation_local_date=as_beijing(now).date(),
        period_start_at=as_beijing(now).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc),
        deadline_at=(as_beijing(now) + timedelta(hours=1)).astimezone(timezone.utc),
        day_phase="active", planning_anchor_at=now)
    session.add_all([target, ledger])
    session.flush()
    for ordinal, action in enumerate(actions, 1):
        slot = TaskGroupDailyMessageSlot(id=f"emergency-slot-{ordinal}", tenant_id=1, task_id=task.id,
            task_day_ledger_id=ledger.id, target_operation_target_id=target.id,
            task_account_daily_coverage_id=coverages[ordinal - 1].id, slot_kind="coverage", slot_ordinal=ordinal)
        session.add(slot)
        session.flush()
        action.primary_quantity_slot_id = slot.id
        action.obligation_type, action.obligation_id = "group_quantity_slot", slot.id
        job = GenerationJob(id=f"emergency-job-{ordinal}", tenant_id=1, task_id=task.id,
            task_lifecycle_epoch=1, obligation_type=action.obligation_type, obligation_id=slot.id,
            generation_sequence=1, context_snapshot_version=1, state="generating", generation_owner_id="worker-a")
        session.add(job)
        action.payload = {**action.payload, "primary_quantity_slot_id": slot.id,
            "generation_job_id": job.id, "ai_generation_attempt_id": "emergency-attempt",
            "chat_mode": "bootstrap", "relation_kind": "direct"}
    session.commit()
    request = SimpleNamespace(batch_ids=[row.id for row in actions], tenant_id=1, task_id=task.id,
        claim_owner="worker-a", claim_token="claim-normal", attempt_id="emergency-attempt", group_id=7)
    return task, actions, coverages, request
