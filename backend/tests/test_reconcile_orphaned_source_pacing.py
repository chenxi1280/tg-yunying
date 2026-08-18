from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

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
from app.services.task_center.pacing import PACING_CONTRACT_VERSION


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".github/scripts/reconcile_orphaned_source_pacing.py"
)
pytestmark = pytest.mark.no_postgres
TERMINAL_DATE = date(2026, 8, 18)
CURRENT_DATE = date(2026, 8, 19)
ANCHOR = datetime(2026, 8, 19, 2, 0)
DEADLINE = datetime(2026, 8, 20)


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "reconcile_orphaned_source_pacing",
        SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        _seed(current)
        yield current


def test_manifest_classifies_terminal_and_rebases_current(session: Session) -> None:
    script = _load_script()

    manifest = script.build_manifest(session, _options(script))

    assert manifest["blocked"] is False
    assert manifest["terminal_count"] == 1
    assert manifest["terminal"][0]["mode"] == "safe_settlement"
    assert manifest["current_count"] == 1
    assert manifest["current"][0]["rebased_not_before_at"] == ANCHOR.isoformat()
    assert manifest["current"][0]["target_guard"]["mismatches"] == []
    assert manifest["unclassified_reserved_admission_ids"] == []


def test_manifest_releases_existing_safe_fact_without_duplicate(
    session: Session,
) -> None:
    session.add(_safe_fact())
    session.flush()
    script = _load_script()

    manifest = script.build_manifest(session, _options(script))

    assert manifest["blocked"] is False
    assert manifest["terminal"][0]["mode"] == "release_safe_fact"


def test_apply_closes_orphan_and_rebases_current_timeline(session: Session) -> None:
    script = _load_script()
    options = _options(script)
    manifest = script.build_manifest(session, options)
    state_hash = script.manifest_hash(manifest)

    script._apply_locked_manifest(
        session,
        options,
        manifest,
        state_hash=state_hash,
    )
    session.commit()

    terminal = session.get(Action, "terminal-action")
    current = session.get(Action, "current-action")
    terminal_reservation = session.get(AccountPacingReservation, "terminal-reservation")
    current_reservation = session.get(AccountPacingReservation, "current-reservation")
    terminal_admission = session.get(SourcePacingAdmission, "terminal-admission")
    current_admission = session.get(SourcePacingAdmission, "current-admission")
    state = session.get(SourcePacingState, "shared-source-state")
    fact = session.scalar(select(FulfillmentRemoteFact).where(
        FulfillmentRemoteFact.action_id == terminal.id,
    ))
    post_guards = script.target_guard.attach_target_guards(
        session,
        [{"action_id": item["action_id"]} for item in [
            *manifest["terminal"],
            *manifest["current"],
        ]],
        lock=False,
    )

    assert fact is not None and fact.fact_kind == "safely_not_executed"
    assert terminal_reservation.state == "missed"
    assert terminal_admission.state == "cancelled_pre_gateway"
    assert current.scheduled_at == ANCHOR
    assert current.release_not_before_at == ANCHOR
    assert current_reservation.effective_claim_at == ANCHOR
    assert current_admission.call_not_before_at == ANCHOR
    assert state.next_call_not_before_at == ANCHOR + timedelta(seconds=20)
    assert session.scalar(select(AuditLog)) is not None
    assert [item["target_guard"] for item in post_guards] == [
        item["target_guard"] for item in [*manifest["terminal"], *manifest["current"]]
    ]


def test_apply_rejects_send_target_and_quantity_drift(session: Session) -> None:
    script = _load_script()
    options = _options(script)
    manifest = script.build_manifest(session, options)
    target = session.scalar(select(ChannelViewDailyMessageTarget).where(
        ChannelViewDailyMessageTarget.task_day_ledger_id == "current-ledger",
    ))
    operation_target = session.get(OperationTarget, 10)
    message = session.get(ChannelMessage, 102)
    owner = session.get(ViewFulfillmentObligation, "current-owner")
    mutations = (
        (target, "due_count", 51),
        (target, "effective_target_snapshot", 101),
        (target, "daily_target_snapshot", 101),
        (target, "total_target_snapshot", 301),
        (target, "target_revision", 2),
        (target, "source_state", "expired"),
        (operation_target, "tg_peer_id", "-100999"),
        (operation_target, "reference_revision", 2),
        (message, "message_id", 9999),
        (owner, "account_id", 2),
    )
    for model, field, changed in mutations:
        original = getattr(model, field)
        setattr(model, field, changed)
        session.flush()
        _assert_apply_drift(script, session, options, manifest)
        setattr(model, field, original)
        session.flush()

    assert session.get(Action, "current-action").scheduled_at == DEADLINE + timedelta(hours=5)


def _assert_apply_drift(script, session, options, manifest) -> None:
    with pytest.raises(RuntimeError, match="reconciliation drifted"):
        script._apply_locked_manifest(
            session,
            options,
            manifest,
            state_hash=script.manifest_hash(manifest),
        )


def test_manifest_blocks_payload_send_target_mismatch(session: Session) -> None:
    action = session.get(Action, "current-action")
    action.payload = {**action.payload, "channel_id": "-100999"}
    session.flush()
    script = _load_script()

    manifest = script.build_manifest(session, _options(script))

    assert manifest["blocked"] is True
    assert manifest["current"][0]["target_guard"]["mismatches"] == ["target_peer"]


def _options(script):
    return script.ReconcileOptions(
        task_ids=("view-recovery-task",),
        terminal_date=TERMINAL_DATE,
        current_date=CURRENT_DATE,
        rebase_anchor=ANCHOR,
        deployed_sha="a" * 40,
        actor="codex-production-recovery",
        approval_ref="intake-2026-08-17-planner-pacing-memory-001",
    )


def _seed(session: Session) -> None:
    session.add(Tenant(id=1, name="tenant"))
    session.add(TgAccount(
        id=1,
        tenant_id=1,
        display_name="account",
        phone_masked="***0001",
        status="在线",
    ))
    target = OperationTarget(
        id=10,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-10010",
        title="channel",
    )
    task = Task(
        id="view-recovery-task",
        tenant_id=1,
        name="view recovery",
        type="channel_view",
        status="running",
        fulfillment_contract_version="fact_first_v3",
    )
    session.add_all([target, task])
    session.flush()
    old_ledger = _ledger("old-ledger", task, TERMINAL_DATE)
    current_ledger = _ledger("current-ledger", task, CURRENT_DATE)
    messages = (
        ChannelMessage(id=101, tenant_id=1, channel_target_id=10, message_id=1001),
        ChannelMessage(id=102, tenant_id=1, channel_target_id=10, message_id=1002),
    )
    session.add_all([old_ledger, current_ledger, *messages])
    session.flush()
    session.add_all([
        _target_row("old-target", task, old_ledger, messages[0]),
        _target_row("current-target", task, current_ledger, messages[1]),
    ])
    state = SourcePacingState(
        id="shared-source-state",
        tenant_id=1,
        pacing_domain="view",
        source_key_hash="b" * 64,
        next_call_not_before_at=DEADLINE + timedelta(days=2),
        last_call_started_at=ANCHOR - timedelta(minutes=10),
        last_source_gap_seconds=87,
    )
    session.add(state)
    _seed_terminal(session, task, old_ledger, messages[0], state)
    _seed_current(session, task, current_ledger, messages[1], state)
    session.commit()


def _ledger(ledger_id: str, task: Task, local_date: date) -> TaskDayLedger:
    start = datetime.combine(local_date, datetime.min.time())
    return TaskDayLedger(
        id=ledger_id,
        tenant_id=1,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=local_date,
        period_start_at=start,
        deadline_at=start + timedelta(days=1),
        day_phase="full_day",
        planning_anchor_at=start,
    )


def _seed_terminal(session, task, ledger, message, state) -> None:
    owner = _owner("terminal-owner", ledger, message, current_action_id=None)
    action = _action(
        "terminal-action",
        task,
        owner,
        execution_date=TERMINAL_DATE,
        status="skipped",
        scheduled_at=DEADLINE + timedelta(days=2),
        result={"error_code": "account_task_abandoned"},
    )
    session.add_all([owner, action])
    session.flush()
    session.add_all([
        _reservation("terminal-reservation", task, action, ledger.deadline_at),
        _admission("terminal-admission", task, owner, action, state, 87),
    ])


def _seed_current(session, task, ledger, message, state) -> None:
    owner = _owner("current-owner", ledger, message, current_action_id="current-action")
    action = _action(
        "current-action",
        task,
        owner,
        execution_date=CURRENT_DATE,
        status="pending",
        scheduled_at=DEADLINE + timedelta(hours=5),
        result={"error_code": "pacing_source_not_before"},
    )
    session.add_all([owner, action])
    session.flush()
    session.add_all([
        _reservation("current-reservation", task, action, ledger.deadline_at),
        _admission("current-admission", task, owner, action, state, 20),
    ])


def _owner(owner_id, ledger, message, *, current_action_id):
    return ViewFulfillmentObligation(
        id=owner_id,
        tenant_id=1,
        task_day_ledger_id=ledger.id,
        channel_message_id=message.id,
        account_id=1,
        status="pending" if current_action_id else "open",
        current_action_id=current_action_id,
        pacing_contract_version=PACING_CONTRACT_VERSION,
        pacing_plan_hash="a" * 64,
        pacing_slot_ordinal=1,
        pacing_plan_total=100,
        pacing_due_at=ANCHOR - timedelta(hours=1),
        release_not_before_at=DEADLINE + timedelta(days=2),
    )


def _target_row(target_id, task, ledger, message):
    return ChannelViewDailyMessageTarget(
        id=target_id,
        tenant_id=1,
        task_id=task.id,
        task_day_ledger_id=ledger.id,
        target_peer_id="-10010",
        channel_message_id=message.id,
        target_revision=1,
        daily_target_snapshot=100,
        total_target_snapshot=300,
        effective_target_snapshot=100,
        accrual_anchor_at=ledger.period_start_at,
        active_until=ledger.deadline_at,
        due_count=50,
        source_state="active",
    )


def _action(action_id, task, owner, *, execution_date, status, scheduled_at, result):
    return Action(
        id=action_id,
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="view_message",
        account_id=1,
        status=status,
        scheduled_at=scheduled_at,
        pacing_contract_version=PACING_CONTRACT_VERSION,
        pacing_plan_hash="a" * 64,
        pacing_slot_key=f"slot:{action_id}",
        pacing_due_at=ANCHOR - timedelta(hours=1),
        release_not_before_at=scheduled_at,
        effective_claim_at=scheduled_at,
        result=result,
        payload={
            "execution_date": execution_date.isoformat(),
            "task_day_ledger_id": owner.task_day_ledger_id,
            "view_fulfillment_obligation_id": owner.id,
            "channel_message_id": owner.channel_message_id,
            "channel_id": "-10010",
            "channel_target_id": 10,
            "target_reference_revision": 1,
            "target_reference_snapshot": {"tg_peer_id": "-10010"},
            "message_id": 900 + owner.channel_message_id,
            "daily_view_target": 100,
            "total_view_target": 300,
        },
    )


def _reservation(reservation_id, task, action, deadline):
    return AccountPacingReservation(
        id=reservation_id,
        tenant_id=1,
        task_id=task.id,
        account_id=1,
        pacing_slot_key=action.pacing_slot_key,
        policy_version="account_soft_pacing_v1",
        due_at=ANCHOR - timedelta(hours=1),
        release_not_before_at=action.scheduled_at,
        effective_claim_at=action.scheduled_at,
        source_deadline_at=deadline,
        action_id=action.id,
        state="bound",
    )


def _admission(admission_id, task, owner, action, state, gap_seconds):
    return SourcePacingAdmission(
        id=admission_id,
        admission_key=f"key:{admission_id}",
        tenant_id=1,
        task_id=task.id,
        source_pacing_state_id=state.id,
        owner_type=owner.__tablename__,
        owner_id=owner.id,
        action_id=action.id,
        pacing_period_key=owner.task_day_ledger_id,
        pacing_plan_hash="a" * 64,
        planned_release_at=ANCHOR - timedelta(hours=1),
        call_not_before_at=action.scheduled_at,
        source_gap_seconds=gap_seconds,
        state="reserved",
    )


def _safe_fact() -> FulfillmentRemoteFact:
    return FulfillmentRemoteFact(
        tenant_id=1,
        task_type="channel_view",
        task_id="view-recovery-task",
        task_day_ledger_id="old-ledger",
        obligation_type="view",
        obligation_id="terminal-owner",
        action_id="terminal-action",
        attempt_id="safe-attempt",
        mutation_kind="view_message",
        remote_mutation_key_hash="c" * 64,
        gateway_request_hash="d" * 64,
        fact_kind="safely_not_executed",
        fact_identity_hash="e" * 64,
        outcome={"remote_mutation_started": False},
        observed_at=ANCHOR,
    )
