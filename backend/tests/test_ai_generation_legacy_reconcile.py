from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, AiContentWindowPlan, AiContentWindowPlanSlot, GenerationJob
from app.services._common import _now
from app.services.task_center.ai_generation_recovery import reconcile_generation_jobs
from tests.test_ai_generation_reconcile_fencing import (
    _action,
    _engine,
    _job,
    _seed_scope,
)


pytestmark = pytest.mark.no_postgres


def test_reconcile_recovers_exact_unowned_legacy_action() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_scope(session)
        job = _job("job-unowned", "slot-unowned", state="generating", owner="worker-old")
        action = _action(
            "action-unowned", "slot-unowned", job.id,
            status="pending", generation_status="generating",
        )
        session.add_all([job, action])
        session.commit()

        assert reconcile_generation_jobs(session, limit=10) == 1
        session.commit()

        refreshed_action = session.get(Action, action.id)
        refreshed_job = session.get(GenerationJob, job.id)
        assert refreshed_action.status == "pending"
        assert refreshed_action.payload["ai_generation_status"] == "pending"
        assert refreshed_job.state == "pending"
        assert refreshed_job.generation_stage == "generation_recovery"


def test_reconcile_preserves_provider_started_skipped_legacy_action_as_unknown() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_scope(session)
        job = _job("job-provider-started", "slot-provider-started", state="generating", owner="worker-old")
        action = _action(
            "action-provider-started", "slot-provider-started", job.id,
            status="skipped", owner="worker-old",
        )
        action.claim_owner = ""
        action.claim_token = ""
        action.lease_owner = ""
        action.lease_expires_at = None
        action.result = {"ai_provider_call_started_at": _now().isoformat()}
        session.add_all([job, action])
        session.commit()

        assert reconcile_generation_jobs(session, limit=10) == 1
        session.commit()

        refreshed_action = session.get(Action, action.id)
        refreshed_job = session.get(GenerationJob, job.id)
        action_count = session.scalar(select(func.count(Action.id)))
        assert action_count == 1
        assert refreshed_action.status == "pending"
        assert refreshed_action.payload["ai_generation_status"] == "ai_result_persist_unknown"
        assert refreshed_job.state == "unknown"
        assert refreshed_job.generation_stage == "ai_result_persist_unknown"


def test_reconcile_rejects_provider_started_action_with_different_live_owner() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_scope(session)
        job = _job("job-reclaimed", "slot-reclaimed", state="generating", owner="worker-old")
        action = _action(
            "action-reclaimed", "slot-reclaimed", job.id,
            status="skipped", owner="worker-new",
        )
        action.result = {"ai_provider_call_started_at": _now().isoformat()}
        session.add_all([job, action])
        session.commit()

        with pytest.raises(RuntimeError, match="action_claim_changed"):
            reconcile_generation_jobs(session, limit=10)
        session.rollback()

        refreshed_action = session.get(Action, action.id)
        refreshed_job = session.get(GenerationJob, job.id)
        assert refreshed_action.claim_owner == "worker-new"
        assert refreshed_job.generation_owner_id == "worker-old"


def test_reconcile_repairs_unowned_pending_generating_residue() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_scope(session)
        job = _job("job-pending-residue", "slot-pending-residue", state="pending")
        job.generation_stage = "generation_recovery"
        action = _action(
            "action-pending-residue", "slot-pending-residue", job.id,
            status="pending", generation_status="generating",
        )
        session.add_all([job, action])
        session.commit()

        assert reconcile_generation_jobs(session, limit=10) == 1
        session.commit()

        refreshed_action = session.get(Action, action.id)
        refreshed_job = session.get(GenerationJob, job.id)
        assert refreshed_action.status == "pending"
        assert refreshed_action.payload["ai_generation_status"] == "pending"
        assert refreshed_action.action_version == 2
        assert refreshed_job.state == "pending"
        assert refreshed_job.generation_stage == "generation_recovery"


def test_reconcile_repairs_unowned_routing_pending_generating_residue() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_scope(session)
        job = _job("job-routing-residue", "slot-routing-residue", state="pending")
        job.generation_stage = "routing"
        action = _action(
            "action-routing-residue", "slot-routing-residue", job.id,
            status="pending", generation_status="generating",
        )
        session.add_all([job, action])
        session.commit()

        assert reconcile_generation_jobs(session, limit=10) == 1
        session.commit()

        refreshed_action = session.get(Action, action.id)
        assert refreshed_action.payload["ai_generation_status"] == "pending"
        assert refreshed_action.action_version == 2
        assert session.get(GenerationJob, job.id).generation_stage == "routing"


def test_reconcile_preserves_pending_residue_with_live_job_owner() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_scope(session)
        job = _job(
            "job-live-pending-owner", "slot-live-pending-owner",
            state="pending", owner="worker-live",
        )
        job.generation_stage = "routing"
        action = _action(
            "action-live-pending-owner", "slot-live-pending-owner", job.id,
            status="pending", generation_status="generating",
        )
        session.add_all([job, action])
        session.commit()

        assert reconcile_generation_jobs(session, limit=10) == 0
        session.commit()

        refreshed_action = session.get(Action, action.id)
        assert refreshed_action.payload["ai_generation_status"] == "generating"
        assert refreshed_action.action_version == 1


def test_reconcile_does_not_retry_pending_residue_after_provider_started() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_scope(session)
        job = _job("job-pending-provider", "slot-pending-provider", state="pending")
        job.generation_stage = "generation_recovery"
        action = _action(
            "action-pending-provider", "slot-pending-provider", job.id,
            status="pending", generation_status="generating",
        )
        action.result = {"ai_provider_call_started_at": _now().isoformat()}
        session.add_all([job, action])
        session.commit()

        assert reconcile_generation_jobs(session, limit=10) == 0
        session.commit()

        refreshed_action = session.get(Action, action.id)
        assert refreshed_action.payload["ai_generation_status"] == "generating"
        assert refreshed_action.action_version == 1


def test_reconcile_pending_residue_batches_make_forward_progress() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_scope(session)
        actions = []
        for index in range(3):
            job = _job(f"job-residue-{index}", f"slot-residue-{index}", state="pending")
            job.generation_stage = "generation_recovery"
            action = _action(
                f"action-residue-{index}", f"slot-residue-{index}", job.id,
                status="pending", generation_status="generating",
            )
            actions.append(action)
            session.add_all([job, action])
        session.commit()

        assert [reconcile_generation_jobs(session, limit=1) for _ in range(4)] == [1, 1, 1, 0]
        session.commit()

        assert all(
            session.get(Action, action.id).payload["ai_generation_status"] == "pending"
            for action in actions
        )


def test_reconcile_invalidates_terminal_pre_gateway_slot_residue() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_scope(session)
        job = _job("job-terminal-slot", "slot-terminal", state="failed")
        plan = AiContentWindowPlan(
            id="plan-terminal-slot", tenant_id=1, task_id="task-1",
            task_lifecycle_epoch=1, scope_type="group", scope_id="7",
            pacing_plan_hash="p" * 64, period_key="terminal-period",
            window_start_at=_now(), window_end_at=_now(), task_config_revision=1,
            content_policy_hash="c" * 64, state="frozen", plan_hash="h" * 64,
        )
        session.add_all((job, plan))
        session.flush()
        slot = AiContentWindowPlanSlot(
            id="window-terminal-slot", plan_id=plan.id, slot_ordinal=1,
            obligation_type=job.obligation_type, obligation_id=job.obligation_id,
            generation_sequence=1, account_id=11, due_at=_now(),
            context_scope_revision=1, context_snapshot_hash="s" * 64,
            context_route="general", content_mode="general",
            route_evidence_hash="e" * 64, prompt_contract_version="general_v1",
            state="claimed", claimed_by_job_id=job.id, lease_epoch=1,
            lease_expires_at=_now(),
        )
        session.add(slot)
        session.flush()
        job.window_slot_id = slot.id
        session.commit()

        assert reconcile_generation_jobs(session, limit=10) == 1
        session.commit()

        assert slot.state == "invalidated"
        assert slot.claimed_by_job_id is None
        assert slot.lease_expires_at is None


def test_reconcile_preserves_gateway_bound_and_open_job_slots() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_scope(session)
        terminal = _job("job-terminal-gateway", "slot-gateway", state="failed")
        unknown = _job("job-unknown-claimed", "slot-unknown", state="unknown")
        plan = AiContentWindowPlan(
            id="plan-preserved", tenant_id=1, task_id="task-1",
            task_lifecycle_epoch=1, scope_type="group", scope_id="7",
            pacing_plan_hash="p" * 64, period_key="preserved-period",
            window_start_at=_now(), window_end_at=_now(), task_config_revision=1,
            content_policy_hash="c" * 64, state="frozen", plan_hash="h" * 64,
        )
        session.add_all((terminal, unknown, plan))
        session.flush()
        gateway_slot = _owned_slot(
            "window-gateway", plan.id, terminal, state="gateway_bound", ordinal=1,
        )
        unknown_slot = _owned_slot(
            "window-unknown", plan.id, unknown, state="claimed", ordinal=2,
        )
        session.add_all((gateway_slot, unknown_slot))
        session.flush()
        terminal.window_slot_id = gateway_slot.id
        unknown.window_slot_id = unknown_slot.id
        session.commit()

        assert reconcile_generation_jobs(session, limit=10) == 0
        session.commit()

        assert gateway_slot.state == "gateway_bound"
        assert gateway_slot.claimed_by_job_id == terminal.id
        assert unknown_slot.state == "claimed"
        assert unknown_slot.claimed_by_job_id == unknown.id


def _owned_slot(
    slot_id: str,
    plan_id: str,
    job: GenerationJob,
    *,
    state: str,
    ordinal: int,
) -> AiContentWindowPlanSlot:
    return AiContentWindowPlanSlot(
        id=slot_id, plan_id=plan_id, slot_ordinal=ordinal,
        obligation_type=job.obligation_type, obligation_id=job.obligation_id,
        generation_sequence=1, account_id=11, due_at=_now(),
        context_scope_revision=1, context_snapshot_hash="s" * 64,
        context_route="general", content_mode="general",
        route_evidence_hash="e" * 64, prompt_contract_version="general_v1",
        state=state, claimed_by_job_id=job.id, lease_epoch=1,
        lease_expires_at=_now(),
    )
