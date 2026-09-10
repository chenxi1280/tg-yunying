from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPacingReservation,
    Action,
    FulfillmentObligationProjection,
    FulfillmentRemoteFact,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services.task_center.ai_generation_parallel import (
    _candidate_statement,
    _claim_one,
)
from app.services.task_center.direct_action_claims import (
    claim_fact_first_candidates,
)
from app.timezone import BEIJING_TZ
from scripts.abandon_channel_historical_backlog import (
    abandon_channel_historical_backlog,
)

pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 9, 8, 10, 0, tzinfo=BEIJING_TZ)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all([
            Tenant(id=1, name="test-tenant"),
            TgAccount(
                id=101,
                tenant_id=1,
                display_name="account-101",
                phone_masked="***",
                status="active",
            ),
            TgGroup(
                id=201,
                tenant_id=1,
                title="test-group",
                tg_peer_id="-100201",
            ),
        ])
        s.commit()
        yield s


def _seed_task_and_action(
    session: Session,
    *,
    task_id: str = "ai-task-1",
    action_id: str = "action-expired-1",
    scheduled_at: datetime,
    deadline_at: datetime,
    message_text: str = "",
    generation_status: str = "pending",
) -> tuple[Task, Action, AccountPacingReservation]:
    task = Task(
        id=task_id,
        tenant_id=1,
        name="AI Chat Task",
        type="group_ai_chat",
        status="running",
        fulfillment_contract_version="fact_first_v3",
        account_config={"mode": "all"},
        type_config={
            "daily_message_target": 10,
            "topic_participation_rate": 0.30,
        },
    )
    ledger = TaskDayLedger(
        id=f"ledger-{task_id}",
        tenant_id=1,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 9, 8),
        period_start_at=NOW - timedelta(days=1),
        deadline_at=deadline_at,
        day_phase="active",
        planning_anchor_at=NOW - timedelta(days=1),
    )
    slot = TaskGroupDailyMessageSlot(
        id=f"slot-{action_id}",
        tenant_id=1,
        task_id=task.id,
        task_day_ledger_id=ledger.id,
        target_operation_target_id=201,
        slot_kind="quantity",
        slot_ordinal=1,
    )
    action = Action(
        id=action_id,
        tenant_id=1,
        task_id=task.id,
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=101,
        status="pending",
        scheduled_at=scheduled_at,
        release_not_before_at=scheduled_at,
        pacing_slot_key=f"pacing-{action_id}",
        primary_quantity_slot_id=slot.id,
        payload={
            "message_text": message_text,
            "ai_generation_status": generation_status,
            "primary_quantity_slot_id": slot.id,
        },
        result={},
    )
    reservation = AccountPacingReservation(
        tenant_id=1,
        task_id=task.id,
        account_id=101,
        pacing_slot_key=f"pacing-{action_id}",
        state="bound",
        action_id=action.id,
        action_class="send_message",
        due_at=scheduled_at,
        release_not_before_at=scheduled_at,
        effective_claim_at=scheduled_at,
        source_deadline_at=deadline_at,
        policy_version="account_behavior_session_pacing_v1",
        version=1,
    )
    projection = FulfillmentObligationProjection(
        id=f"proj-{action_id}",
        tenant_id=1,
        task_id=task.id,
        task_day_ledger_id=ledger.id,
        task_lifecycle_epoch=1,
        obligation_type="task_group_daily_message_slot",
        obligation_id=slot.id,
        work_lane="group_ai_chat",
        state="open",
        active_action_id=action.id,
        materialization_version=1,
        version=1,
    )
    for row in (task, ledger, slot, action, reservation, projection):
        session.add(row)
        session.flush()
    session.commit()
    return task, action, reservation


def test_ungenerated_expired_ai_action_skipped_by_dispatcher(session: Session) -> None:
    scheduled_at = NOW - timedelta(hours=2)
    deadline_at = NOW - timedelta(hours=1)
    _, action, reservation = _seed_task_and_action(
        session,
        scheduled_at=scheduled_at,
        deadline_at=deadline_at,
        message_text="",
        generation_status="pending",
    )

    batch = claim_fact_first_candidates(
        session,
        owner="test-dispatcher",
        limit=10,
        now=NOW,
        lease_seconds=30,
    )

    session.refresh(action)
    session.refresh(reservation)

    assert action.id not in batch.action_ids
    assert action.status == "skipped"
    assert action.result.get("error_code") == "pacing_claim_deadline_exceeded"
    assert reservation.state == "missed"

    fact = session.scalar(
        select(FulfillmentRemoteFact).where(FulfillmentRemoteFact.action_id == action.id)
    )
    assert fact is not None
    assert fact.fact_kind == "safely_not_executed"


def test_ai_generation_parallel_candidate_query_excludes_deadline_expired(
    session: Session,
) -> None:
    scheduled_at = NOW - timedelta(hours=2)
    deadline_at = NOW - timedelta(hours=1)
    _, action, _ = _seed_task_and_action(
        session,
        scheduled_at=scheduled_at,
        deadline_at=deadline_at,
        message_text="",
        generation_status="pending",
    )

    stmt = _candidate_statement(10, now=NOW)
    candidates = list(session.scalars(stmt))
    candidate_ids = [c.id for c in candidates]

    assert action.id not in candidate_ids


def test_ai_generation_parallel_claim_one_defensively_skips_expired(
    session: Session,
) -> None:
    scheduled_at = NOW - timedelta(hours=2)
    deadline_at = NOW - timedelta(hours=1)
    _, action, reservation = _seed_task_and_action(
        session,
        scheduled_at=scheduled_at,
        deadline_at=deadline_at,
        message_text="",
        generation_status="pending",
    )

    claim = _claim_one(session, action, owner="test-generator", now=NOW)
    assert claim is None

    session.refresh(action)
    session.refresh(reservation)

    assert action.status == "skipped"
    assert action.result.get("error_code") == "pacing_claim_deadline_exceeded"
    assert reservation.state == "missed"


def test_abandon_channel_historical_backlog_supports_group_ai_chat(
    session: Session,
) -> None:
    cutoff = NOW - timedelta(minutes=30)
    _, action, _ = _seed_task_and_action(
        session,
        scheduled_at=NOW - timedelta(hours=2),
        deadline_at=NOW - timedelta(hours=1),
        message_text="",
        generation_status="pending",
    )

    preview = abandon_channel_historical_backlog(
        session,
        cutoff=cutoff,
        apply=False,
        task_types={"group_ai_chat"},
    )
    assert preview["candidate_count"] == 1
    session.refresh(action)
    assert action.status == "pending"

    settled = abandon_channel_historical_backlog(
        session,
        cutoff=cutoff,
        apply=True,
        task_types={"group_ai_chat"},
    )
    assert settled["settled_count"] == 1
    session.refresh(action)
    assert action.status == "skipped"
    assert action.result.get("error_code") == "pacing_claim_deadline_exceeded"
