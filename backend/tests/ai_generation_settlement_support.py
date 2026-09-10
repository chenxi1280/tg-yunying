"""Neutral generation claims shared by SQLite and real PostgreSQL regressions."""
import hashlib
from datetime import timedelta

from app.models import Action, AiContentWindowPlan, AiContentWindowPlanSlot, GenerationJob, Task, Tenant, TgAccount
from app.services._common import _now
from app.services.task_center.ai_generation_parallel import ParallelGenerationClaim


TASK_ID = "settlement-task"
ACTION_ID = "settlement-action"
JOB_ID = "settlement-job"
CONTENT = "周末大家更喜欢跑步还是骑车？"
OWNER = "generation-worker"
TOKEN = "generation-token"


def seed_ready_claim(factory, *, action_status="executing"):
    with factory() as session:
        session.add(Tenant(id=1, name="settlement QA"))
        session.flush()
        session.add_all([
            Task(id=TASK_ID, tenant_id=1, name="Weekend sports", type="group_ai_chat",
                 status="running", task_lifecycle_epoch=1),
            TgAccount(id=11, tenant_id=1, display_name="QA account", phone_masked="test"),
        ])
        session.flush()
        session.add(GenerationJob(
            id=JOB_ID, tenant_id=1, task_id=TASK_ID, task_lifecycle_epoch=1,
            obligation_type="quantity_slot", obligation_id="settlement-obligation",
            generation_sequence=1, context_snapshot_version=1, state="generating",
            generation_owner_id=OWNER, generation_lease_epoch=1, job_version=2,
            lease_expires_at=_now() - timedelta(minutes=1),
        ))
        session.flush()
        owner = OWNER if action_status == "executing" else ""
        token = TOKEN if owner else ""
        session.add(Action(
            id=ACTION_ID, tenant_id=1, task_id=TASK_ID, task_type="group_ai_chat",
            action_type="send_message", account_id=11, task_lifecycle_epoch=1,
            obligation_type="quantity_slot", obligation_id="settlement-obligation",
            status=action_status, claim_owner=owner, claim_token=token, lease_owner=owner,
            lease_expires_at=_now() - timedelta(minutes=1) if owner else None,
            candidate_hash=hashlib.sha256(CONTENT.encode()).hexdigest(),
            payload={"generation_job_id": JOB_ID, "message_text": CONTENT,
                     "ai_generation_status": "ready", "ai_generation_claim_owner": owner,
                     "ai_generation_claim_token": token},
            result={"generation_stage": "generation_ready", "generation_outcome": "ready"},
        ))
        session.commit()
    return ParallelGenerationClaim(ACTION_ID, JOB_ID, OWNER, TOKEN, 2, 1)


def seed_prepared_window(factory, *, state="candidate_ready"):
    with factory() as session:
        now = _now()
        job, action = session.get(GenerationJob, JOB_ID), session.get(Action, ACTION_ID)
        plan = AiContentWindowPlan(tenant_id=1, task_id=TASK_ID, task_lifecycle_epoch=1,
            scope_type="group", scope_id="7", pacing_plan_hash="p" * 64,
            period_key="settlement-period", window_start_at=now,
            window_end_at=now + timedelta(hours=1), task_config_revision=1,
            content_policy_hash="c" * 64, state="frozen", plan_hash="h" * 64)
        session.add(plan)
        session.flush()
        slot = AiContentWindowPlanSlot(id="settlement-window", plan_id=plan.id,
            slot_ordinal=1, slot_revision=1, obligation_type=job.obligation_type,
            obligation_id=job.obligation_id, generation_sequence=1, account_id=11,
            due_at=now, context_scope_revision=1, context_snapshot_hash="s" * 64,
            context_route="general", content_mode="general", route_evidence_hash="e" * 64,
            prompt_contract_version="general_v1", state=state, claimed_by_job_id=job.id)
        session.add(slot)
        session.flush()
        job.window_slot_id = slot.id
        job.window_plan_hash = plan.plan_hash
        job.candidate_hash = action.candidate_hash
        session.commit()
