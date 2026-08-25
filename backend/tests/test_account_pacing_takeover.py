from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPacingReservation,
    Action,
    ExecutionAttempt,
    FulfillmentRemoteFact,
    Task,
    Tenant,
    TgAccount,
)
from app.services.task_center import account_pacing_guard
from app.services.task_center.account_pacing_guard import (
    release_safe_task_account_pacing_reservations,
    reserve_account_pacing,
)


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 8, 25, 20, 0)
TENANT_ID = 880_025
ACCOUNT_ID = 880_026
TASK_ID = "account-pacing-takeover"


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add_all(
            [
                Tenant(id=TENANT_ID, name="tenant"),
                TgAccount(
                    id=ACCOUNT_ID,
                    tenant_id=TENANT_ID,
                    display_name="account",
                    phone_masked="***0026",
                    status="在线",
                ),
                Task(
                    id=TASK_ID,
                    tenant_id=TENANT_ID,
                    name="AI group canary",
                    type="group_ai_chat",
                    status="paused",
                ),
            ]
        )
        current.commit()
        yield current


def _failed_action(action_id: str, slot_key: str) -> Action:
    stale_at = NOW - timedelta(hours=2)
    return Action(
        id=action_id,
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=ACCOUNT_ID,
        status="failed",
        scheduled_at=stale_at,
        pacing_slot_key=slot_key,
        pacing_due_at=stale_at,
        release_not_before_at=stale_at,
        result={},
    )


def _bound_reservation(action: Action) -> AccountPacingReservation:
    stale_at = NOW - timedelta(hours=2)
    return AccountPacingReservation(
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        account_id=ACCOUNT_ID,
        pacing_slot_key=str(action.pacing_slot_key),
        policy_version="account_soft_pacing_v1",
        due_at=stale_at,
        release_not_before_at=stale_at,
        effective_claim_at=stale_at,
        action_id=action.id,
        state="bound",
    )


def test_safe_failed_reservation_is_rearmed_from_new_release(
    session: Session,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        account_pacing_guard,
        "get_settings",
        lambda: SimpleNamespace(account_soft_pacing_min_gap_seconds=20),
    )
    action = _failed_action("safe-failed", "ai:safe-slot")
    reservation = _bound_reservation(action)
    session.add_all([action, reservation])
    session.flush()

    task = session.get(Task, TASK_ID)
    assert release_safe_task_account_pacing_reservations(session, task) == 1
    assert reservation.action_id is None
    assert reservation.state == "reserved"
    assert release_safe_task_account_pacing_reservations(session, task) == 0

    due_at = NOW + timedelta(minutes=1)
    release_at = NOW + timedelta(minutes=5)
    rearmed = reserve_account_pacing(
        session,
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        account_id=ACCOUNT_ID,
        slot_key="ai:safe-slot",
        due_at=due_at,
        release_not_before_at=release_at,
        deadline_at=NOW + timedelta(hours=1),
    )

    assert rearmed.id == reservation.id
    assert rearmed.due_at == due_at
    assert rearmed.release_not_before_at == release_at
    assert rearmed.effective_claim_at == release_at


def test_remote_bound_reservations_remain_immutable(session: Session) -> None:
    gateway_action = _failed_action("gateway-started", "ai:gateway-slot")
    fact_action = _failed_action("typed-fact", "ai:fact-slot")
    reservations = [
        _bound_reservation(gateway_action),
        _bound_reservation(fact_action),
    ]
    session.add_all([gateway_action, fact_action, *reservations])
    session.flush()
    session.add_all(
        [
            ExecutionAttempt(
                tenant_id=TENANT_ID,
                action_id=gateway_action.id,
                account_id=ACCOUNT_ID,
                gateway_call_started_at=NOW,
            ),
            FulfillmentRemoteFact(
                tenant_id=TENANT_ID,
                task_type="group_ai_chat",
                task_id=TASK_ID,
                obligation_type="quantity_slot",
                obligation_id="typed-fact-slot",
                action_id=fact_action.id,
                attempt_id="typed-fact-attempt",
                mutation_kind="send_message",
                remote_mutation_key_hash="a" * 64,
                gateway_request_hash="b" * 64,
                fact_kind="remote_message_observed",
                fact_identity_hash="c" * 64,
                outcome={"remote_message_id": "remote-1"},
            ),
        ]
    )
    session.flush()

    task = session.get(Task, TASK_ID)
    assert release_safe_task_account_pacing_reservations(session, task) == 0
    assert [row.action_id for row in reservations] == [
        gateway_action.id,
        fact_action.id,
    ]
