from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ConsistencyQuarantine,
    DispatchAllocationExclusion,
    DispatchClaimReservation,
    DispatchClaimWindow,
    ExecutionAttempt,
    SearchClickOpportunityAssignment,
)
from app.services._common import _now
from app.services.task_center import (
    dispatch_reservations,
    dispatcher,
    prebound_search_claim,
    search_click_release_locking,
)
from app.services.task_center.prebound_search_claim import (
    confirm_prebound_search_claim,
    plan_prebound_search_claims,
)
from app.services.task_center.search_click_assignment_release import (
    release_search_click_assignment,
)
from app.timezone import BEIJING_TZ
from search_click_assignment_test_support import seed_assignment


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed_assignment(db)
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


def test_expired_prebound_assignment_releases_instead_of_rebinding(
    session: Session,
) -> None:
    action = session.get(Action, "action-1")
    window = session.get(DispatchClaimWindow, "window-1")
    window.bucket_end = _now() - timedelta(seconds=1)

    plan = plan_prebound_search_claims(session, [action])
    binding = plan.bindings_by_action_id[action.id]
    action.status = "claiming"
    action.claim_owner = "worker-1"
    action.claim_token = "token-1"
    batch = dispatcher.ActionClaimBatch(
        (action.id,),
        "worker-1",
        "token-1",
        {action.id: binding},
    )

    assert not confirm_prebound_search_claim(session, action, binding)
    dispatcher._release_failed_claim_confirmation(
        session,
        action.id,
        batch,
    )

    assignment = session.get(SearchClickOpportunityAssignment, "assignment-1")
    reservation = session.get(DispatchClaimReservation, "reservation-1")
    assert action.status == "skipped"
    assert assignment.state == "released"
    assert assignment.release_reason == "search_assignment_expired"
    assert reservation.bound_count == 0
    assert reservation.released_count == 1


def test_assignment_release_uses_central_claim_lock_order(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    _track_release_lock_order(monkeypatch, order)

    release_search_click_assignment(
        session,
        "assignment-1",
        trigger_key="release:lock-order",
        reason_code="search_assignment_expired",
        now_value=_now(),
    )

    assert order == [
        "DispatchClaimScope",
        "DispatchClaimWindow",
        "DispatchClaimShardAllocation",
        "DispatchClaimReservation",
        "SearchClickOpportunityAssignment",
    ]


def _track_release_lock_order(
    monkeypatch: pytest.MonkeyPatch,
    order: list[str],
) -> None:
    original_scope = search_click_release_locking.locked_release_scope
    original_row = search_click_release_locking.locked_release_row
    original_assignment = search_click_release_locking.locked_assignment

    def track_scope(current, scope_name):
        order.append("DispatchClaimScope")
        return original_scope(current, scope_name)

    def track_row(current, model, row_id):
        order.append(model.__name__)
        return original_row(current, model, row_id)

    def track_assignment(current, assignment_id):
        order.append("SearchClickOpportunityAssignment")
        return original_assignment(current, assignment_id)

    monkeypatch.setattr(
        search_click_release_locking,
        "locked_release_scope",
        track_scope,
    )
    monkeypatch.setattr(
        search_click_release_locking,
        "locked_release_row",
        track_row,
    )
    monkeypatch.setattr(
        search_click_release_locking,
        "locked_assignment",
        track_assignment,
    )


def test_prebound_account_policy_failure_releases_current_unit(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = session.get(Action, "action-1")
    action.status = "claiming"
    monkeypatch.setattr(
        dispatcher,
        "account_capacity_decision",
        lambda *_args, **_kwargs: SimpleNamespace(
            available=False,
            reason="账号全局冷却中",
            defer_until=_now() + timedelta(minutes=5),
        ),
    )

    assert not dispatcher._apply_claim_account_policy(session, action)

    assignment = session.get(SearchClickOpportunityAssignment, "assignment-1")
    reservation = session.get(DispatchClaimReservation, "reservation-1")
    assert action.status == "skipped"
    assert assignment.state == "released"
    assert assignment.release_reason == "search_resource_saturated"
    assert reservation.bound_count == 0
    assert reservation.released_count == 1


def test_invalid_prebound_action_never_falls_back_to_unbound_allocation(
    session: Session,
) -> None:
    action = session.get(Action, "action-1")
    action.result = {
        **action.result,
        "search_click_assignment_id": "missing-assignment",
    }
    plan = plan_prebound_search_claims(session, [action])

    assert not plan.bindings_by_action_id
    assert dispatch_reservations._unbound_actions([action], plan) == []


def test_counter_conflict_keeps_prebound_identity_for_exact_release(
    session: Session,
) -> None:
    action = session.get(Action, "action-1")
    reservation = session.get(DispatchClaimReservation, "reservation-1")
    reservation.bound_count = 0

    plan = plan_prebound_search_claims(session, [action])

    assert action.id in plan.bindings_by_action_id


def test_prebound_integrity_conflict_releases_current_unit(
    session: Session,
) -> None:
    action = session.get(Action, "action-1")
    action.status = "claiming"
    action.claim_owner = "worker-1"
    action.claim_token = "token-1"
    session.commit()
    batch = dispatcher.ActionClaimBatch(
        (action.id,),
        "worker-1",
        "token-1",
        {},
    )

    dispatcher._release_conflicting_action_claim(
        session,
        batch,
        action,
    )

    assignment = session.get(SearchClickOpportunityAssignment, "assignment-1")
    assert action.status == "skipped"
    assert assignment.state == "released"
    assert assignment.release_reason == "search_reservation_cas_abandoned"


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
