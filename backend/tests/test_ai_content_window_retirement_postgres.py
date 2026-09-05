import pytest

from app.database import SessionLocal
from app.models import Action, AiContentWindowPlan, AiContentWindowPlanSlot
from app.models import ExecutionAttempt, FulfillmentRemoteFact, GenerationJob, Task, Tenant, TgAccount
from app.services._common import _now
from app.services.task_center.ai_content_runtime import invalidate_terminal_pre_gateway_obligation_slot


TENANT_ID = 991350
TASK_ID = "window-retirement-pg"
ACCOUNT_ID = 991350


@pytest.mark.parametrize("has_attempt", [False, True])
def test_postgres_retirement_requires_no_execution_attempt(has_attempt):
    with SessionLocal() as session:
        action, job, slot = _seed(session)
        if has_attempt:
            session.add(ExecutionAttempt(tenant_id=TENANT_ID, action_id=action.id))
            session.flush()
        changed = invalidate_terminal_pre_gateway_obligation_slot(session,
            obligation_type=job.obligation_type, obligation_id=job.obligation_id)
        assert changed is not has_attempt
        assert slot.state == ("gateway_bound" if has_attempt else "invalidated")
        assert job.state == "ready" and action.status == "failed"
        session.rollback()


@pytest.mark.parametrize("fact_kind", ["safely_not_executed", "remote_outcome_unknown"])
def test_postgres_retirement_distinguishes_terminal_remote_evidence(fact_kind):
    with SessionLocal() as session:
        action, job, slot = _seed(session)
        attempt = ExecutionAttempt(id="retirement-attempt", tenant_id=TENANT_ID, action_id=action.id,
            task_lifecycle_epoch=1, status="failed", gateway_call_started_at=_now(), after_call_at=_now())
        fact = FulfillmentRemoteFact(tenant_id=TENANT_ID, task_id=TASK_ID, task_type=action.task_type,
            action_id=action.id, attempt_id=attempt.id, mutation_kind=action.action_type,
            obligation_type=job.obligation_type, obligation_id=job.obligation_id, fact_kind=fact_kind,
            remote_mutation_key_hash="m" * 64, gateway_request_hash="g" * 64,
            fact_identity_hash="f" * 64, observed_at=_now())
        session.add_all([attempt, fact])
        session.flush()
        changed = invalidate_terminal_pre_gateway_obligation_slot(session,
            obligation_type=job.obligation_type, obligation_id=job.obligation_id)
        assert changed is (fact_kind == "safely_not_executed")
        assert slot.state == ("invalidated" if changed else "gateway_bound")
        assert session.get(ExecutionAttempt, attempt.id).status == "failed"
        assert session.get(FulfillmentRemoteFact, fact.fact_id).fact_kind == fact_kind
        session.rollback()


def _seed(session):
    session.add(Tenant(id=TENANT_ID, name="window retirement postgres"))
    session.flush()
    session.add(TgAccount(id=ACCOUNT_ID, tenant_id=TENANT_ID,
                          display_name="window account", phone_masked="test"))
    session.add(Task(id=TASK_ID, tenant_id=TENANT_ID, name="window retirement",
                     type="group_ai_chat", status="running"))
    session.flush()
    job = GenerationJob(id="retirement-job", tenant_id=TENANT_ID, task_id=TASK_ID,
        task_lifecycle_epoch=1, obligation_type="coverage", obligation_id="retirement-owner",
        generation_sequence=1, context_snapshot_version=1, state="ready", generation_stage="gateway_bound")
    plan = AiContentWindowPlan(id="retirement-plan", tenant_id=TENANT_ID, task_id=TASK_ID,
        task_lifecycle_epoch=1, scope_type="group", scope_id="7", pacing_plan_hash="p" * 64,
        period_key="period", window_start_at=_now(), window_end_at=_now(),
        task_config_revision=1, content_policy_hash="c" * 64, plan_hash="h" * 64)
    action = Action(id="retirement-action", tenant_id=TENANT_ID, task_id=TASK_ID,
        task_type="group_ai_chat", action_type="send_message", account_id=ACCOUNT_ID,
        obligation_type=job.obligation_type, obligation_id=job.obligation_id,
        task_lifecycle_epoch=1, status="failed",
        payload={"generation_job_id": job.id, "ai_generation_status": "ready"})
    session.add_all([job, plan, action])
    session.flush()
    slot = AiContentWindowPlanSlot(id="retirement-slot", plan_id=plan.id, slot_ordinal=1,
        obligation_type=job.obligation_type, obligation_id=job.obligation_id,
        generation_sequence=1, account_id=ACCOUNT_ID, due_at=_now(), context_scope_revision=1,
        context_snapshot_hash="s" * 64, context_route="general", content_mode="general",
        route_evidence_hash="r" * 64, prompt_contract_version="general_v1",
        state="gateway_bound", claimed_by_job_id=job.id)
    session.add(slot)
    session.flush()
    job.window_slot_id = slot.id
    session.flush()
    return action, job, slot
