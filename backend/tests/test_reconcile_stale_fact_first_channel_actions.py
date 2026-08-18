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
    FulfillmentRemoteFact,
    OperationTarget,
    Task,
    TaskDayLedger,
    Tenant,
    TgAccount,
    ViewFulfillmentObligation,
)


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".github/scripts/reconcile_stale_fact_first_channel_actions.py"
)
pytestmark = pytest.mark.no_postgres
EXECUTION_DATE = date(2026, 8, 18)
DEPLOYED_SHA = "a" * 40


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "reconcile_stale_fact_first_channel_actions",
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


def test_manifest_selects_exact_no_gateway_stale_action(session: Session) -> None:
    script = _load_script()

    manifest = script.build_manifest(session, _options(script))

    assert manifest["candidate_count"] == 1
    assert manifest["scope"] == {
        "stale_count": 1,
        "existing_fact_count": 0,
        "gateway_started_count": 0,
        "no_fact_no_gateway_count": 1,
        "blocked_no_fact_no_gateway_count": 0,
    }
    assert manifest["candidates"][0]["action_id"] == "stale-view-action"


def test_manifest_blocks_no_gateway_row_without_open_reservation(
    session: Session,
) -> None:
    script = _load_script()
    reservation = session.scalar(select(AccountPacingReservation))
    reservation.state = "missed"
    session.flush()

    manifest = script.build_manifest(session, _options(script))

    assert manifest["candidate_count"] == 0
    assert manifest["blocked"] is True
    assert manifest["scope"]["blocked_no_fact_no_gateway_count"] == 1


def test_apply_chunk_writes_safe_fact_and_releases_owner(session: Session) -> None:
    script = _load_script()
    options = _options(script)
    manifest = script.build_manifest(session, options)

    script._apply_chunk(
        session,
        options,
        manifest["candidates"],
        state_hash=script.manifest_hash(manifest),
        batch_ordinal=1,
    )
    session.commit()

    action = session.get(Action, "stale-view-action")
    owner = session.get(ViewFulfillmentObligation, "stale-view-owner")
    reservation = session.scalar(select(AccountPacingReservation))
    fact = session.scalar(select(FulfillmentRemoteFact).where(
        FulfillmentRemoteFact.action_id == action.id,
    ))
    audit = session.scalar(select(AuditLog))
    assert action.result["error_code"] == "stale_channel_daily_action"
    assert fact is not None and fact.fact_kind == "safely_not_executed"
    assert owner.status == "open" and owner.current_action_id is None
    assert reservation.state == "missed"
    assert audit is not None and options.approval_ref in audit.detail


def _options(script):
    return script.ReconcileOptions(
        task_ids=("stale-view-task",),
        execution_date=EXECUTION_DATE,
        deployed_sha=DEPLOYED_SHA,
        actor="codex-production-recovery",
        approval_ref="intake-2026-08-17-planner-pacing-memory-001",
    )


def _seed(session: Session) -> None:
    session.add(Tenant(id=1, name="租户"))
    session.flush()
    session.add(TgAccount(
        id=1,
        tenant_id=1,
        display_name="账号",
        phone_masked="***0001",
        status="在线",
    ))
    target = OperationTarget(
        id=10,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-10010",
        title="频道",
    )
    task = Task(
        id="stale-view-task",
        tenant_id=1,
        name="旧日浏览",
        type="channel_view",
        status="running",
        fulfillment_contract_version="fact_first_v3",
    )
    session.add_all([target, task])
    session.flush()
    ledger = _ledger(task)
    message = ChannelMessage(
        id=101,
        tenant_id=1,
        channel_target_id=target.id,
        message_id=1001,
    )
    session.add_all([ledger, message])
    session.flush()
    owner = ViewFulfillmentObligation(
        id="stale-view-owner",
        tenant_id=1,
        task_day_ledger_id=ledger.id,
        channel_message_id=message.id,
        account_id=1,
        status="pending",
        current_action_id="stale-view-action",
    )
    action = _action(task, ledger, owner)
    session.add_all([owner, action])
    session.flush()
    session.add(_reservation(task, action, ledger.deadline_at))
    session.commit()


def _ledger(task: Task) -> TaskDayLedger:
    start = datetime(2026, 8, 18)
    return TaskDayLedger(
        id="stale-view-ledger",
        tenant_id=1,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=EXECUTION_DATE,
        period_start_at=start,
        deadline_at=start + timedelta(days=1),
        day_phase="full_day",
        planning_anchor_at=start,
    )


def _action(
    task: Task,
    ledger: TaskDayLedger,
    owner: ViewFulfillmentObligation,
) -> Action:
    return Action(
        id="stale-view-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="view_message",
        account_id=1,
        status="skipped",
        scheduled_at=ledger.deadline_at + timedelta(hours=1),
        executed_at=ledger.deadline_at,
        pacing_slot_key="stale-view-slot",
        result={"error_code": "stale_channel_daily_action"},
        payload={
            "execution_date": EXECUTION_DATE.isoformat(),
            "task_day_ledger_id": ledger.id,
            "view_fulfillment_obligation_id": owner.id,
            "channel_message_id": 101,
            "channel_id": "-10010",
        },
    )


def _reservation(
    task: Task,
    action: Action,
    deadline: datetime,
) -> AccountPacingReservation:
    return AccountPacingReservation(
        tenant_id=1,
        task_id=task.id,
        account_id=1,
        pacing_slot_key=action.pacing_slot_key,
        policy_version="account_soft_pacing_v1",
        due_at=deadline - timedelta(hours=1),
        release_not_before_at=action.scheduled_at,
        effective_claim_at=action.scheduled_at,
        source_deadline_at=deadline,
        action_id=action.id,
        state="bound",
    )
