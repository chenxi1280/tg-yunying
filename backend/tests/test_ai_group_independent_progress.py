"""Ready speakers must not be hidden behind a bounded membership batch."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountProxyBinding, ExecutionCircuitState, ExecutionResiliencePolicyRevision,
    ManagedPresencePlan, ManagedPresencePolicyRevision, NaturalOpportunitySupplyPlanRevision,
    OperationTarget, Task, TaskAccountDailyCoverage, TaskDayLedger, TaskMembershipAdmissionItem,
    Tenant, TgAccount, TgGroup, TgGroupAccount,
)
from app.services.task_center import channel_membership
from app.services.task_center.ai_group_circuit_eligibility import blocked_coverage_account_ids
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


def test_circuit_blocked_prefix_is_filtered_before_batch_limit(session, monkeypatch):
    task, group, _target = _seed(session, prefix_state="ready")
    policy = ExecutionResiliencePolicyRevision(tenant_id=1)
    session.add(policy)
    session.flush()
    for account_id in range(1, READY_ACCOUNT_ID):
        session.add(ExecutionCircuitState(
            tenant_id=1,
            resilience_policy_revision_id=policy.id,
            domain_kind="account",
            domain_key=f"account:{account_id}",
            state="open",
            opened_until=NOW + timedelta(minutes=15),
        ))
    session.commit()

    rows = _candidate_rows(
        session,
        task,
        group,
        admitted=list(range(1, READY_ACCOUNT_ID + 1)),
        monkeypatch=monkeypatch,
    )

    assert [row.account_id for row in rows] == [READY_ACCOUNT_ID]


def test_all_circuit_domains_block_and_closed_domains_restore(session):
    task, _group, _target = _seed(session, prefix_state="ready")
    policy = ExecutionResiliencePolicyRevision(tenant_id=1)
    session.add(policy)
    session.flush()
    for account_id, proxy_id, exit_ip in ((1, 101, "1.1.1.1"), (2, 102, "2.2.2.2")):
        account = session.get(TgAccount, account_id)
        account.proxy_id = proxy_id
        session.add(AccountProxyBinding(
            tenant_id=1,
            account_id=account_id,
            proxy_id=proxy_id,
            observed_exit_ip=exit_ip,
        ))
    session.add_all([
        ExecutionCircuitState(
            tenant_id=1, resilience_policy_revision_id=policy.id,
            domain_kind="proxy_route", domain_key="proxy:101", state="open",
            opened_until=NOW + timedelta(minutes=15),
        ),
        ExecutionCircuitState(
            tenant_id=1, resilience_policy_revision_id=policy.id,
            domain_kind="proxy_egress", domain_key="exit:2.2.2.2", state="half_open",
            probe_lease_until=NOW + timedelta(minutes=1),
        ),
        ExecutionCircuitState(
            tenant_id=1, resilience_policy_revision_id=policy.id,
            domain_kind="account", domain_key="account:3", state="open",
            opened_until=NOW - timedelta(seconds=1),
        ),
        ExecutionCircuitState(
            tenant_id=1, resilience_policy_revision_id=policy.id,
            domain_kind="account", domain_key="account:4", state="closed",
        ),
    ])
    session.commit()

    assert blocked_coverage_account_ids(session, task) == {1, 2, 3}

    for circuit in session.scalars(select(ExecutionCircuitState)):
        circuit.state = "closed"
    session.flush()
    assert blocked_coverage_account_ids(session, task) == set()


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


def test_idle_continuation_decision_falls_back_to_task_creation(session):
    task = Task(
        id="idle-task",
        tenant_id=1,
        created_at=NOW - timedelta(minutes=10),
        scheduled_start=NOW - timedelta(minutes=10),
        stats={},
        type_config={"idle_continuation_enabled": True, "idle_continuation_seconds": 300},
    )
    decision = group_ai_chat._idle_continuation_decision(session, task, task.type_config)
    assert decision["due"] is True
    assert decision["next_run_at"] is not None


def _zero_natural_opportunity_ledger(session, task):
    period_start = NOW.replace(hour=0)
    ledger = TaskDayLedger(id="zero-opportunity-day", tenant_id=task.tenant_id,
        task_id=task.id, timezone_snapshot="Asia/Shanghai", timezone_revision=1,
        obligation_local_date=NOW.date(), period_start_at=period_start,
        deadline_at=period_start + timedelta(days=1), day_phase="full_day",
        planning_anchor_at=period_start)
    session.add_all([ledger, ManagedPresencePolicyRevision(
        tenant_id=task.tenant_id, bootstrap_allowance=0,
    )])
    session.flush()
    return ledger


@pytest.mark.parametrize("idle_enabled", [None, True, False])
@pytest.mark.parametrize("required_units", [0, 1])
@pytest.mark.parametrize("previous_error", ["natural_opportunity_plan_unproven", "account_eligibility_busy"])
def test_zero_natural_opportunity_preserves_admitted_quantity_supply_and_quality_evidence(
    session, monkeypatch, *, idle_enabled, required_units, previous_error,
):
    task, group, _target = _seed(session)
    ledger = _zero_natural_opportunity_ledger(session, task)
    task.last_error = previous_error
    if idle_enabled is not None:
        task.type_config = {**task.type_config, "idle_continuation_enabled": idle_enabled}
    monkeypatch.setattr(group_ai_chat, "_portfolio_coverage_rows", lambda _s, _t, **kw: kw["rows"])

    rows = group_ai_chat._coverage_candidate_rows(session, task,
        group=group, ledger=ledger, target=SimpleNamespace(id="target"),
        participation=SimpleNamespace(selected_account_ids=[READY_ACCOUNT_ID]),
        admission=SimpleNamespace(admissible_account_ids=[READY_ACCOUNT_ID]),
        timestamp=NOW, required_units=required_units)

    assert [row.account_id for row in rows] == [READY_ACCOUNT_ID]
    assert session.query(TaskAccountDailyCoverage).count() == READY_ACCOUNT_ID
    assert session.get(TaskAccountDailyCoverage, "coverage-1").state == "pending_admission"
    presence = session.scalar(select(ManagedPresencePlan))
    opportunity = session.scalar(select(NaturalOpportunitySupplyPlanRevision))
    assert presence.external_human_turn_count == presence.remaining_capacity == 0
    assert opportunity.guaranteed_now_capacity == 0
    assert opportunity.deficit == required_units
    assert task.stats["natural_opportunity"]["effect"] == "quality_observation_only"
    assert task.stats["natural_opportunity"]["deficit"] == required_units
    assert task.last_error == ("" if previous_error == "natural_opportunity_plan_unproven" else previous_error)


def test_finalize_generation_schedule_retains_partial_items_on_shortfall():
    task = Task(
        id="schedule-task",
        fulfillment_contract_version="fact_first_v3",
        stats={},
        last_error="",
    )
    quality_items = [{"text": "msg1"}, {"text": "msg2"}, {"text": "msg3"}]
    times = [NOW + timedelta(seconds=10), NOW + timedelta(seconds=20)]
    
    # Simulate schedule returning 2 times for 3 items
    session = None
    facts = SimpleNamespace(config={}, hard_progress={})
    context = SimpleNamespace(usable_rows=[1], mode="autonomous")
    
    import unittest.mock as mock
    with mock.patch.object(group_ai_chat, "_schedule_generation_items", return_value=(quality_items, times)):
        res = group_ai_chat._finalize_generation_schedule(
            session, task, facts, context, quality_items=quality_items, is_generic_warmup=False
        )
        assert res is not None
        items, sched_times = res
        assert len(items) == 2
        assert len(sched_times) == 2
        assert task.stats["pacing_schedule_shortfall"]["scheduled"] == 2
        assert task.stats["pacing_schedule_shortfall"]["requested"] == 3
