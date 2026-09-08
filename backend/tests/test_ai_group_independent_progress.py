"""Ready speakers must not be hidden behind a bounded membership batch."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    OperationTarget, Task, TaskAccountDailyCoverage, TaskMembershipAdmissionItem,
    Tenant, TgAccount, TgGroup, TgGroupAccount,
)
from app.services.task_center import channel_membership
from app.services.task_center.executors import group_ai_chat


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 9, 9, 1)
BLOCKED_PREFIX_COUNT = 25
READY_ACCOUNT_ID = BLOCKED_PREFIX_COUNT + 1


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        yield current
    engine.dispose()


def _seed(session, *, prefix_state="pending_admission"):
    session.add(Tenant(id=1, name="test"))
    group = TgGroup(id=1, tenant_id=1, tg_peer_id="-1001", title="test group")
    target = OperationTarget(id=1, tenant_id=1, target_type="group",
        tg_peer_id="-1001", title="test group", auth_status="已授权运营", can_send=True)
    task = Task(id="independent-group", tenant_id=1, name="test task",
        type="group_ai_chat", status="running", fulfillment_contract_version="fact_first_v3",
        type_config={"engagement_contract_version": "unified_engagement_v1",
            "account_coverage_mode": "all_accounts_daily", "target_group_id": 1,
            "target_operation_target_id": 1})
    session.add_all([group, target, task])
    session.flush()
    for identity in range(1, READY_ACCOUNT_ID + 1):
        ready = identity == READY_ACCOUNT_ID
        session.add(TgAccount(id=identity, tenant_id=1, display_name="test account",
            phone_masked="test", status="在线", account_lifecycle_status="business_active",
            session_ciphertext="test-session"))
        session.add(TaskMembershipAdmissionItem(tenant_id=1, task_id=task.id,
            target_id=target.id, account_id=identity, phase="completed" if ready else "joining"))
        session.add(TaskAccountDailyCoverage(id=f"coverage-{identity}", tenant_id=1,
            task_id=task.id, group_id=group.id, account_id=identity,
            coverage_date=NOW.date(), target_count=1,
            state="ready" if ready else prefix_state,
            targeted_at=NOW - timedelta(minutes=READY_ACCOUNT_ID - identity)))
    session.add(TgGroupAccount(tenant_id=1, group_id=group.id,
        account_id=READY_ACCOUNT_ID, can_send=True))
    session.commit()
    return task, group, target


def _candidate_rows(session, task, group, *, admitted, monkeypatch):
    monkeypatch.setattr(group_ai_chat, "_portfolio_coverage_rows", lambda _s, _t, **kw: kw["rows"])
    monkeypatch.setattr(group_ai_chat, "ensure_natural_opportunity_plan",
        lambda *_a, **_kw: SimpleNamespace(guaranteed_now_capacity=20))
    return group_ai_chat._coverage_candidate_rows(session, task,
        group=group, ledger=SimpleNamespace(id="day"), target=SimpleNamespace(id="target"),
        participation=SimpleNamespace(selected_account_ids=list(range(1, READY_ACCOUNT_ID + 1))),
        admission=SimpleNamespace(admissible_account_ids=admitted),
        timestamp=NOW, required_units=1)


@pytest.mark.parametrize("prefix_state", ["pending_admission", "ready"])
def test_admitted_speaker_after_full_rejected_prefix_is_selected(session, monkeypatch, prefix_state):
    task, group, _target = _seed(session, prefix_state=prefix_state)
    rows = _candidate_rows(session, task, group,
        admitted=[READY_ACCOUNT_ID], monkeypatch=monkeypatch)
    assert [row.account_id for row in rows] == [READY_ACCOUNT_ID]
    assert session.query(TaskAccountDailyCoverage).count() == READY_ACCOUNT_ID


def test_empty_admission_selects_no_speaker_without_widening_scope(session, monkeypatch):
    task, group, _target = _seed(session)
    assert _candidate_rows(session, task, group, admitted=[], monkeypatch=monkeypatch) == []


def test_empty_membership_batch_does_not_block_existing_ready_speaker(session, monkeypatch):
    task, _group, target = _seed(session)
    task.stats = {"account_assignment_eligibility": {"pending_count": BLOCKED_PREFIX_COUNT}}
    monkeypatch.setattr(channel_membership, "_task_membership_candidates", lambda *_a, **_kw: [])
    result = channel_membership.gate_channel_membership(session, task, target, require_send=True)
    assert result.ready
    assert result.created == 0
    assert task.stats["membership_joined_count"] == 1
    assert session.scalar(select(TaskMembershipAdmissionItem.phase).where(
        TaskMembershipAdmissionItem.account_id == 1)) == "joining"


def test_empty_membership_batch_without_ready_speaker_still_waits(session, monkeypatch):
    task, _group, target = _seed(session)
    item = session.scalar(select(TaskMembershipAdmissionItem).where(
        TaskMembershipAdmissionItem.account_id == READY_ACCOUNT_ID))
    item.phase = "joining"
    task.stats = {"account_assignment_eligibility": {"pending_count": READY_ACCOUNT_ID}}
    session.flush()
    monkeypatch.setattr(channel_membership, "_task_membership_candidates", lambda *_a, **_kw: [])
    result = channel_membership.gate_channel_membership(session, task, target, require_send=True)
    assert not result.ready
    assert result.blocker_reason == "account_eligibility_busy"
