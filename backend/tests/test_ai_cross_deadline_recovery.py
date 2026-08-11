from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ContentMixCycle,
    ContentMixCycleSlot,
    ExecutionAttempt,
    OperationTarget,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    Tenant,
)
from scripts import recover_ai_cross_deadline_actions as recovery
from app.services.task_center import dispatcher


pytestmark = pytest.mark.no_postgres
DEPLOYED_SHA = "a" * 40


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        yield current


def test_preview_apply_replans_only_exact_pre_gateway_action(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = _seed_cross_deadline_action(session)
    deadline = datetime(2026, 8, 12)
    monkeypatch.setattr(recovery, "_now", lambda: datetime(2026, 8, 11, 23))
    monkeypatch.setattr(dispatcher, "_reconcile_content_mix_for_slot", lambda *_args: None)
    request = _request(action.task_id)
    manifest = recovery.build_manifest(session, request, lock=False)
    state_hash = recovery.manifest_hash(manifest)

    apply_request = recovery.RecoveryRequest(
        **{**request.__dict__, "apply": True, "expected_state_hash": state_hash},
    )
    locked = recovery.build_manifest(session, apply_request, lock=True)
    applied = recovery.apply_recovery(session, apply_request, locked)
    session.commit()

    slot = session.get(ContentMixCycleSlot, action.content_mix_cycle_slot_id)
    quantity = session.get(TaskGroupDailyMessageSlot, action.primary_quantity_slot_id)
    assert applied == [action.id]
    assert action.status == "skipped"
    assert action.result["error_code"] == "ai_schedule_beyond_task_day_deadline"
    assert slot.slot_state == "replan_required"
    assert quantity.state == "open"
    assert recovery.build_manifest(session, request, lock=False)["candidate_count"] == 0


def test_gateway_started_action_is_never_in_recovery_manifest(
    session: Session,
) -> None:
    action = _seed_cross_deadline_action(session)
    session.add(ExecutionAttempt(
        tenant_id=1,
        action_id=action.id,
        attempt_no=1,
        gateway_call_started_at=datetime(2026, 8, 11, 23),
    ))
    session.flush()

    manifest = recovery.build_manifest(session, _request(action.task_id), lock=False)

    assert manifest["candidate_count"] == 0


def test_apply_rejects_action_version_drift(session: Session) -> None:
    action = _seed_cross_deadline_action(session)
    request = _request(action.task_id)
    manifest = recovery.build_manifest(session, request, lock=False)
    state_hash = recovery.manifest_hash(manifest)
    action.action_version += 1
    session.flush()
    apply_request = recovery.RecoveryRequest(
        **{**request.__dict__, "apply": True, "expected_state_hash": state_hash},
    )

    with pytest.raises(RuntimeError, match="state hash changed"):
        recovery.apply_recovery(
            session,
            apply_request,
            recovery.build_manifest(session, apply_request, lock=True),
        )


def _request(task_id: str) -> recovery.RecoveryRequest:
    return recovery.RecoveryRequest(
        task_ids=(task_id,),
        deployed_sha=DEPLOYED_SHA,
        apply=False,
        expected_state_hash="",
        actor="codex-production-recovery",
        approval_ref="intake-2026-08-11-production-due-backlog-001",
    )


def _seed_cross_deadline_action(session: Session) -> Action:
    session.add(Tenant(id=1, name="租户"))
    target = OperationTarget(
        id=31,
        tenant_id=1,
        target_type="group",
        tg_peer_id="-10031",
        title="目标群",
        auth_status="已授权运营",
        can_send=True,
    )
    task = Task(
        id="cross-deadline-task",
        tenant_id=1,
        name="跨日恢复",
        type="group_ai_chat",
        status="running",
        fulfillment_contract_version="fact_first_v3",
    )
    ledger = TaskDayLedger(
        id="ledger-cross-day",
        tenant_id=1,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 8, 11),
        period_start_at=datetime(2026, 8, 11),
        deadline_at=datetime(2026, 8, 12),
        day_phase="full_day",
        planning_anchor_at=datetime(2026, 8, 11),
    )
    quantity = TaskGroupDailyMessageSlot(
        id="quantity-cross-day",
        tenant_id=1,
        task_id=task.id,
        task_day_ledger_id=ledger.id,
        target_operation_target_id=target.id,
        slot_kind="extra_volume",
        slot_ordinal=1,
        state="open",
    )
    session.add_all([target, task, ledger, quantity])
    session.flush()
    action = Action(
        id="action-cross-day",
        tenant_id=1,
        task_id=task.id,
        task_type="group_ai_chat",
        action_type="send_message",
        status="pending",
        scheduled_at=datetime(2026, 8, 12, 1),
        primary_quantity_slot_id=quantity.id,
        payload={"primary_quantity_slot_id": quantity.id},
    )
    session.add(action)
    session.flush()
    cycle = ContentMixCycle(
        id="cycle-cross-day",
        tenant_id=1,
        task_id=task.id,
        target_operation_target_id=target.id,
        task_day_ledger_id=ledger.id,
        cycle_seq=1,
        config_revision=1,
        scope_total_slots=1,
        allocation_seed="seed",
        allocation_closed_at=datetime(2026, 8, 11),
    )
    slot = ContentMixCycleSlot(
        id="content-slot-cross-day",
        tenant_id=1,
        cycle_id=cycle.id,
        slot_index=0,
        primary_quantity_slot_id=quantity.id,
        relation_kind="direct",
        current_action_id=action.id,
        slot_state="materialized",
    )
    session.add_all([cycle, slot])
    session.flush()
    action.content_mix_cycle_slot_id = slot.id
    action.payload = {
        **action.payload,
        "content_mix_cycle_slot_id": slot.id,
    }
    session.flush()
    return action
