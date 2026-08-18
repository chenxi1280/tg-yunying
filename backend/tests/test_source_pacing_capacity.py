from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPacingReservation,
    Action,
    ChannelMessage,
    ChannelViewDailyMessageTarget,
    ExecutionAttempt,
    FulfillmentRemoteFact,
    OperationTarget,
    SourcePacingAdmission,
    SourcePacingState,
    Task,
    TaskDayLedger,
    Tenant,
    TgAccount,
    ViewFulfillmentObligation,
)
from app.services.task_center.direct_action_claims import (
    claim_fact_first_candidates,
    reconcile_source_pacing_states,
    settle_fact_first_action_before_gateway,
)
from app.services.task_center import dispatcher
from app.services.task_center.pacing import PACING_CONTRACT_VERSION
from app.services.task_center.source_pacing_admission import admit_source_paced_attempt


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 8, 18, 10, 0)
DEADLINE = NOW + timedelta(days=1)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        _seed_base(current)
        yield current


def test_view_source_gap_uses_ledger_aggregate_due_count(session: Session) -> None:
    task, ledger, messages = _seed_view_period(session)
    owner = _view_owner(ledger, messages[0], plan_total=600)
    action = _view_action(task, owner, release_at=NOW)
    session.add_all([owner, action])
    session.flush()
    attempt = _attempt(action, NOW)
    session.add(attempt)
    session.flush()

    assert admit_source_paced_attempt(session, action, attempt, now_value=NOW)
    admission = session.scalar(select(SourcePacingAdmission))

    assert admission is not None
    assert owner.pacing_plan_total == 600
    assert admission.source_gap_seconds == 86


def test_future_action_past_deadline_is_safely_closed_now(session: Session) -> None:
    task, ledger, messages = _seed_view_period(session)
    task.fulfillment_contract_version = "fact_first_v3"
    future = DEADLINE + timedelta(days=2)
    owner = _view_owner(ledger, messages[0], plan_total=600)
    action = _view_action(task, owner, release_at=future)
    action.scheduled_at = future
    owner.current_action_id = action.id
    owner.status = "pending"
    reservation = _account_reservation(task, action, deadline=DEADLINE, future=future)
    state, admission = _future_source_reservation(task, owner, action, future=future)
    session.add_all([owner, action, reservation, state, admission])
    session.commit()

    batch = claim_fact_first_candidates(
        session,
        owner="test-worker",
        limit=10,
        now=NOW,
        lease_seconds=30,
    )
    session.refresh(owner)
    session.refresh(reservation)
    session.refresh(admission)
    session.refresh(state)
    fact = session.scalar(select(FulfillmentRemoteFact).where(
        FulfillmentRemoteFact.action_id == action.id,
    ))

    assert batch.action_ids == ()
    assert action.status == "skipped"
    assert reservation.state == "missed"
    assert admission.state == "cancelled_pre_gateway"
    assert owner.status == "open" and owner.current_action_id is None
    assert fact is not None and fact.fact_kind == "safely_not_executed"
    assert state.next_call_not_before_at == NOW - timedelta(seconds=25)


def test_stale_skipped_action_can_be_reconciled_without_gateway(session: Session) -> None:
    task, ledger, messages = _seed_view_period(session)
    task.fulfillment_contract_version = "fact_first_v3"
    future = DEADLINE + timedelta(days=2)
    owner = _view_owner(ledger, messages[0], plan_total=600)
    action = _view_action(task, owner, release_at=future)
    action.status = "skipped"
    action.executed_at = DEADLINE
    action.result = {"error_code": "stale_channel_daily_action"}
    owner.current_action_id = action.id
    owner.status = "pending"
    reservation = _account_reservation(task, action, deadline=DEADLINE, future=future)
    state, admission = _future_source_reservation(task, owner, action, future=future)
    session.add_all([owner, action, reservation, state, admission])
    session.commit()

    settle_fact_first_action_before_gateway(
        session,
        action,
        now=DEADLINE + timedelta(minutes=1),
        reason_code="stale_channel_daily_action",
        detail="旧日浏览 Action 在 Gateway 前误终结，补齐安全事实",
    )
    session.commit()
    fact = session.scalar(select(FulfillmentRemoteFact).where(
        FulfillmentRemoteFact.action_id == action.id,
    ))
    attempt = session.scalar(select(ExecutionAttempt).where(
        ExecutionAttempt.action_id == action.id,
    ))

    assert action.status == "skipped"
    assert action.result["pre_gateway_safe_settlement"]["reason_code"] == (
        "stale_channel_daily_action"
    )
    assert attempt is not None and attempt.gateway_call_started_at is None
    assert fact is not None and fact.fact_kind == "safely_not_executed"
    assert reservation.state == "missed"
    assert admission.state == "cancelled_pre_gateway"
    assert owner.status == "open" and owner.current_action_id is None


def test_reconciled_cursor_includes_remaining_reserved_gap(session: Session) -> None:
    task, ledger, messages = _seed_view_period(session)
    task.fulfillment_contract_version = "fact_first_v3"
    future = DEADLINE + timedelta(days=2)
    owner = _view_owner(ledger, messages[0], plan_total=600)
    action = _view_action(task, owner, release_at=future)
    owner.current_action_id = action.id
    owner.status = "pending"
    reservation = _account_reservation(task, action, deadline=DEADLINE, future=future)
    state, admission = _future_source_reservation(task, owner, action, future=future)
    remaining = _remaining_source_reservation(task, state, future=future)
    session.add_all([owner, action, reservation, state, admission, remaining])
    session.flush()

    state_ids = settle_fact_first_action_before_gateway(
        session,
        action,
        now=DEADLINE + timedelta(minutes=1),
        reason_code="stale_channel_daily_action",
        detail="安全结案并保留剩余来源槽位",
    )
    reconcile_source_pacing_states(session, state_ids)

    assert admission.state == "cancelled_pre_gateway"
    assert state.next_call_not_before_at == (
        remaining.call_not_before_at
        + timedelta(seconds=remaining.source_gap_seconds)
    )


def test_account_abandonment_safely_settles_pending_view_sibling(
    session: Session,
) -> None:
    task, ledger, messages = _seed_view_period(session)
    task.fulfillment_contract_version = "fact_first_v3"
    future = DEADLINE + timedelta(days=2)
    owner = _view_owner(ledger, messages[0], plan_total=600)
    sibling = _view_action(task, owner, release_at=future)
    owner.current_action_id = sibling.id
    owner.status = "pending"
    reservation = _account_reservation(task, sibling, deadline=DEADLINE, future=future)
    state, admission = _future_source_reservation(task, owner, sibling, future=future)
    failed = _view_action(task, owner, release_at=NOW)
    failed.id = "view-capacity-failed-action"
    failed.status = "failed"
    session.add_all([owner, sibling, reservation, state, admission, failed])
    session.flush()

    dispatcher._abandon_pending_account_actions(session, failed)

    fact = session.scalar(select(FulfillmentRemoteFact).where(
        FulfillmentRemoteFact.action_id == sibling.id,
    ))
    assert sibling.status == "skipped"
    assert sibling.result["error_code"] == "account_task_abandoned"
    assert fact is not None and fact.fact_kind == "safely_not_executed"
    assert reservation.state == "missed"
    assert admission.state == "cancelled_pre_gateway"
    assert owner.status == "open" and owner.current_action_id is None
    assert state.next_call_not_before_at == NOW - timedelta(seconds=25)


def _seed_base(session: Session) -> None:
    session.add(Tenant(id=1, name="tenant"))
    session.add(TgAccount(
        id=1,
        tenant_id=1,
        display_name="account",
        phone_masked="***0001",
        status="在线",
    ))
    session.add(OperationTarget(
        id=10,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-1009001",
        title="channel",
    ))
    session.commit()


def _seed_view_period(
    session: Session,
) -> tuple[Task, TaskDayLedger, tuple[ChannelMessage, ChannelMessage]]:
    task = Task(
        id="view-capacity-task",
        tenant_id=1,
        name="view capacity",
        type="channel_view",
        status="running",
    )
    ledger = TaskDayLedger(
        id="view-capacity-ledger",
        tenant_id=1,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 8, 18),
        period_start_at=NOW,
        deadline_at=DEADLINE,
        day_phase="active",
        planning_anchor_at=NOW,
    )
    messages = (
        ChannelMessage(id=101, tenant_id=1, channel_target_id=10, message_id=5001),
        ChannelMessage(id=102, tenant_id=1, channel_target_id=10, message_id=5002),
    )
    session.add_all([task, ledger, *messages])
    session.flush()
    session.add_all([
        _view_target(task, ledger, messages[0], due_count=600),
        _view_target(task, ledger, messages[1], due_count=400),
    ])
    session.flush()
    return task, ledger, messages


def _view_target(
    task: Task,
    ledger: TaskDayLedger,
    message: ChannelMessage,
    *,
    due_count: int,
) -> ChannelViewDailyMessageTarget:
    return ChannelViewDailyMessageTarget(
        tenant_id=1,
        task_id=task.id,
        task_day_ledger_id=ledger.id,
        target_peer_id="-1009001",
        channel_message_id=message.id,
        target_revision=1,
        daily_target_snapshot=due_count,
        total_target_snapshot=due_count,
        effective_target_snapshot=due_count,
        accrual_anchor_at=NOW,
        active_until=DEADLINE,
        due_count=due_count,
        source_state="active",
    )


def _view_owner(
    ledger: TaskDayLedger,
    message: ChannelMessage,
    *,
    plan_total: int,
) -> ViewFulfillmentObligation:
    return ViewFulfillmentObligation(
        id="view-capacity-owner",
        tenant_id=1,
        task_day_ledger_id=ledger.id,
        channel_message_id=message.id,
        account_id=1,
        status="open",
        pacing_contract_version=PACING_CONTRACT_VERSION,
        pacing_plan_hash="a" * 64,
        pacing_slot_ordinal=0,
        pacing_plan_total=plan_total,
        pacing_due_at=NOW,
        release_not_before_at=NOW,
    )


def _view_action(
    task: Task,
    owner: ViewFulfillmentObligation,
    *,
    release_at: datetime,
) -> Action:
    return Action(
        id="view-capacity-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="view_message",
        account_id=1,
        status="pending",
        scheduled_at=release_at,
        pacing_contract_version=PACING_CONTRACT_VERSION,
        pacing_plan_hash="a" * 64,
        pacing_slot_key="view-capacity-slot",
        pacing_due_at=NOW,
        release_not_before_at=release_at,
        effective_claim_at=NOW,
        payload={
            "view_fulfillment_obligation_id": owner.id,
            "task_day_ledger_id": owner.task_day_ledger_id,
            "channel_message_id": owner.channel_message_id,
            "channel_id": "-1009001",
        },
    )


def _attempt(action: Action, now: datetime) -> ExecutionAttempt:
    return ExecutionAttempt(
        tenant_id=1,
        action_id=action.id,
        account_id=1,
        attempt_no=1,
        status="before_call",
        before_call_at=now,
    )


def _account_reservation(
    task: Task,
    action: Action,
    *,
    deadline: datetime,
    future: datetime,
) -> AccountPacingReservation:
    return AccountPacingReservation(
        tenant_id=1,
        task_id=task.id,
        account_id=1,
        pacing_slot_key=action.pacing_slot_key,
        policy_version="account_soft_pacing_v1",
        due_at=NOW,
        release_not_before_at=future,
        effective_claim_at=future,
        source_deadline_at=deadline,
        action_id=action.id,
        state="bound",
    )


def _future_source_reservation(
    task: Task,
    owner: ViewFulfillmentObligation,
    action: Action,
    *,
    future: datetime,
) -> tuple[SourcePacingState, SourcePacingAdmission]:
    state = SourcePacingState(
        id="view-capacity-state",
        tenant_id=1,
        pacing_domain="view",
        source_key_hash="b" * 64,
        next_call_not_before_at=future,
        last_call_started_at=NOW - timedelta(seconds=30),
        last_source_gap_seconds=5,
    )
    admission = SourcePacingAdmission(
        id="view-capacity-admission",
        admission_key="view-capacity-key",
        tenant_id=1,
        task_id=task.id,
        source_pacing_state_id=state.id,
        owner_type=owner.__tablename__,
        owner_id=owner.id,
        action_id=action.id,
        pacing_period_key=owner.task_day_ledger_id,
        pacing_plan_hash="a" * 64,
        planned_release_at=NOW,
        call_not_before_at=future,
        source_gap_seconds=87,
        state="reserved",
    )
    return state, admission


def _remaining_source_reservation(
    task: Task,
    state: SourcePacingState,
    *,
    future: datetime,
) -> SourcePacingAdmission:
    return SourcePacingAdmission(
        id="view-capacity-remaining-admission",
        admission_key="view-capacity-remaining-key",
        tenant_id=1,
        task_id=task.id,
        source_pacing_state_id=state.id,
        owner_type="view_fulfillment_obligations",
        owner_id="remaining-owner",
        action_id=None,
        pacing_period_key="remaining-period",
        pacing_plan_hash="c" * 64,
        planned_release_at=NOW,
        call_not_before_at=future + timedelta(hours=1),
        source_gap_seconds=20,
        state="reserved",
    )
