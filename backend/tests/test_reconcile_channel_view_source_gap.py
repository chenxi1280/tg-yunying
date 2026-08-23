from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPacingReservation,
    Action,
    AuditLog,
    ChannelMessage,
    ChannelViewDailyMessageTarget,
    ExecutionAttempt,
    OperationTarget,
    SourcePacingAdmission,
    SourcePacingState,
    Task,
    TaskDayLedger,
    Tenant,
    TgAccount,
    ViewFulfillmentObligation,
)
from app.services.task_center.fulfillment_activation import CURRENT_CONTRACT_VERSION
from app.services.task_center.pacing import PACING_CONTRACT_VERSION
from scripts import reconcile_channel_view_source_gap as recovery


pytestmark = pytest.mark.no_postgres
DAY = date(2026, 8, 24)
START = datetime(2026, 8, 24)
DEADLINE = START + timedelta(days=1)
ANCHOR = START + timedelta(minutes=10)


@pytest.fixture
def session(monkeypatch) -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    current = Session(engine)
    _seed(current)
    monkeypatch.setattr(
        recovery.target_guard,
        "attach_target_guards",
        lambda _session, items, lock: [
            {**item, "target_guard": {"mismatches": []}} for item in items
        ],
    )
    monkeypatch.setattr(
        recovery.target_guard,
        "assert_target_guards_unchanged",
        lambda _session, _items: None,
    )
    yield current
    current.close()


def test_apply_rebases_exact_no_gateway_gap_and_source_cursor(session: Session) -> None:
    request = _request()
    manifest = recovery.build_manifest(session, request)

    assert manifest["blockers"] == []
    assert manifest["admission"]["source_gap_seconds"] == 86_400
    assert manifest["corrected_source_gap_seconds"] == 86
    assert manifest["rebased_not_before_at"] == ANCHOR.isoformat()

    result = recovery.apply_recovery(
        session,
        request,
        expected_fingerprint=manifest["fingerprint"],
        actor="codex-online-task-recovery",
        approval_ref="incident-2026-08-24-view-gap",
    )
    session.commit()

    action = session.get(Action, "view-action")
    admission = session.get(SourcePacingAdmission, "view-admission")
    state = session.get(SourcePacingState, "view-state")
    attempt = session.get(ExecutionAttempt, "view-attempt")
    assert action.scheduled_at == ANCHOR
    assert action.result["source_gap_recovery"]["fingerprint"] == manifest["fingerprint"]
    assert admission.source_gap_seconds == 86
    assert admission.call_not_before_at == ANCHOR
    assert state.next_call_not_before_at == ANCHOR + timedelta(seconds=86)
    assert attempt.status == "skipped_before_gateway"
    assert result["audit_count"] == 1
    assert session.scalar(select(AuditLog.id)) is not None


def test_gateway_started_blocks_recovery(session: Session) -> None:
    attempt = session.get(ExecutionAttempt, "view-attempt")
    attempt.gateway_call_started_at = ANCHOR
    session.flush()

    manifest = recovery.build_manifest(session, _request())

    assert "remote_effect_not_proven_absent" in manifest["blockers"]
    with pytest.raises(RuntimeError, match="recovery blocked"):
        recovery.apply_recovery(
            session,
            _request(),
            expected_fingerprint=manifest["fingerprint"],
            actor="codex-online-task-recovery",
            approval_ref="incident-2026-08-24-view-gap",
        )


def _request() -> recovery.RecoveryRequest:
    return recovery.RecoveryRequest(
        task_id="view-task",
        action_id="view-action",
        local_date=DAY,
        rebase_anchor=ANCHOR,
        deployed_sha="a" * 40,
    )


def _seed(session: Session) -> None:
    task, ledger, message = _seed_scope(session)
    target, owner, action = _seed_view_rows(task, ledger, message)
    reservation, state, admission, attempt = _seed_pacing_rows(
        task, ledger, action, owner=owner,
    )
    session.add_all([target, owner, action, reservation, state, admission, attempt])
    session.commit()


def _seed_scope(session: Session) -> tuple[Task, TaskDayLedger, ChannelMessage]:
    task = Task(
        id="view-task",
        tenant_id=1,
        name="view task",
        type="channel_view",
        status="running",
        fulfillment_contract_version=CURRENT_CONTRACT_VERSION,
    )
    ledger = TaskDayLedger(
        id="view-ledger",
        tenant_id=1,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=DAY,
        period_start_at=START,
        deadline_at=DEADLINE,
        day_phase="active",
        planning_anchor_at=START,
    )
    message = ChannelMessage(
        id=101,
        tenant_id=1,
        channel_target_id=10,
        message_id=5001,
        published_at=START - timedelta(hours=1),
    )
    session.add_all([
        Tenant(id=1, name="tenant"),
        TgAccount(id=1, tenant_id=1, display_name="account", phone_masked="***0001", status="在线"),
        OperationTarget(id=10, tenant_id=1, target_type="channel", tg_peer_id="-1009001", title="channel"),
        task,
        ledger,
        message,
    ])
    session.commit()
    return task, ledger, message


def _seed_view_rows(
    task: Task,
    ledger: TaskDayLedger,
    message: ChannelMessage,
) -> tuple[ChannelViewDailyMessageTarget, ViewFulfillmentObligation, Action]:
    return _view_target(task, ledger, message), *_view_owner_action(task, ledger, message)


def _view_target(
    task: Task,
    ledger: TaskDayLedger,
    message: ChannelMessage,
) -> ChannelViewDailyMessageTarget:
    return ChannelViewDailyMessageTarget(
        id="view-target",
        tenant_id=1,
        task_id=task.id,
        task_day_ledger_id=ledger.id,
        target_peer_id="-1009001",
        channel_message_id=message.id,
        target_revision=1,
        daily_target_snapshot=1_000,
        total_target_snapshot=1_000,
        effective_target_snapshot=1_000,
        accrual_anchor_at=START,
        active_until=DEADLINE,
        due_count=1,
        source_state="active",
    )


def _view_owner_action(
    task: Task,
    ledger: TaskDayLedger,
    message: ChannelMessage,
) -> tuple[ViewFulfillmentObligation, Action]:
    owner = ViewFulfillmentObligation(
        id="view-owner",
        tenant_id=1,
        task_day_ledger_id=ledger.id,
        channel_message_id=message.id,
        account_id=1,
        current_action_id="view-action",
        status="pending",
        pacing_contract_version=PACING_CONTRACT_VERSION,
        pacing_plan_hash="a" * 64,
        pacing_slot_ordinal=0,
        pacing_plan_total=1_000,
        pacing_due_at=START + timedelta(seconds=20),
        release_not_before_at=DEADLINE - timedelta(seconds=5),
    )
    action = Action(
        id="view-action",
        tenant_id=1,
        task_id=task.id,
        task_type="channel_view",
        action_type="view_message",
        account_id=1,
        status="pending",
        scheduled_at=DEADLINE - timedelta(seconds=5),
        pacing_contract_version=PACING_CONTRACT_VERSION,
        pacing_plan_hash="a" * 64,
        pacing_slot_key="view-slot",
        pacing_due_at=START + timedelta(seconds=20),
        release_not_before_at=DEADLINE - timedelta(seconds=5),
        effective_claim_at=START + timedelta(minutes=5),
        payload={
            "view_fulfillment_obligation_id": owner.id,
            "execution_date": DAY.isoformat(),
            "channel_id": "-1009001",
            "channel_message_id": message.id,
        },
        result={"error_code": "pacing_source_not_before"},
    )
    return owner, action


def _seed_pacing_rows(
    task: Task,
    ledger: TaskDayLedger,
    action: Action,
    *,
    owner: ViewFulfillmentObligation,
) -> tuple[
    AccountPacingReservation,
    SourcePacingState,
    SourcePacingAdmission,
    ExecutionAttempt,
]:
    reservation, state = _reservation_state(task, action)
    admission, attempt = _admission_attempt(
        task, ledger, action, owner=owner, state=state,
    )
    return reservation, state, admission, attempt


def _reservation_state(
    task: Task,
    action: Action,
) -> tuple[AccountPacingReservation, SourcePacingState]:
    reservation = AccountPacingReservation(
        id="view-reservation",
        tenant_id=1,
        task_id=task.id,
        account_id=1,
        pacing_slot_key="view-slot",
        policy_version="account_soft_pacing_v1",
        due_at=START + timedelta(seconds=20),
        release_not_before_at=START + timedelta(minutes=1),
        effective_claim_at=START + timedelta(minutes=5),
        source_deadline_at=DEADLINE,
        action_id=action.id,
        state="bound",
    )
    state = SourcePacingState(
        id="view-state",
        tenant_id=1,
        pacing_domain="view",
        source_key_hash="b" * 64,
        next_call_not_before_at=DEADLINE + timedelta(days=1),
        last_call_started_at=START - timedelta(seconds=5),
        last_source_gap_seconds=20,
    )
    return reservation, state


def _admission_attempt(
    task: Task,
    ledger: TaskDayLedger,
    action: Action,
    *,
    owner: ViewFulfillmentObligation,
    state: SourcePacingState,
) -> tuple[SourcePacingAdmission, ExecutionAttempt]:
    admission = SourcePacingAdmission(
        id="view-admission",
        admission_key="view-admission-key",
        tenant_id=1,
        task_id=task.id,
        source_pacing_state_id=state.id,
        owner_type=owner.__tablename__,
        owner_id=owner.id,
        action_id=action.id,
        attempt_id="view-attempt",
        pacing_period_key=ledger.id,
        pacing_plan_hash="a" * 64,
        planned_release_at=START + timedelta(minutes=1),
        call_not_before_at=DEADLINE - timedelta(seconds=5),
        source_gap_seconds=86_400,
        state="reserved",
    )
    attempt = ExecutionAttempt(
        id="view-attempt",
        tenant_id=1,
        action_id=action.id,
        account_id=1,
        attempt_no=1,
        status="skipped_before_gateway",
        before_call_at=START + timedelta(minutes=5),
        after_call_at=START + timedelta(minutes=5),
        failure_type="pacing_source_not_before",
    )
    return admission, attempt
