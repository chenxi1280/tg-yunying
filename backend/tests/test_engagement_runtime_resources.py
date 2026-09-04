from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountBehaviorBudgetLedger,
    AccountBehaviorBudgetPolicyRevision,
    AccountBehaviorBudgetReservation,
    AccountExternalUseHold,
    AccountPool,
    AccountPoolConcurrencyLease,
    AccountPoolConcurrencyPolicyRevision,
    Action,
    ExecutionAttempt,
    ExecutionCircuitState,
    ExecutionResiliencePolicyRevision,
    ExternalAccountUsePolicyRevision,
    OperationTarget,
    RemoteInvocationFence,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
    UnownedOutboundActivityObservation,
)
from app.models.risk_control import AccountProxy
from app.schemas import ChannelLikeTaskCreate
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.daily_ledgers import ensure_task_day_ledger
from app.services.task_center.engagement_participation import (
    ensure_source_participation_plan,
)
from app.services.task_center.gateway_evidence_journal import (
    bind_gateway_request_identity,
)
from app.services.task_center.engagement_runtime_resources import (
    RuntimeResourceBlocked,
    mark_attempt_call_issued,
    reserve_attempt_resources,
    settle_attempt_resources,
)
from app.services.task_center.engagement_unowned_activity import (
    observe_managed_outbound,
)
from app.services.task_center.service import create_channel_like_task


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed(
    session: Session,
    *,
    task_limit: int = 2,
    pool_limit: int = 2,
    proxy_limit: int = 2,
):
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(AccountPool(id=1, tenant_id=1, name="互动一组"))
    session.add_all([_account(11), _account(12), _account(13)])
    session.add(OperationTarget(id=101, tenant_id=1, target_type="channel", tg_peer_id="-100101", title="测试频道"))
    session.commit()
    _seed_policies(session, pool_limit, proxy_limit)
    task = create_channel_like_task(
        session,
        1,
        ChannelLikeTaskCreate(
            name="统一点赞",
            target_channel_id=101,
            engagement_contract_version="unified_engagement_v1",
            account_group_ids=[1],
            concurrency_limit_per_group=task_limit,
            daily_reaction_cap=80,
        ),
        "tester",
    )
    return task


def _account(account_id: int) -> TgAccount:
    return TgAccount(
        id=account_id,
        tenant_id=1,
        pool_id=1,
        display_name=f"账号{account_id}",
        phone_masked=str(account_id),
        status="在线",
    )


def _seed_policies(
    session: Session,
    pool_limit: int,
    proxy_limit: int,
) -> None:
    session.add_all(
        [
            ExecutionResiliencePolicyRevision(
                tenant_id=1,
                proxy_route_inflight_limit=proxy_limit,
                proxy_egress_inflight_limit=proxy_limit,
            ),
            AccountPoolConcurrencyPolicyRevision(
                tenant_id=1,
                account_pool_id=1,
                hard_remote_inflight_limit=pool_limit,
            ),
            AccountBehaviorBudgetPolicyRevision(
                tenant_id=1,
                account_class="normal",
                action_budgets={"total": 5, "reaction": 5, "view": 5},
            ),
        ]
    )
    session.commit()


def _attempt(
    session: Session,
    task,
    account_id: int,
    *,
    action_type: str = "like_message",
):
    action = Action(
        tenant_id=1,
        task_id=task.id,
        task_type="channel_like",
        action_type=action_type,
        account_id=account_id,
        payload={},
    )
    session.add(action)
    session.flush()
    attempt = ExecutionAttempt(
        tenant_id=1,
        action_id=action.id,
        account_id=account_id,
        attempt_no=1,
        status="before_call",
    )
    session.add(attempt)
    session.flush()
    return action, attempt


def test_pool_global_limit_and_release_are_cross_action() -> None:
    with _session() as session:
        task = _seed(session, pool_limit=2)
        first = _attempt(session, task, 11)
        second = _attempt(session, task, 12)
        third = _attempt(session, task, 13)
        for action, attempt in (first, second):
            reserve_attempt_resources(session, action, attempt)
            mark_attempt_call_issued(session, attempt)

        with pytest.raises(RuntimeResourceBlocked, match="account_pool_remote_inflight_full"):
            reserve_attempt_resources(session, *third)

        first[0].status = "success"
        first[1].status = "success"
        settle_attempt_resources(first[1], first[0], remote_mutation_started=True)
        reserve_attempt_resources(session, *third)
        leases = list(session.scalars(select(AccountPoolConcurrencyLease)))
        assert [row.state for row in leases].count("released") == 1


def test_runtime_lease_uses_frozen_origin_after_account_moves_pool() -> None:
    with _session() as session:
        task = _seed(session)
        ledger = ensure_task_day_ledger(session, task)
        plan = ensure_source_participation_plan(
            session,
            task,
            ledger,
            source_identity="message:runtime-origin",
            required_count=3,
        )
        assert plan is not None and 11 in plan.selected_account_ids
        session.add(AccountPool(id=2, tenant_id=1, name="互动二组"))
        session.flush()
        session.get(TgAccount, 11).pool_id = 2
        action, attempt = _attempt(session, task, 11)

        reserve_attempt_resources(session, action, attempt)

        lease = session.scalar(select(AccountPoolConcurrencyLease))
        assert lease.account_pool_id == 1
        assert attempt.result_snapshot["engagement_account_pool_id"] == 1
        assert (
            attempt.result_snapshot["engagement_account_pool_provenance"]
            == "frozen_participation_plan"
        )
        assert attempt.result_snapshot["engagement_participation_plan_id"] == plan.id


def test_task_group_share_is_not_multiplied_by_accounts() -> None:
    with _session() as session:
        task = _seed(session, task_limit=1, pool_limit=2)
        first = _attempt(session, task, 11)
        second = _attempt(session, task, 12)
        reserve_attempt_resources(session, *first)

        with pytest.raises(RuntimeResourceBlocked, match="task_group_share_full"):
            reserve_attempt_resources(session, *second)


def test_cross_adapter_total_budget_is_enforced_at_gateway_reservation() -> None:
    with _session() as session:
        task = _seed(session)
        policy = session.scalar(select(AccountBehaviorBudgetPolicyRevision))
        policy.action_budgets = {"total": 1, "reaction": 5, "view": 5}
        reaction = _attempt(session, task, 11)
        reserve_attempt_resources(session, *reaction)
        mark_attempt_call_issued(session, reaction[1])
        reaction[0].status = "success"
        reaction[1].status = "success"
        settle_attempt_resources(
            reaction[1], reaction[0], remote_mutation_started=True
        )

        view = _attempt(session, task, 11, action_type="view_message")
        with pytest.raises(
            RuntimeResourceBlocked,
            match="account_behavior_total_budget_exhausted",
        ):
            reserve_attempt_resources(session, *view)


def test_external_use_hold_is_peer_source_scoped_and_never_blocks_view() -> None:
    with _session() as session:
        task = _seed(session)
        authorization = TgAccountAuthorization(
            tenant_id=1, account_id=11, is_current=True,
            telegram_user_id_digest=hashlib.sha256(b"88").hexdigest(),
        )
        session.add(authorization)
        session.flush()
        assert observe_managed_outbound(
            session, tenant_id=1, canonical_peer_id="-100101",
            payload={
                "source_message_id": 800,
                "sender_peer_id": "88",
                "source_revision_id": "source-1",
            },
            action_class="reaction", source_event_id="event-800",
        )

        same_action, same_attempt = _attempt(session, task, 11)
        same_action.payload = {
            "channel_id": "-100101", "source_revision_id": "source-1",
        }
        with pytest.raises(RuntimeResourceBlocked, match="account_external_use_hold"):
            reserve_attempt_resources(session, same_action, same_attempt)

        other_action, other_attempt = _attempt(session, task, 11)
        other_action.payload = {
            "channel_id": "-100102", "source_revision_id": "source-1",
        }
        reserve_attempt_resources(session, other_action, other_attempt)
        other_action.status = "failed"
        other_attempt.status = "failed"
        settle_attempt_resources(
            other_attempt, other_action, remote_mutation_started=False,
        )

        view_action, view_attempt = _attempt(
            session, task, 11, action_type="view_message",
        )
        view_action.payload = {
            "channel_id": "-100101", "source_revision_id": "source-1",
        }
        reserve_attempt_resources(session, view_action, view_attempt)


def test_proxy_bulkhead_isolates_stuck_route_without_blocking_other_proxy() -> None:
    with _session() as session:
        task = _seed(session, task_limit=3, pool_limit=3, proxy_limit=1)
        session.add_all(
            [
                AccountProxy(id=1, tenant_id=1, name="proxy-1", port=10001),
                AccountProxy(id=2, tenant_id=1, name="proxy-2", port=10002),
            ]
        )
        session.get(TgAccount, 11).proxy_id = 1
        session.get(TgAccount, 12).proxy_id = 1
        session.get(TgAccount, 13).proxy_id = 2
        session.flush()

        reserve_attempt_resources(session, *_attempt(session, task, 11))
        with pytest.raises(RuntimeResourceBlocked, match="proxy_route_inflight_full"):
            reserve_attempt_resources(session, *_attempt(session, task, 12))

        reserve_attempt_resources(session, *_attempt(session, task, 13))


def test_unknown_holds_physical_lease_and_behavior_occupancy() -> None:
    with _session() as session:
        task = _seed(session)
        action, attempt = _attempt(session, task, 11)
        reserve_attempt_resources(session, action, attempt)
        mark_attempt_call_issued(session, attempt)
        action.status = "unknown_after_send"
        attempt.status = "result_unknown"

        settle_attempt_resources(attempt, action, remote_mutation_started=True)

        reservation = session.scalar(select(AccountBehaviorBudgetReservation))
        fence = session.scalar(select(RemoteInvocationFence))
        ledger = session.scalar(select(AccountBehaviorBudgetLedger))
        assert reservation is not None and reservation.state == "unknown"
        assert fence is not None and fence.business_outcome_state == "unknown"
        assert ledger is not None
        assert ledger.counters["reaction"]["unknown"] == 1
        lease = session.scalar(select(AccountPoolConcurrencyLease))
        assert lease.state == "remote_unknown"
        assert lease.released_at is None
        assert fence.state == "remote_unknown"
        assert fence.transport_termination_state == "unproven"
        assert fence.transport_terminated_at is None


def test_returned_unknown_releases_physical_lease_but_holds_business_budget() -> None:
    with _session() as session:
        task = _seed(session)
        action, attempt = _attempt(session, task, 11)
        reserve_attempt_resources(session, action, attempt)
        mark_attempt_call_issued(session, attempt)
        bind_gateway_request_identity(action, attempt)
        attempt.gateway_call_started_at = _now()
        action.status = "unknown_after_send"

        dispatcher._finish_execution_attempt(attempt, action)

        lease = session.scalar(select(AccountPoolConcurrencyLease))
        reservation = session.scalar(select(AccountBehaviorBudgetReservation))
        fence = session.scalar(select(RemoteInvocationFence))
        assert attempt.status == "result_unknown"
        assert attempt.result_snapshot["transport_termination_state"] == "acknowledged"
        assert lease.state == "released"
        assert lease.release_reason == "transport_terminated_business_unknown"
        assert reservation.state == "unknown"
        assert fence.state == "remote_unknown"
        assert fence.business_outcome_state == "unknown"
        assert fence.transport_termination_state == "acknowledged"
        assert fence.transport_terminated_at is not None
        assert session.scalar(select(func.count(ExecutionCircuitState.id))) == 0


def test_gateway_mutation_unknown_is_not_reclassified_as_retryable_failure() -> None:
    with _session() as session:
        task = _seed(session)
        action, attempt = _attempt(session, task, 11)
        reserve_attempt_resources(session, action, attempt)
        mark_attempt_call_issued(session, attempt)
        bind_gateway_request_identity(action, attempt)
        attempt.gateway_call_started_at = _now()

        dispatcher._apply_send_result(
            action,
            session.get(TgAccount, 11),
            False,
            failure_type="comment_remote_result_unknown",
            detail="channel_comment_remote_identity_unproven",
            attempt=attempt,
            remote_mutation_started=None,
        )

        lease = session.scalar(select(AccountPoolConcurrencyLease))
        reservation = session.scalar(select(AccountBehaviorBudgetReservation))
        fence = session.scalar(select(RemoteInvocationFence))
        assert action.status == "unknown_after_send"
        assert attempt.status == "result_unknown"
        assert lease.state == "released"
        assert reservation.state == "unknown"
        assert fence.business_outcome_state == "unknown"
        assert action.result["error_code"] == "comment_remote_result_unknown"


def test_unknown_blocks_same_account_but_not_healthy_peer() -> None:
    with _session() as session:
        task = _seed(session, pool_limit=2)
        unknown_action, unknown_attempt = _attempt(session, task, 11)
        reserve_attempt_resources(session, unknown_action, unknown_attempt)
        mark_attempt_call_issued(session, unknown_attempt)
        unknown_action.status = "unknown_after_send"
        unknown_attempt.status = "result_unknown"
        settle_attempt_resources(
            unknown_attempt,
            unknown_action,
            remote_mutation_started=True,
        )

        with pytest.raises(RuntimeResourceBlocked, match="account_remote_inflight"):
            reserve_attempt_resources(session, *_attempt(session, task, 11))

        reserve_attempt_resources(session, *_attempt(session, task, 12))


def test_unknown_to_confirmed_releases_once_and_moves_budget_once() -> None:
    with _session() as session:
        task = _seed(session)
        action, attempt = _attempt(session, task, 11)
        reserve_attempt_resources(session, action, attempt)
        mark_attempt_call_issued(session, attempt)
        action.status = "unknown_after_send"
        attempt.status = "result_unknown"
        settle_attempt_resources(attempt, action, remote_mutation_started=True)

        action.status = "success"
        attempt.status = "success"
        settle_attempt_resources(attempt, action, remote_mutation_started=True)
        settle_attempt_resources(attempt, action, remote_mutation_started=True)

        lease = session.scalar(select(AccountPoolConcurrencyLease))
        reservation = session.scalar(select(AccountBehaviorBudgetReservation))
        fence = session.scalar(select(RemoteInvocationFence))
        ledger = session.scalar(select(AccountBehaviorBudgetLedger))
        assert lease.state == "released"
        assert reservation.state == "confirmed"
        assert fence.state == "terminal"
        assert ledger.counters["reaction"]["unknown"] == 0
        assert ledger.counters["reaction"]["confirmed"] == 1


def test_unknown_to_proven_absence_releases_budget_and_lease() -> None:
    with _session() as session:
        task = _seed(session)
        action, attempt = _attempt(session, task, 11)
        reserve_attempt_resources(session, action, attempt)
        mark_attempt_call_issued(session, attempt)
        action.status = "unknown_after_send"
        attempt.status = "result_unknown"
        settle_attempt_resources(attempt, action, remote_mutation_started=True)

        action.status = "failed"
        attempt.status = "failed"
        settle_attempt_resources(attempt, action, remote_mutation_started=False)

        lease = session.scalar(select(AccountPoolConcurrencyLease))
        reservation = session.scalar(select(AccountBehaviorBudgetReservation))
        fence = session.scalar(select(RemoteInvocationFence))
        ledger = session.scalar(select(AccountBehaviorBudgetLedger))
        assert lease.state == "released"
        assert reservation.state == "released"
        assert fence.business_outcome_state == "safely_not_called"
        assert ledger.counters["reaction"]["unknown"] == 0


def test_pre_gateway_probe_failure_releases_bulkhead_without_unknown() -> None:
    with _session() as session:
        task = _seed(session)
        action, attempt = _attempt(session, task, 11)
        reserve_attempt_resources(session, action, attempt)
        action.status = "pending"
        action.result = {
            "error_code": "conversation_remote_probe_failed",
            "error_message": "probe timeout",
        }

        dispatcher._settle_group_send_preflight_attempt(session, action, attempt)

        lease = session.scalar(select(AccountPoolConcurrencyLease))
        reservation = session.scalar(select(AccountBehaviorBudgetReservation))
        fence = session.scalar(select(RemoteInvocationFence))
        assert attempt.gateway_call_started_at is None
        assert attempt.status == "skipped_before_gateway"
        assert lease.state == "released"
        assert reservation.state == "released"
        assert fence.business_outcome_state == "safely_not_called"


def test_dispatch_timeout_moves_resources_to_unknown_without_losing_identity() -> None:
    with _session() as session:
        task = _seed(session)
        action, attempt = _attempt(session, task, 11)
        reserve_attempt_resources(session, action, attempt)
        mark_attempt_call_issued(session, attempt)
        attempt.gateway_call_started_at = _now()
        frozen = dict(attempt.result_snapshot)
        session.flush()

        dispatcher._mark_unknown_after_send(session, action, "gateway deadline")

        lease = session.scalar(select(AccountPoolConcurrencyLease))
        reservation = session.scalar(select(AccountBehaviorBudgetReservation))
        assert attempt.status == "result_unknown"
        assert lease.state == "remote_unknown"
        assert reservation.state == "unknown"
        assert attempt.result_snapshot["remote_invocation_fence_id"] == (
            frozen["remote_invocation_fence_id"]
        )
        assert attempt.result_snapshot["telegram_gateway_timeout_seconds"] == 10
        assert attempt.result_snapshot["telegram_connect_timeout_seconds"] == 5


def test_unconfirmed_timeout_keeps_physical_lease_until_reconciled() -> None:
    with _session() as session:
        task = _seed(session)
        action, attempt = _attempt(session, task, 11)
        reserve_attempt_resources(session, action, attempt)
        mark_attempt_call_issued(session, attempt)
        attempt.gateway_call_started_at = _now()

        dispatcher._mark_unknown_after_send(
            session,
            action,
            "gateway deadline",
            transport_termination_acknowledged=False,
        )

        lease = session.scalar(select(AccountPoolConcurrencyLease))
        fence = session.scalar(select(RemoteInvocationFence))
        assert lease.state == "remote_unknown"
        assert lease.released_at is None
        assert fence.transport_termination_state == "cancellation_unconfirmed"
        assert fence.cancellation_requested_at is not None
        assert fence.transport_terminated_at is None


def test_two_independent_proxy_unknowns_open_only_that_proxy_circuit() -> None:
    with _session() as session:
        task = _seed(session, task_limit=4, pool_limit=4, proxy_limit=4)
        session.add_all(
            [
                AccountProxy(id=1, tenant_id=1, name="proxy-1", port=10001),
                AccountProxy(id=2, tenant_id=1, name="proxy-2", port=10002),
                _account(14),
            ]
        )
        for account_id in (11, 12, 13):
            session.get(TgAccount, account_id).proxy_id = 1
        session.flush()
        session.get(TgAccount, 14).proxy_id = 2

        for account_id in (11, 12):
            action, attempt = _attempt(session, task, account_id)
            reserve_attempt_resources(session, action, attempt)
            mark_attempt_call_issued(session, attempt)
            action.status = "unknown_after_send"
            attempt.status = "result_unknown"
            attempt.failure_type = "gateway_timeout"
            settle_attempt_resources(attempt, action, remote_mutation_started=None)
            settle_attempt_resources(attempt, action, remote_mutation_started=None)

        with pytest.raises(RuntimeResourceBlocked, match="execution_circuit_open"):
            reserve_attempt_resources(session, *_attempt(session, task, 13))
        reserve_attempt_resources(session, *_attempt(session, task, 14))

        proxy_state = session.scalar(
            select(ExecutionCircuitState).where(
                ExecutionCircuitState.domain_kind == "proxy_route",
                ExecutionCircuitState.domain_key == "proxy:1",
            )
        )
        assert proxy_state is not None and proxy_state.state == "open"
        assert len(proxy_state.failure_times) == 2
