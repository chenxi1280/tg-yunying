from __future__ import annotations

from contextlib import contextmanager
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.models import Action, AiContentWindowPlan, AiContentWindowPlanSlot, GenerationJob
from app.services._common import _now
from app.services.task_center.ai_generation_pending_recovery import (
    recover_pending_generation_residue,
)
from app.services.task_center.ai_content_runtime import (
    invalidate_terminal_pre_gateway_obligation_slot,
    recover_terminal_pre_gateway_window_slots,
)


pytestmark = pytest.mark.isolated_postgres


def test_terminal_slot_recovery_releases_partial_unique_for_replacement() -> None:
    with _postgres_schema() as engine:
        with engine.begin() as connection:
            _create_tables(connection)
        with Session(engine, expire_on_commit=False) as session:
            job, plan = _terminal_job_and_plan()
            session.add_all((job, plan))
            session.flush()
            old_slot = _slot("old-slot", plan.id, job, state="claimed")
            session.add(old_slot)
            session.flush()
            job.window_slot_id = old_slot.id
            session.commit()

            assert recover_terminal_pre_gateway_window_slots(session, 1) == 1
            replacement = _slot(
                "replacement-slot",
                plan.id,
                job,
                state="frozen",
                ordinal=2,
                claimed_by_job_id=None,
            )
            session.add(replacement)
            session.commit()

            assert old_slot.state == "invalidated"
            assert old_slot.claimed_by_job_id is None
            current = session.scalars(select(AiContentWindowPlanSlot).where(
                AiContentWindowPlanSlot.state.in_(
                    ("frozen", "claimed", "candidate_ready", "gateway_bound")
                )
            )).all()
            assert [slot.id for slot in current] == [replacement.id]


def test_binding_guard_invalidates_exact_terminal_owner_with_row_lock() -> None:
    with _postgres_schema() as engine:
        with engine.begin() as connection:
            _create_tables(connection)
        with Session(engine, expire_on_commit=False) as session:
            job, plan = _terminal_job_and_plan()
            session.add_all((job, plan))
            session.flush()
            slot = _slot("binding-slot", plan.id, job, state="claimed")
            session.add(slot)
            session.flush()
            job.window_slot_id = slot.id
            session.commit()

            assert invalidate_terminal_pre_gateway_obligation_slot(
                session,
                obligation_type=job.obligation_type,
                obligation_id=job.obligation_id,
            )
            session.commit()

            assert slot.state == "invalidated"
            assert slot.claimed_by_job_id is None


def test_pending_routing_residue_recovers_with_postgres_json_guards() -> None:
    with _postgres_schema() as engine:
        with engine.begin() as connection:
            _create_tables(connection)
        with Session(engine, expire_on_commit=False) as session:
            job = _pending_routing_job()
            action = _pending_generating_action(job)
            session.add_all((job, action))
            session.commit()

            assert recover_pending_generation_residue(
                session, 1,
                action_resolver=lambda current, _job: current.get(Action, action.id),
            ) == 1
            session.commit()
            session.expire(action)

            assert action.status == "pending"
            assert action.payload["ai_generation_status"] == "pending"
            assert job.generation_stage == "routing"


def _pending_routing_job() -> GenerationJob:
    return GenerationJob(
        id="pending-routing-job", tenant_id=1, task_id="task-1",
        task_lifecycle_epoch=1, obligation_type="coverage",
        obligation_id="pending-routing-coverage", generation_sequence=1,
        context_snapshot_version=1, state="pending", generation_stage="routing",
    )


def _pending_generating_action(job: GenerationJob) -> Action:
    return Action(
        id="pending-routing-action", tenant_id=1, task_id="task-1",
        task_type="group_ai_chat", action_type="send_message", account_id=1,
        status="pending", obligation_type=job.obligation_type,
        obligation_id=job.obligation_id, task_lifecycle_epoch=1,
        payload={
            "message_text": "", "generation_job_id": job.id,
            "ai_generation_status": "generating",
            "ai_generation_claim_owner": "", "ai_generation_claim_token": "",
        },
        result={},
    )


def _terminal_job_and_plan() -> tuple[GenerationJob, AiContentWindowPlan]:
    now_value = _now()
    job = GenerationJob(
        id="terminal-job", tenant_id=1, task_id="task-1",
        task_lifecycle_epoch=1, obligation_type="coverage",
        obligation_id="coverage-1", generation_sequence=1,
        context_snapshot_version=1, state="failed",
    )
    plan = AiContentWindowPlan(
        id="plan-1", tenant_id=1, task_id="task-1", task_lifecycle_epoch=1,
        scope_type="group", scope_id="7", pacing_plan_hash="p" * 64,
        period_key="period-1", window_start_at=now_value,
        window_end_at=now_value, task_config_revision=1,
        content_policy_hash="c" * 64, state="frozen", plan_hash="h" * 64,
    )
    return job, plan


def _slot(
    slot_id: str,
    plan_id: str,
    job: GenerationJob,
    *,
    state: str,
    ordinal: int = 1,
    claimed_by_job_id: str | None = "terminal-job",
) -> AiContentWindowPlanSlot:
    return AiContentWindowPlanSlot(
        id=slot_id, plan_id=plan_id, slot_ordinal=ordinal,
        obligation_type=job.obligation_type, obligation_id=job.obligation_id,
        generation_sequence=1, account_id=1, due_at=_now(),
        context_scope_revision=1, context_snapshot_hash="s" * 64,
        context_route="general", content_mode="general",
        route_evidence_hash="e" * 64, prompt_contract_version="general_v1",
        state=state, claimed_by_job_id=claimed_by_job_id, lease_epoch=1,
        lease_expires_at=_now() if claimed_by_job_id else None,
    )


def _create_tables(connection) -> None:
    connection.exec_driver_sql("CREATE TABLE tenants (id INTEGER PRIMARY KEY)")
    connection.exec_driver_sql("CREATE TABLE tasks (id VARCHAR(36) PRIMARY KEY)")
    connection.exec_driver_sql("CREATE TABLE tg_accounts (id INTEGER PRIMARY KEY)")
    connection.exec_driver_sql(
        "CREATE TABLE task_group_daily_message_slots (id VARCHAR(36) PRIMARY KEY)"
    )
    connection.exec_driver_sql(
        "CREATE TABLE content_mix_cycle_slots (id VARCHAR(36) PRIMARY KEY)"
    )
    connection.exec_driver_sql(
        "INSERT INTO tenants VALUES (1); "
        "INSERT INTO tasks VALUES ('task-1'); "
        "INSERT INTO tg_accounts VALUES (1)"
    )
    GenerationJob.__table__.create(connection)
    Action.__table__.create(connection)
    AiContentWindowPlan.__table__.create(connection)
    AiContentWindowPlanSlot.__table__.create(connection)


@contextmanager
def _postgres_schema():
    schema = f"test_ai_terminal_slot_{uuid4().hex}"
    database_url = os.environ["TEST_DATABASE_URL"]
    admin_engine = create_engine(database_url, future=True, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        database_url,
        future=True,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        yield engine
    finally:
        engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()
