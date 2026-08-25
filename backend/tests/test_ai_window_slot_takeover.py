from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import (
    AiContentWindowPlan,
    AiContentWindowPlanSlot,
    GenerationJob,
    Task,
    Tenant,
    TgAccount,
)
from app.services.task_center.ai_generation_parallel import (
    ParallelGenerationClaim,
    finish_generation_job,
)
from app.services.task_center.ai_generation_recovery import (
    _reset_generation_job_for_cached_retry,
)
from app.services.task_center.task_pause_cleanup import cancel_open_generation_jobs


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 8, 25, 20, 0)
TASK_ID = "window-slot-takeover"
JOB_ID = "terminal-generation-job"
SLOT_ID = "claimed-window-slot"


def _session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_claimed_slot(factory, *, job_state: str, owner: str = "") -> None:
    with factory() as session:
        session.add_all(
            [
                Tenant(id=1, name="tenant"),
                TgAccount(
                    id=1,
                    tenant_id=1,
                    display_name="account",
                    phone_masked="***",
                ),
                Task(
                    id=TASK_ID,
                    tenant_id=1,
                    name="task",
                    type="group_ai_chat",
                    status="paused",
                ),
            ]
        )
        job = GenerationJob(
            id=JOB_ID,
            tenant_id=1,
            task_id=TASK_ID,
            task_lifecycle_epoch=8,
            obligation_type="coverage",
            obligation_id="coverage-owner",
            generation_sequence=1,
            context_snapshot_version=1,
            state=job_state,
            generation_owner_id=owner,
        )
        session.add(job)
        plan = AiContentWindowPlan(
            tenant_id=1,
            task_id=TASK_ID,
            task_lifecycle_epoch=8,
            scope_type="group",
            scope_id="7",
            pacing_plan_hash="a" * 64,
            period_key="2026-08-25:coverage-owner:1",
            window_start_at=NOW,
            window_end_at=NOW + timedelta(hours=1),
            task_config_revision=2,
            content_policy_hash="b" * 64,
            state="frozen",
            plan_hash="c" * 64,
        )
        session.add(plan)
        session.flush()
        slot = AiContentWindowPlanSlot(
            id=SLOT_ID,
            plan_id=plan.id,
            slot_ordinal=1,
            slot_revision=1,
            obligation_type="coverage",
            obligation_id="coverage-owner",
            generation_sequence=1,
            account_id=1,
            due_at=NOW,
            context_scope_revision=1,
            context_snapshot_hash="d" * 64,
            context_route="adult_service_inquiry",
            content_mode="adult_service_inquiry",
            route_evidence_hash="e" * 64,
            prompt_contract_version="adult_service_inquiry_v1",
            state="claimed",
            claimed_by_job_id=job.id,
        )
        session.add(slot)
        session.flush()
        job.window_slot_id = slot.id
        session.commit()


def test_pause_invalidates_historical_terminal_pre_gateway_slot() -> None:
    factory = _session_factory()
    _seed_claimed_slot(factory, job_state="failed")

    with factory() as session:
        task = session.get(Task, TASK_ID)
        assert cancel_open_generation_jobs(session, task) == 0
        session.commit()

    with factory() as session:
        slot = session.get(AiContentWindowPlanSlot, SLOT_ID)
        assert slot.state == "invalidated"
        assert slot.claimed_by_job_id is None


def test_failed_generation_releases_claimed_slot_immediately() -> None:
    factory = _session_factory()
    _seed_claimed_slot(factory, job_state="generating", owner="worker-1")
    claim = ParallelGenerationClaim(
        action_id="missing-action-is-allowed",
        job_id=JOB_ID,
        owner="worker-1",
        token="claim-token",
    )

    finish_generation_job(factory, claim, state="failed")

    with factory() as session:
        job = session.get(GenerationJob, JOB_ID)
        slot = session.get(AiContentWindowPlanSlot, SLOT_ID)
        assert job.state == "failed"
        assert slot.state == "invalidated"
        assert slot.claimed_by_job_id is None


def test_persist_unknown_requeues_same_job_without_releasing_slot() -> None:
    factory = _session_factory()
    _seed_claimed_slot(factory, job_state="generating", owner="worker-1")

    with factory() as session:
        job = session.get(GenerationJob, JOB_ID)
        job.lease_expires_at = NOW + timedelta(minutes=10)
        _reset_generation_job_for_cached_retry(
            session,
            {"generation_job_id": JOB_ID},
        )
        session.commit()

    with factory() as session:
        job = session.get(GenerationJob, JOB_ID)
        slot = session.get(AiContentWindowPlanSlot, SLOT_ID)
        assert job.state == "pending"
        assert job.generation_stage == "persist_retry"
        assert job.generation_owner_id == ""
        assert job.lease_expires_at is None
        assert slot.state == "claimed"
        assert slot.claimed_by_job_id == job.id
