from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.no_postgres

from app.database import Base
from app.models import (
    AccountBehaviorBudgetLedger,
    AccountBehaviorBudgetPolicyRevision,
    AccountBehaviorBudgetReservation,
    AccountPool,
    AccountPoolConcurrencyLease,
    AccountPoolConcurrencyPolicyRevision,
    Action,
    ExecutionAttempt,
    ExecutionResiliencePolicyRevision,
    NegativeOutcomeCircuitState,
    NegativeOutcomePolicyRevision,
    Task,
    TaskAccountGroupBindingSetRevision,
    Tenant,
    TgAccount,
)
from app.services._common import _now
from app.services.task_center.engagement_binding import activate_due_binding
from app.services.task_center.engagement_runtime_policy import (
    ensure_behavior_policy,
    ensure_pool_policy,
    ensure_resilience_policy,
)
from app.services.task_center.engagement_runtime_resources import (
    _active_budget_policy,
    _active_pool_policy,
    _active_resilience_policy,
    recover_stale_concurrency_leases,
)
from app.services.task_center.negative_outcome_circuit import (
    ensure_negative_outcome_policy,
    recover_circuit_from_visibility,
)
from app.services.task_center.service import (
    _check_stop_conditions,
    _commit_claimed_stale_recovery,
    _naive_datetime,
    _scheduled_at_is_future,
    RecoveryClaim,
)
from app.timezone import BEIJING_TZ, as_beijing


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as session:
        yield session


def test_naive_datetime_and_scheduled_at_is_future():
    now = _now()
    # Test naive datetime input
    naive_future = now + timedelta(hours=2)
    naive_past = now - timedelta(hours=2)
    assert _scheduled_at_is_future(naive_future) is True
    assert _scheduled_at_is_future(naive_past) is False

    # Test UTC aware datetime input
    utc_future = datetime.now(timezone.utc) + timedelta(hours=2)
    utc_past = datetime.now(timezone.utc) - timedelta(hours=2)
    assert _scheduled_at_is_future(utc_future) is True
    assert _scheduled_at_is_future(utc_past) is False

    # _naive_datetime returns normalized beijing wall time safely
    converted = _naive_datetime(utc_future)
    assert converted is not None
    assert _naive_datetime(None) is None


def test_check_stop_conditions_naive_and_aware(session: Session):
    tenant = Tenant(id=1, name="Test Tenant")
    session.add(tenant)
    session.flush()

    # Case 1: scheduled_end is naive past -> should stop and complete without TypeError
    naive_past = _now() - timedelta(hours=1)
    task1 = Task(
        id="task-naive-past",
        tenant_id=1,
        name="Task 1",
        type="channel_view",
        status="running",
        scheduled_end=naive_past,
    )
    session.add(task1)
    session.flush()
    stopped = _check_stop_conditions(session, task1)
    assert stopped is True
    assert task1.status == "completed"

    # Case 2: scheduled_end is aware future -> should NOT stop
    aware_future = datetime.now(timezone.utc) + timedelta(hours=1)
    task2 = Task(
        id="task-aware-future",
        tenant_id=1,
        name="Task 2",
        type="channel_view",
        status="running",
        scheduled_end=aware_future,
    )
    session.add(task2)
    session.flush()
    stopped = _check_stop_conditions(session, task2)
    assert stopped is False
    assert task2.status == "running"


def test_self_healing_runtime_policies(session: Session):
    tenant_id = 99
    pool_id = 101
    session.add(Tenant(id=tenant_id, name="policy tenant"))
    session.flush()
    session.add(AccountPool(id=pool_id, tenant_id=tenant_id, name="policy pool"))
    session.flush()

    # 1. Active pool policy self-healing
    pool_policy = _active_pool_policy(session, tenant_id, pool_id)
    assert pool_policy is not None
    assert pool_policy.tenant_id == tenant_id
    assert pool_policy.account_pool_id == pool_id
    assert pool_policy.state == "active"

    # 2. Active budget policy self-healing for custom / empty / normal classes
    budget_policy = _active_budget_policy(session, tenant_id, "custom_persona")
    assert budget_policy is not None
    assert budget_policy.tenant_id == tenant_id
    assert budget_policy.state == "active"

    # 3. Active resilience policy self-healing
    resilience_policy = _active_resilience_policy(session, tenant_id)
    assert resilience_policy is not None
    assert resilience_policy.tenant_id == tenant_id
    assert resilience_policy.state == "active"


def test_orphan_without_attempt_is_not_proof_of_remote_completion(session: Session):
    tenant_id = 1
    pool_id = 10
    action_id = "act-123"
    attempt_id = "att-123"

    # Create an orphaned lease where the action is already completed/failed
    action = Action(
        id=action_id,
        tenant_id=tenant_id,
        task_id="task-1",
        task_type="channel_view",
        action_type="view_message",
        status="failed",
    )
    lease = AccountPoolConcurrencyLease(
        id=str(uuid4()),
        tenant_id=tenant_id,
        account_pool_id=pool_id,
        policy_revision_id="pol-1",
        task_group_share_limit=2,
        invocation_identity="inv-123",
        action_id=action_id,
        attempt_id=attempt_id,
        account_id=55,
        task_id="task-1",
        state="call_issued",
        acquired_at=_now() - timedelta(minutes=20),
    )
    session.add_all([action, lease])
    session.flush()

    # Run recovery
    recovered_count = recover_stale_concurrency_leases(session)
    assert recovered_count == 0
    assert lease.state == "call_issued"


def test_stale_recovery_exposes_incomplete_resource_set(session: Session):
    tenant_id = 1
    pool_id = 10
    action_id = "act-stale"
    attempt_id = "att-stale"

    action = Action(
        id=action_id,
        tenant_id=tenant_id,
        task_id="task-stale",
        task_type="channel_view",
        action_type="view_message",
        status="executing",
        payload={"execution_date": "2026-09-05"},
    )
    attempt = ExecutionAttempt(
        id=attempt_id,
        tenant_id=tenant_id,
        action_id=action_id,
        attempt_no=1,
        status="call_not_started",
    )
    lease = AccountPoolConcurrencyLease(
        id=str(uuid4()),
        tenant_id=tenant_id,
        account_pool_id=pool_id,
        policy_revision_id="pol-stale",
        task_group_share_limit=2,
        invocation_identity="inv-stale",
        action_id=action_id,
        attempt_id=attempt_id,
        account_id=55,
        task_id="task-stale",
        state="call_issued",
    )
    task = Task(
        id="task-stale",
        tenant_id=tenant_id,
        name="Task Stale",
        type="channel_view",
        status="running",
    )
    session.add_all([task, action, attempt, lease])
    session.flush()

    claim = RecoveryClaim(action_id=action_id, token="token-1")
    with pytest.raises(RuntimeError, match="engagement_runtime_resource_set_incomplete"):
        _commit_claimed_stale_recovery(
            session, action, task=task, claim=claim, latest_attempt=attempt,
            projection=None, now=_now(),
        )
    assert lease.state == "call_issued"


def test_activate_due_binding_without_active_binding(session: Session):
    from app.models import AccountPool
    from app.services.task_center.engagement_binding import validate_engagement_binding
    from tests.account_group_revision_test_support import bootstrap_groups

    session.add(Tenant(id=1, name="Binding tenant"))
    session.flush()
    session.add(AccountPool(id=101, tenant_id=1, name="Binding group"))
    bootstrap_groups(session, 1, (101,))
    config = {"engagement_contract_version": "unified_engagement_v1",
        "account_selection_mode": "group", "account_group_ids": [101],
        "concurrency_limit_per_group": 2}
    spec = validate_engagement_binding(session, 1, "group_ai_chat", config)
    task = Task(
        id="task-binding",
        tenant_id=1,
        name="Task Binding",
        type="group_ai_chat",
        status="running",
    )
    scheduled_binding = TaskAccountGroupBindingSetRevision(
        id="bind-scheduled",
        tenant_id=1,
        task_id="task-binding",
        binding_set_revision=1,
        state="scheduled",
        effective_from=_now() - timedelta(minutes=5),
        account_group_ids=[101],
        concurrency_limit_per_group=2,
        group_contracts=list(spec.group_contracts),
        binding_set_hash=spec.binding_set_hash,
    )
    session.add_all([task, scheduled_binding])
    session.flush()

    # Calling activate_due_binding when no active binding exists should NOT raise ValueError
    activated = activate_due_binding(session, task, period_start=_now())
    assert activated is not None
    assert activated.state == "active"


def test_recover_circuit_from_visibility_self_healing(session: Session):
    tenant_id = 1
    tenant = Tenant(id=tenant_id, name="Test")
    circuit = NegativeOutcomeCircuitState(
        id=str(uuid4()),
        tenant_id=tenant_id,
        peer_id="peer-100",
        level="response_restricted",
        eligible_exit_at=_now() - timedelta(minutes=10),
        events=[],
        policy_revision_id="non-existent-policy-id",
    )
    session.add_all([tenant, circuit])
    session.flush()

    # Recovering should self-heal policy instead of raising RuntimeError
    recover_circuit_from_visibility(
        session,
        tenant_id=tenant_id,
        peer_id="peer-100",
        account_id=None,
        route="",
        observed_at=_now(),
    )
    assert circuit.level == "normal"
