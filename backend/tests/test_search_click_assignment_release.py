from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ConsistencyQuarantine,
    DispatchAllocationExclusion,
    DispatchClaimReservation,
    DispatchClaimScope,
    DispatchClaimShardAllocation,
    DispatchClaimWindow,
    ExecutionAttempt,
    OperationTarget,
    SearchClickAssignmentEpoch,
    SearchClickFulfillmentObligation,
    SearchClickOpportunityAssignment,
    Task,
    TaskDayLedger,
    Tenant,
    TgAccount,
)
from app.services._common import _now
from app.services.task_center import prebound_search_claim
from app.services.task_center.prebound_search_claim import (
    confirm_prebound_search_claim,
    plan_prebound_search_claims,
)
from app.services.task_center.search_click_assignment_release import (
    release_search_click_assignment,
)
from app.timezone import BEIJING_TZ


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _seed_assignment(db)
        yield db


def test_prebound_assignment_claim_consumes_bound_counter(session: Session) -> None:
    action = session.get(Action, "action-1")
    plan = plan_prebound_search_claims(session, [action])
    binding = plan.bindings_by_action_id[action.id]
    action.status = "claiming"

    assert confirm_prebound_search_claim(session, action, binding)

    reservation = session.get(DispatchClaimReservation, "reservation-1")
    assignment = session.get(SearchClickOpportunityAssignment, "assignment-1")
    assert reservation.bound_count == 0
    assert reservation.claimed_count == 1
    assert assignment.state == "claimed"
    assert action.result["dispatch_claim_active"] is True


def test_prebound_assignment_accepts_postgres_aware_window(
    session: Session,
) -> None:
    action = session.get(Action, "action-1")
    window = session.get(DispatchClaimWindow, "window-1")
    window.bucket_end = _now().replace(tzinfo=BEIJING_TZ) + timedelta(minutes=1)

    plan = plan_prebound_search_claims(session, [action])
    binding = plan.bindings_by_action_id[action.id]
    action.status = "claiming"

    assert confirm_prebound_search_claim(session, action, binding)


def test_prebound_confirm_uses_central_claim_lock_order(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = session.get(Action, "action-1")
    binding = plan_prebound_search_claims(
        session,
        [action],
    ).bindings_by_action_id[action.id]
    action.status = "claiming"
    order: list[str] = []
    original_locked = prebound_search_claim._locked
    original_assignment = prebound_search_claim._locked_assignment

    def track_locked(current, model, row_id):
        order.append(model.__name__)
        return original_locked(current, model, row_id)

    def track_assignment(current, current_action):
        order.append("SearchClickOpportunityAssignment")
        return original_assignment(current, current_action)

    monkeypatch.setattr(prebound_search_claim, "_locked", track_locked)
    monkeypatch.setattr(
        prebound_search_claim,
        "_locked_assignment",
        track_assignment,
    )

    assert confirm_prebound_search_claim(session, action, binding)
    assert order == [
        "DispatchClaimWindow",
        "DispatchClaimShardAllocation",
        "DispatchClaimReservation",
        "SearchClickOpportunityAssignment",
    ]


def test_pre_gateway_release_is_idempotent_and_opens_one_rebuild_wave(
    session: Session,
) -> None:
    action = session.get(Action, "action-1")
    plan = plan_prebound_search_claims(session, [action])
    action.status = "claiming"
    assert confirm_prebound_search_claim(
        session,
        action,
        plan.bindings_by_action_id[action.id],
    )
    now_value = _now()

    first = release_search_click_assignment(
        session,
        "assignment-1",
        trigger_key="pre_gateway:action-1:route_invalid",
        reason_code="search_assignment_pre_gateway_terminal",
        now_value=now_value,
    )
    replay = release_search_click_assignment(
        session,
        "assignment-1",
        trigger_key="pre_gateway:action-1:route_invalid",
        reason_code="search_assignment_pre_gateway_terminal",
        now_value=now_value,
    )
    overlap = release_search_click_assignment(
        session,
        "assignment-1",
        trigger_key="task_stopped:action-1",
        reason_code="unclaimed_action_no_longer_due",
        now_value=now_value,
    )

    reservation = session.get(DispatchClaimReservation, "reservation-1")
    assignment = session.get(SearchClickOpportunityAssignment, "assignment-1")
    window = session.get(DispatchClaimWindow, "window-1")
    exclusion_count = session.scalar(select(func.count(DispatchAllocationExclusion.id)))
    assert replay.id == first.id
    assert first.release_unit_count == 1
    assert overlap.already_released_unit_count == 1
    assert reservation.claimed_count == 0
    assert reservation.released_count == 1
    assert assignment.state == "released"
    assert exclusion_count == 1
    assert window.allocation_epoch == 2
    assert window.rebuild_input_version == 1
    assert window.allocation_state == "rebuild_required"


def test_release_counter_contradiction_is_reconciled_before_release(
    session: Session,
) -> None:
    reservation = session.get(DispatchClaimReservation, "reservation-1")
    reservation.bound_count = 0
    session.commit()

    batch = release_search_click_assignment(
        session,
        "assignment-1",
        trigger_key="pre_gateway:action-1:counter_conflict",
        reason_code="search_assignment_pre_gateway_terminal",
        now_value=_now(),
    )

    quarantine = session.scalar(select(ConsistencyQuarantine))
    assignment = session.get(SearchClickOpportunityAssignment, "assignment-1")
    assert quarantine is not None
    assert quarantine.reason_code == "dispatch_release_counter_invariant"
    assert quarantine.status == "resolved"
    assert batch.release_unit_count == 1
    assert assignment.state == "released"
    assert session.scalar(select(func.count(DispatchAllocationExclusion.id))) == 1


def test_released_assignment_without_release_facts_is_quarantined(
    session: Session,
) -> None:
    assignment = session.get(SearchClickOpportunityAssignment, "assignment-1")
    assignment.state = "released"
    session.commit()

    with pytest.raises(RuntimeError, match="release_fact_incomplete"):
        release_search_click_assignment(
            session,
            "assignment-1",
            trigger_key="repair:orphan-release",
            reason_code="search_assignment_pre_gateway_terminal",
            now_value=_now(),
        )

    quarantine = session.scalar(select(ConsistencyQuarantine))
    assert quarantine is not None
    assert quarantine.reason_code == "release_fact_incomplete"
    assert quarantine.status == "active"


def test_release_and_gateway_fact_conflict_stays_quarantined(
    session: Session,
) -> None:
    release_search_click_assignment(
        session,
        "assignment-1",
        trigger_key="release:before-late-gateway",
        reason_code="search_assignment_pre_gateway_terminal",
        now_value=_now(),
    )
    session.add(ExecutionAttempt(
        tenant_id=1,
        action_id="action-1",
        gateway_call_started_at=_now(),
        status="after_call",
    ))
    session.commit()

    with pytest.raises(RuntimeError, match="release_claim_fact_conflict"):
        release_search_click_assignment(
            session,
            "assignment-1",
            trigger_key="release:late-gateway-conflict",
            reason_code="search_assignment_pre_gateway_terminal",
            now_value=_now(),
        )

    quarantine = session.scalar(select(ConsistencyQuarantine).where(
        ConsistencyQuarantine.reason_code == "release_claim_fact_conflict"
    ))
    assert quarantine is not None
    assert quarantine.status == "active"


def test_complete_release_facts_realign_pre_gateway_state(
    session: Session,
) -> None:
    release_search_click_assignment(
        session,
        "assignment-1",
        trigger_key="release:complete-facts",
        reason_code="search_assignment_pre_gateway_terminal",
        now_value=_now(),
    )
    assignment = session.get(SearchClickOpportunityAssignment, "assignment-1")
    reservation = session.get(DispatchClaimReservation, "reservation-1")
    action = session.get(Action, "action-1")
    assignment.state = "action_bound"
    reservation.bound_count = 1
    reservation.released_count = 0
    action.status = "pending"
    session.commit()

    replay = release_search_click_assignment(
        session,
        "assignment-1",
        trigger_key="release:repair-complete-facts",
        reason_code="search_assignment_pre_gateway_terminal",
        now_value=_now(),
    )

    assert replay.already_released_unit_count == 1
    assert assignment.state == "released"
    assert reservation.bound_count == 0
    assert reservation.released_count == 1
    assert action.status == "skipped"


def _seed_assignment(session: Session) -> None:
    now_value = _now()
    session.add(Tenant(id=1, name="tenant"))
    session.add(TgAccount(
        id=1,
        tenant_id=1,
        display_name="account",
        phone_masked="+861***0001",
        status="在线",
    ))
    target = OperationTarget(
        id=1,
        tenant_id=1,
        target_type="group",
        tg_peer_id="target_group",
        username="target_group",
        title="target",
    )
    session.add(target)
    task = Task(
        id="task-1",
        tenant_id=1,
        name="click",
        type="search_click",
        status="running",
    )
    session.add(task)
    ledger = TaskDayLedger(
        id="ledger-1",
        tenant_id=1,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=now_value.date(),
        period_start_at=now_value,
        deadline_at=now_value + timedelta(days=1),
        day_phase="full_day",
        planning_anchor_at=now_value,
    )
    session.add(ledger)
    obligation = SearchClickFulfillmentObligation(
        id="obligation-1",
        tenant_id=1,
        task_day_ledger_id=ledger.id,
        target_id=target.id,
        click_obligation_ordinal=1,
        status="action_bound",
        source_action_id="action-1",
    )
    session.add(obligation)
    _seed_dispatch_rows(session, now_value)
    epoch = SearchClickAssignmentEpoch(
        id="epoch-1",
        dispatch_claim_window_id="window-1",
        dispatch_allocation_epoch=1,
        solver_owner_lease_id="worker-1",
        solver_fencing_token="token-1",
        solver_claimed_at=now_value,
        solver_problem_hash="a" * 64,
        solver_input_hash="b" * 64,
        outcome="optimal",
        finalize_status="finalized",
    )
    session.add(epoch)
    action = Action(
        id="action-1",
        tenant_id=1,
        task_id=task.id,
        task_type="search_click",
        action_type="search_join",
        account_id=1,
        status="pending",
        scheduled_at=now_value,
        payload={
            "search_click_assignment_id": "assignment-1",
            "search_click_obligation_id": obligation.id,
        },
        result={
            "dispatch_prebound": True,
            "search_click_assignment_id": "assignment-1",
        },
    )
    session.add(action)
    session.add(SearchClickOpportunityAssignment(
        id="assignment-1",
        tenant_id=1,
        task_id=task.id,
        task_day_ledger_id=ledger.id,
        obligation_id=obligation.id,
        search_click_assignment_epoch_id=epoch.id,
        dispatch_claim_reservation_id="reservation-1",
        fulfillment_lane_claim_ordinal=1,
        account_id=1,
        authorization_id=1,
        keyword_hash="c" * 64,
        proxy_route_id="proxy-1",
        protocol_sample_version="v1",
        resource_snapshot_hash="d" * 64,
        action_id=action.id,
        state="action_bound",
    ))
    session.commit()


def _seed_dispatch_rows(session: Session, now_value) -> None:
    session.add(DispatchClaimScope(
        id="scope-1",
        dispatcher_scope="task_center_dispatch",
        claim_capacity=1,
    ))
    session.add(DispatchClaimWindow(
        id="window-1",
        dispatcher_scope="task_center_dispatch",
        bucket_start=now_value - timedelta(seconds=1),
        bucket_end=now_value + timedelta(minutes=1),
        claim_capacity=1,
        unclaimed_allocated_count=1,
        allocation_epoch=1,
        allocation_state="ready",
    ))
    session.add(DispatchClaimShardAllocation(
        id="shard-1",
        dispatch_claim_window_id="window-1",
        dispatch_allocation_epoch=1,
        account_shard_total=1,
        account_shard_index=0,
        unclaimed_allocated_count=1,
    ))
    session.add(DispatchClaimReservation(
        id="reservation-1",
        dispatch_claim_shard_allocation_id="shard-1",
        dispatch_allocation_epoch=1,
        tenant_id=1,
        task_id="task-1",
        claim_class="search_join",
        bucket_start=now_value,
        required_claims=1,
        reserved_claims=1,
        bound_count=1,
    ))
