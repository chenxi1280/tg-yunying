from __future__ import annotations

from datetime import UTC, datetime, timedelta
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AiContentWindowPlan,
    AiContentWindowPlanSlot,
    ContentMixCycle,
    GenerationJob,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services._common import _now
from app.services.task_center.ai_acceptance import ai_acceptance_statuses
from app.services.task_center.ai_generation_recovery import (
    reconcile_generation_jobs,
    recover_stale_pre_gateway_generation,
)
from app.services.task_center.ai_generation_worker import drain_ai_generation
from app.services.task_center.ai_generator import AiGenerationUnavailable


pytestmark = pytest.mark.no_postgres


def test_stale_pre_gateway_recovery_resets_both_action_and_job() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _seed_basic_scope(session)
        now_val = _now()
        action = Action(
            id="action-recovery-1",
            tenant_id=1,
            task_id="task-1",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="executing",
            obligation_type="quantity_slot",
            obligation_id="slot-recovery-1",
            claim_owner="worker-1",
            claim_token="token-1",
            lease_owner="worker-1",
            lease_expires_at=now_val - timedelta(minutes=1),
            payload={
                "group_id": 7,
                "ai_generation_status": "generating",
                "ai_generation_claim_owner": "worker-1",
                "ai_generation_claim_token": "token-1",
                "generation_job_id": "job-recovery-1",
            },
            result={},
        )
        job = GenerationJob(
            id="job-recovery-1",
            tenant_id=1,
            task_id="task-1",
            obligation_type="quantity_slot",
            obligation_id="slot-recovery-1",
            generation_sequence=1,
            context_snapshot_version=1,
            state="generating",
            generation_owner_id="worker-1",
            lease_expires_at=now_val - timedelta(minutes=1),
            job_version=1,
        )
        session.add_all([action, job])
        session.commit()

        # Provider did not start -> should reset to pending
        assert recover_stale_pre_gateway_generation(action, session=session) is True
        session.commit()

        refreshed_action = session.get(Action, "action-recovery-1")
        refreshed_job = session.get(GenerationJob, "job-recovery-1")

        assert refreshed_action.status == "pending"
        assert refreshed_action.payload["ai_generation_status"] == "pending"
        assert refreshed_action.claim_owner == ""
        assert refreshed_action.lease_owner == ""
        assert refreshed_action.result["generation_stage"] == "generation_recovery"

        assert refreshed_job.state == "pending"
        assert refreshed_job.generation_owner_id == ""
        assert refreshed_job.lease_expires_at is None
        assert refreshed_job.generation_stage == "generation_recovery"
        assert refreshed_job.job_version == 2


def test_stale_pre_gateway_recovery_with_provider_started_sets_unknown() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _seed_basic_scope(session)
        now_val = _now()
        action = Action(
            id="action-recovery-2",
            tenant_id=1,
            task_id="task-1",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="executing",
            obligation_type="quantity_slot",
            obligation_id="slot-recovery-2",
            claim_owner="worker-1",
            claim_token="token-1",
            lease_owner="worker-1",
            lease_expires_at=now_val - timedelta(minutes=1),
            payload={
                "group_id": 7,
                "ai_generation_status": "generating",
                "ai_generation_attempt_id": "attempt-99",
                "ai_generation_claim_owner": "worker-1",
                "ai_generation_claim_token": "token-1",
                "generation_job_id": "job-recovery-2",
            },
            result={"ai_provider_call_started_at": now_val.isoformat()},
        )
        job = GenerationJob(
            id="job-recovery-2",
            tenant_id=1,
            task_id="task-1",
            obligation_type="quantity_slot",
            obligation_id="slot-recovery-2",
            generation_sequence=1,
            context_snapshot_version=1,
            state="generating",
            generation_owner_id="worker-1",
            lease_expires_at=now_val - timedelta(minutes=1),
            job_version=1,
        )
        session.add_all([action, job])
        session.commit()

        # Provider started -> should set unknown without re-calling provider
        assert recover_stale_pre_gateway_generation(action, session=session) is True
        session.commit()

        refreshed_action = session.get(Action, "action-recovery-2")
        refreshed_job = session.get(GenerationJob, "job-recovery-2")

        assert refreshed_action.payload["ai_generation_status"] == "ai_result_persist_unknown"
        assert refreshed_job.state == "unknown"
        assert refreshed_job.generation_owner_id == ""
        assert refreshed_job.lease_expires_at is None
        assert refreshed_job.generation_stage == "ai_result_persist_unknown"


def test_reconcile_generation_jobs_cancels_lifecycle_expired_jobs() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _seed_basic_scope(session)
        now_val = _now()
        task = session.get(Task, "task-1")
        task.task_lifecycle_epoch = 2  # Task lifecycle epoch advanced
        job = GenerationJob(
            id="job-expired-epoch",
            tenant_id=1,
            task_id="task-1",
            task_lifecycle_epoch=1,  # Stale epoch
            obligation_type="quantity_slot",
            obligation_id="slot-old",
            generation_sequence=1,
            context_snapshot_version=1,
            state="generating",
            generation_owner_id="worker-1",
            lease_expires_at=now_val - timedelta(minutes=5),
            job_version=1,
        )
        session.add(job)
        session.commit()

        reconciled = reconcile_generation_jobs(session, limit=10)
        assert reconciled == 1
        session.commit()

        refreshed_job = session.get(GenerationJob, "job-expired-epoch")
        assert refreshed_job.state == "cancelled"
        assert refreshed_job.generation_stage == "lifecycle_expired"
        assert refreshed_job.generation_owner_id == ""


def test_reconcile_keeps_provider_started_job_unknown_after_lifecycle_change() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _seed_basic_scope(session)
        now_val = _now()
        session.get(Task, "task-1").task_lifecycle_epoch = 2
        action = Action(
            id="action-provider-started-old-epoch",
            tenant_id=1,
            task_id="task-1",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="executing",
            task_lifecycle_epoch=1,
            obligation_type="quantity_slot",
            obligation_id="slot-provider-started-old-epoch",
            claim_owner="worker-1",
            claim_token="token-1",
            lease_owner="worker-1",
            lease_expires_at=now_val - timedelta(minutes=5),
            payload={
                "group_id": 7,
                "ai_generation_status": "generating",
                "ai_generation_attempt_id": "attempt-old-epoch",
                "ai_generation_claim_owner": "worker-1",
                "ai_generation_claim_token": "token-1",
                "generation_job_id": "job-provider-started-old-epoch",
            },
            result={"ai_provider_call_started_at": now_val.isoformat()},
        )
        job = GenerationJob(
            id="job-provider-started-old-epoch",
            tenant_id=1,
            task_id="task-1",
            task_lifecycle_epoch=1,
            obligation_type="quantity_slot",
            obligation_id="slot-provider-started-old-epoch",
            generation_sequence=1,
            context_snapshot_version=1,
            state="generating",
            generation_owner_id="worker-1",
            lease_expires_at=now_val - timedelta(minutes=5),
            job_version=1,
        )
        session.add_all([action, job])
        session.commit()

        assert reconcile_generation_jobs(session, limit=1) == 1
        session.commit()

        refreshed_action = session.get(Action, action.id)
        refreshed_job = session.get(GenerationJob, job.id)
        assert refreshed_action.status == "pending"
        assert refreshed_action.payload["ai_generation_status"] == "ai_result_persist_unknown"
        assert refreshed_job.state == "unknown"
        assert refreshed_job.generation_stage == "ai_result_persist_unknown"


def test_fact_first_v3_quantity_does_not_fake_content_mix_met() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        now_val = _now()
        session.add(Tenant(id=1, name="tenant"))
        task = Task(
            id="task-v3",
            tenant_id=1,
            name="v3 Task",
            type="group_ai_chat",
            status="running",
            fulfillment_contract_version="fact_first_v3",
            stats={"conversation_quality_e4_passed": True},
        )
        ledger = TaskDayLedger(
            id="ledger-v3-1",
            tenant_id=1,
            task_id="task-v3",
            timezone_snapshot="Asia/Shanghai",
            timezone_revision=1,
            obligation_local_date=now_val.date(),
            period_start_at=now_val,
            deadline_at=now_val + timedelta(days=1),
            day_phase="open",
            planning_anchor_at=now_val,
            lifecycle_status="open",
        )
        slot1 = TaskGroupDailyMessageSlot(
            id="slot-1",
            tenant_id=1,
            task_id="task-v3",
            task_day_ledger_id=ledger.id,
            target_operation_target_id=7,
            slot_kind="message",
            slot_ordinal=1,
            state="confirmed",
        )
        slot2 = TaskGroupDailyMessageSlot(
            id="slot-2",
            tenant_id=1,
            task_id="task-v3",
            task_day_ledger_id=ledger.id,
            target_operation_target_id=7,
            slot_kind="message",
            slot_ordinal=2,
            state="confirmed",
        )
        session.add_all([task, ledger, slot1, slot2])
        session.commit()

        statuses = ai_acceptance_statuses(session, task, task.stats, now=now_val)
        assert statuses == {
            "quantity_status": "met",
            "content_mix_status": "evaluating",
            "conversation_quality_status": "met",
            "acceptance_status": "evaluating",
        }


def test_fact_first_v3_content_mix_met_from_current_window_facts() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        now_val = _now()
        session.add(Tenant(id=1, name="tenant"))
        session.add(TgAccount(
            id=11,
            tenant_id=1,
            display_name="account-content",
            phone_masked="+861112",
            status="在线",
        ))
        task = Task(
            id="task-v3-content",
            tenant_id=1,
            name="v3 content facts",
            type="group_ai_chat",
            status="running",
            task_lifecycle_epoch=2,
            fulfillment_contract_version="fact_first_v3",
            stats={"conversation_quality_e4_passed": True},
        )
        ledger = TaskDayLedger(
            id="ledger-v3-content",
            tenant_id=1,
            task_id=task.id,
            timezone_snapshot="Asia/Shanghai",
            timezone_revision=1,
            obligation_local_date=now_val.date(),
            period_start_at=now_val,
            deadline_at=now_val + timedelta(days=1),
            day_phase="open",
            planning_anchor_at=now_val,
            lifecycle_status="open",
        )
        quantity = TaskGroupDailyMessageSlot(
            id="slot-content-1",
            tenant_id=1,
            task_id=task.id,
            task_day_ledger_id=ledger.id,
            target_operation_target_id=7,
            slot_kind="message",
            slot_ordinal=1,
            state="confirmed",
        )
        plan = AiContentWindowPlan(
            id="plan-content-1",
            tenant_id=1,
            task_id=task.id,
            task_lifecycle_epoch=2,
            scope_type="group",
            scope_id="7",
            pacing_plan_hash="p" * 64,
            period_key="content-period",
            window_start_at=now_val,
            window_end_at=now_val + timedelta(hours=1),
            task_config_revision=1,
            content_policy_hash="q" * 64,
            state="frozen",
            plan_hash="h" * 64,
        )
        content = AiContentWindowPlanSlot(
            plan_id=plan.id,
            slot_ordinal=1,
            obligation_type="quantity_slot",
            obligation_id=quantity.id,
            generation_sequence=1,
            account_id=11,
            due_at=now_val,
            context_scope_revision=1,
            context_snapshot_hash="c" * 64,
            context_route="general",
            content_mode="general",
            route_evidence_hash="e" * 64,
            prompt_contract_version="general_v3",
            state="gateway_bound",
        )
        session.add_all([task, ledger, quantity, plan, content])
        session.commit()

        statuses = ai_acceptance_statuses(session, task, task.stats, now=now_val)
        assert statuses["quantity_status"] == "met"
        assert statuses["content_mix_status"] == "met"
        assert statuses["acceptance_status"] == "met"


def test_fact_first_v3_acceptance_statuses_missed_when_slot_is_terminal() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        now_val = _now()
        session.add(Tenant(id=1, name="tenant"))
        task = Task(
            id="task-v3-missed",
            tenant_id=1,
            name="v3 Task Missed",
            type="group_ai_chat",
            status="running",
            fulfillment_contract_version="fact_first_v3",
            stats={"conversation_quality_e4_passed": True},
        )
        ledger = TaskDayLedger(
            id="ledger-v3-2",
            tenant_id=1,
            task_id="task-v3-missed",
            timezone_snapshot="Asia/Shanghai",
            timezone_revision=1,
            obligation_local_date=now_val.date(),
            period_start_at=now_val,
            deadline_at=now_val + timedelta(days=1),
            day_phase="open",
            planning_anchor_at=now_val,
            lifecycle_status="open",
        )
        slot1 = TaskGroupDailyMessageSlot(
            id="slot-1",
            tenant_id=1,
            task_id="task-v3-missed",
            task_day_ledger_id=ledger.id,
            target_operation_target_id=7,
            slot_kind="message",
            slot_ordinal=1,
            state="confirmed",
        )
        slot2 = TaskGroupDailyMessageSlot(
            id="slot-2",
            tenant_id=1,
            task_id="task-v3-missed",
            task_day_ledger_id=ledger.id,
            target_operation_target_id=7,
            slot_kind="message",
            slot_ordinal=2,
            state="terminal",
        )
        session.add_all([task, ledger, slot1, slot2])
        session.commit()

        statuses = ai_acceptance_statuses(session, task, task.stats, now=now_val)
        assert statuses["quantity_status"] == "missed"
        assert statuses["content_mix_status"] == "evaluating"
        assert statuses["acceptance_status"] == "missed"


def _seed_basic_scope(session: Session) -> None:
    session.add(Tenant(id=1, name="tenant"))
    session.add(Task(
        id="task-1",
        tenant_id=1,
        name="test task",
        type="group_ai_chat",
        status="running",
        task_lifecycle_epoch=1,
        fulfillment_contract_version="fact_first_v3",
    ))
    session.add(TgAccount(
        id=11,
        tenant_id=1,
        display_name="account-1",
        phone_masked="+861111",
        status="在线",
    ))
    session.add(TgGroup(
        id=7,
        tenant_id=1,
        tg_peer_id="-1007",
        title="group-7",
    ))
    session.commit()
