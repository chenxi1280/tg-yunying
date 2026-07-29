from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    DispatchClaimReservation,
    SearchClickFulfillmentObligation,
    SearchClickOpportunityAssignment,
)
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.prebound_search_claim import (
    prebound_search_epoch_action_ids,
)
from search_click_assignment_test_support import seed_assignment


def test_claim_capacity_excludes_same_epoch_prebound_cohort() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed_assignment(session)
        _add_second_assignment(session)
        first = session.get(Action, "action-1")

        excluded = dispatcher._capacity_excluded_action_ids(
            session,
            first,
            first.account_id,
        )

        assert excluded == {"action-1", "action-2"}
        assert prebound_search_epoch_action_ids(
            session,
            first,
            first.account_id,
        ) == {"action-1", "action-2"}


def _add_second_assignment(session: Session) -> None:
    now_value = _now()
    session.add(SearchClickFulfillmentObligation(
        id="obligation-2",
        tenant_id=1,
        task_day_ledger_id="ledger-1",
        target_id=1,
        click_obligation_ordinal=2,
        status="action_bound",
        source_action_id="action-2",
    ))
    action = Action(
        id="action-2",
        tenant_id=1,
        task_id="task-1",
        task_type="search_click",
        action_type="search_join",
        account_id=1,
        status="pending",
        scheduled_at=now_value,
        payload={"search_click_assignment_id": "assignment-2"},
        result={
            "dispatch_prebound": True,
            "search_click_assignment_id": "assignment-2",
        },
    )
    session.add(action)
    session.add(SearchClickOpportunityAssignment(
        id="assignment-2",
        tenant_id=1,
        task_id="task-1",
        task_day_ledger_id="ledger-1",
        obligation_id="obligation-2",
        search_click_assignment_epoch_id="epoch-1",
        dispatch_claim_reservation_id="reservation-1",
        fulfillment_lane_claim_ordinal=2,
        account_id=1,
        authorization_id=1,
        keyword_hash="e" * 64,
        proxy_route_id="proxy-1",
        protocol_sample_version="v1",
        resource_snapshot_hash="f" * 64,
        action_id=action.id,
        state="action_bound",
    ))
    reservation = session.get(DispatchClaimReservation, "reservation-1")
    reservation.reserved_claims = 2
    reservation.bound_count = 2
    session.commit()
