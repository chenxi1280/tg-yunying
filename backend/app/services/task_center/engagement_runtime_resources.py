from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app.models import (
    AccountBehaviorBudgetLedger,
    AccountBehaviorBudgetPolicyRevision,
    AccountBehaviorBudgetReservation,
    AccountPoolConcurrencyPolicyRevision,
    Action,
    ExecutionAttempt,
    ExecutionResiliencePolicyRevision,
    RemoteInvocationFence,
    TaskDayLedger,
    TgAccount,
)
from app.services._common import _now
from app.timezone import as_beijing

from .engagement_action_contract import action_uses_unified_contract
from .engagement_action_classes import ACTION_CLASS_BY_TYPE
from .engagement_legacy_resource_origin import (
    assert_legacy_attempt_uncalled, legacy_cutover_origin, legacy_original_task_day,
)
from .engagement_account_origin import (
    FrozenAccountOrigin,
    resolve_frozen_account_origin,
)
from .engagement_runtime_domains import (
    ACTIVE_DOMAIN_LEASE_STATES,
    new_pool_lease,
    proxy_domain_keys,
)
from .engagement_runtime_capacity import (
    _assert_behavior_capacity, _assert_circuit_available, _assert_external_use_hold,
    _assert_negative_outcome_circuit, _assert_pool_capacity, _assert_portfolio_capacity,
    _assert_proxy_capacity,
    _assert_call_activity_capacity, _assert_shared_account_capacity,
)
from .engagement_runtime_error import RuntimeResourceBlocked
from .engagement_runtime_settlement import (
    attempt_resources as _attempt_resources,
    locked_ledger_by_id,
    move_counter,
    settle_resource_set,
)
from .engagement_shared_usage import SharedUsageScope


ACTIVE_LEASE_STATES = ACTIVE_DOMAIN_LEASE_STATES
ACTIVE_FENCE_STATES = ("reserved", "active", "remote_unknown")
@dataclass(frozen=True)
class RuntimeResourceContext:
    account: TgAccount
    origin: FrozenAccountOrigin
    pool_policy: AccountPoolConcurrencyPolicyRevision
    resilience: ExecutionResiliencePolicyRevision
    ledger: AccountBehaviorBudgetLedger
    budget_policy: AccountBehaviorBudgetPolicyRevision
    action_class: str
    proxy_route_key: str
    proxy_egress_key: str


def reserve_attempt_resources(
    session: Session, action: Action, attempt: ExecutionAttempt
) -> None:
    context = _admitted_resource_context(session, action, attempt)
    if context is None:
        return
    session.flush()
    _assert_runtime_capacity(session, action, context)
    lease = new_pool_lease(
        action,
        attempt=attempt,
        account=context.account,
        binding=context.origin.binding,
        policy=context.pool_policy,
        pool_id=context.origin.account_pool_id,
        route_key=context.proxy_route_key,
        egress_key=context.proxy_egress_key,
    )
    reservation = _new_budget_reservation(
        action,
        attempt=attempt,
        ledger=context.ledger,
        action_class=context.action_class,
    )
    fence = _new_remote_fence(
        action,
        attempt=attempt,
        account=context.account,
        policy=context.resilience,
    )
    session.add_all([lease, reservation, fence])
    move_counter(
        context.ledger,
        context.action_class,
        old_state=None,
        new_state="reserved",
    )
    session.flush()
    _record_resource_ids(
        attempt,
        lease=lease,
        reservation=reservation,
        fence=fence,
        resilience=context.resilience,
        origin=context.origin,
    )


def _admitted_resource_context(session: Session, action: Action, attempt: ExecutionAttempt):
    try:
        if _uses_unified_engagement_contract(session, action):
            return _runtime_resource_context(session, action)
        origin = legacy_cutover_origin(session, action)
        if origin is None:
            return None
        assert_legacy_attempt_uncalled(action, attempt)
        task_day = legacy_original_task_day(session, action)
        return _runtime_resource_context(session, action, origin=origin, task_day=task_day)
    except ValueError as exc:
        raise RuntimeResourceBlocked(str(exc), "存量动作的原资源归属无法核对") from exc


def _runtime_resource_context(
    session: Session,
    action: Action,
    *,
    origin: FrozenAccountOrigin | None = None,
    task_day: date | None = None,
) -> RuntimeResourceContext:
    account = _locked_account(session, action)
    try:
        origin = origin or resolve_frozen_account_origin(session, action, account)
    except ValueError as exc:
        raise RuntimeResourceBlocked(str(exc), "账号缺少可证明的冻结分组归属") from exc
    pool_policy = _active_pool_policy(
        session, action.tenant_id, origin.account_pool_id
    )
    budget_policy = _active_budget_policy(
        session, action.tenant_id, account.account_identity
    )
    resilience = _active_resilience_policy(session, action.tenant_id)
    proxy_route_key, proxy_egress_key = proxy_domain_keys(session, account)
    action_class = _action_class(action)
    ledger = _locked_budget_ledger(
        session,
        action,
        account=account,
        policy=budget_policy,
        task_day=task_day or _task_day(session, action),
    )
    return RuntimeResourceContext(
        account=account,
        origin=origin,
        pool_policy=pool_policy,
        resilience=resilience,
        ledger=ledger,
        budget_policy=budget_policy,
        action_class=action_class,
        proxy_route_key=proxy_route_key,
        proxy_egress_key=proxy_egress_key,
    )


def _assert_runtime_capacity(
    session: Session,
    action: Action,
    context: RuntimeResourceContext,
) -> None:
    _assert_pool_capacity(
        session,
        action,
        account=context.account,
        binding=context.origin.binding,
        policy=context.pool_policy,
        pool_id=context.origin.account_pool_id,
    )
    _assert_proxy_capacity(
        session,
        action,
        context.resilience,
        proxy_route_key=context.proxy_route_key,
        proxy_egress_key=context.proxy_egress_key,
    )
    _assert_shared_account_capacity(session, action, context)
    _assert_external_use_hold(
        session,
        action,
        account_id=context.account.id,
        action_class=context.action_class,
    )
    _assert_portfolio_capacity(
        session,
        action,
        context.ledger,
        account_id=context.account.id,
        action_class=context.action_class,
    )
    _assert_circuit_available(
        session,
        action,
        proxy_route_key=context.proxy_route_key,
        proxy_egress_key=context.proxy_egress_key,
    )
    _assert_negative_outcome_circuit(
        session,
        action,
        context=context,
    )


def _uses_unified_engagement_contract(session: Session, action: Action) -> bool:
    try:
        return action_uses_unified_contract(session, action)
    except ValueError as exc:
        raise RuntimeResourceBlocked(str(exc), "互动动作的原运行合同无法核对") from exc


def mark_attempt_call_issued(
    session: Session, attempt: ExecutionAttempt, *, call_started_at: datetime | None = None,
) -> None:
    session.flush()
    lease, reservation, fence = _attempt_resources(session, attempt.id)
    if lease is None:
        return
    if (attempt.gateway_call_started_at is not None or attempt.status != "before_call"
            or (lease.state, reservation.state, fence.state) != ("reserved", "reserved", "reserved")):
        raise RuntimeResourceBlocked("engagement_attempt_already_called", "原调用已开始或预约已结算，禁止再次发起")
    call_started_at = call_started_at or _now()
    action = session.get(Action, attempt.action_id)
    account = _locked_account(session, action)
    policy = _active_budget_policy(session, attempt.tenant_id, account.account_identity)
    ledger = locked_ledger_by_id(session, reservation.ledger_id)
    scope = SharedUsageScope(attempt.tenant_id, account.id, ledger.task_day,
        as_beijing(call_started_at).date())
    _assert_call_activity_capacity(session, scope, policy, reservation=reservation)
    lease.state = "call_issued"
    reservation.state = "call_issued"
    fence.state = "active"
    fence.started_at = call_started_at
    attempt.gateway_call_started_at = call_started_at
    move_counter(
        ledger,
        reservation.action_class,
        old_state="reserved",
        new_state="call_issued",
    )


def settle_attempt_resources(
    attempt: ExecutionAttempt,
    action: Action,
    *,
    remote_mutation_started: bool | None,
) -> None:
    session = object_session(attempt)
    if session is None:
        raise RuntimeError("engagement_resource_settlement_requires_session")
    session.flush()
    lease, reservation, fence = _attempt_resources(session, attempt.id)
    if lease is None:
        return
    settle_resource_set(
        session,
        attempt,
        action,
        lease=lease,
        reservation=reservation,
        fence=fence,
        remote_mutation_started=remote_mutation_started,
    )


def _locked_account(session: Session, action: Action) -> TgAccount:
    account = session.scalar(
        select(TgAccount)
        .where(TgAccount.id == action.account_id, TgAccount.tenant_id == action.tenant_id)
        .with_for_update()
    )
    if account is None or account.pool_id is None:
        raise RuntimeResourceBlocked("engagement_account_pool_missing", "账号未归属绑定分组")
    return account


def _active_pool_policy(
    session: Session, tenant_id: int, pool_id: int
) -> AccountPoolConcurrencyPolicyRevision:
    policy = session.scalar(
        select(AccountPoolConcurrencyPolicyRevision)
        .where(
            AccountPoolConcurrencyPolicyRevision.tenant_id == tenant_id,
            AccountPoolConcurrencyPolicyRevision.account_pool_id == pool_id,
            AccountPoolConcurrencyPolicyRevision.state == "active",
        )
        .with_for_update()
    )
    if policy is None:
        from .engagement_runtime_policy import ensure_pool_policy

        policy = ensure_pool_policy(session, tenant_id, pool_id)
    return policy


def _active_budget_policy(
    session: Session, tenant_id: int, account_class: str
) -> AccountBehaviorBudgetPolicyRevision:
    normalized_class = account_class or "normal"
    policy = session.scalar(
        select(AccountBehaviorBudgetPolicyRevision).where(
            AccountBehaviorBudgetPolicyRevision.tenant_id == tenant_id,
            AccountBehaviorBudgetPolicyRevision.account_class == normalized_class,
            AccountBehaviorBudgetPolicyRevision.state == "active",
        )
    )
    if policy is None and normalized_class != "normal":
        policy = session.scalar(
            select(AccountBehaviorBudgetPolicyRevision).where(
                AccountBehaviorBudgetPolicyRevision.tenant_id == tenant_id,
                AccountBehaviorBudgetPolicyRevision.account_class == "normal",
                AccountBehaviorBudgetPolicyRevision.state == "active",
            )
        )
    if policy is None:
        from .engagement_runtime_policy import ensure_behavior_policy

        policy = ensure_behavior_policy(session, tenant_id, normalized_class)
    return policy


def _active_resilience_policy(
    session: Session, tenant_id: int
) -> ExecutionResiliencePolicyRevision:
    policy = session.scalar(
        select(ExecutionResiliencePolicyRevision).where(
            ExecutionResiliencePolicyRevision.tenant_id == tenant_id,
            ExecutionResiliencePolicyRevision.state == "active",
        ).with_for_update()
    )
    if policy is None:
        from .engagement_runtime_policy import ensure_resilience_policy

        policy = ensure_resilience_policy(session, tenant_id)
    return policy


def _locked_budget_ledger(
    session: Session,
    action: Action,
    *,
    account: TgAccount,
    policy: AccountBehaviorBudgetPolicyRevision,
    task_day: date,
) -> AccountBehaviorBudgetLedger:
    ledger = session.scalar(
        select(AccountBehaviorBudgetLedger)
        .where(
            AccountBehaviorBudgetLedger.tenant_id == action.tenant_id,
            AccountBehaviorBudgetLedger.account_id == account.id,
            AccountBehaviorBudgetLedger.task_day == task_day,
        )
        .with_for_update()
    )
    if ledger is not None:
        return ledger
    ledger = AccountBehaviorBudgetLedger(
        tenant_id=action.tenant_id,
        account_id=account.id,
        task_day=task_day,
        policy_revision_id=policy.id,
        action_budgets=dict(policy.action_budgets or {}),
        counters={},
    )
    session.add(ledger)
    session.flush()
    return ledger


def _task_day(session: Session, action: Action) -> date:
    ledger_id = str((action.payload or {}).get("task_day_ledger_id") or "")
    ledger = session.get(TaskDayLedger, ledger_id) if ledger_id else None
    if ledger is not None:
        return ledger.obligation_local_date
    return as_beijing(_now()).date()


def _action_class(action: Action) -> str:
    action_class = ACTION_CLASS_BY_TYPE.get(action.action_type)
    if action_class is None:
        raise RuntimeResourceBlocked("engagement_action_class_unsupported", f"不支持的互动动作:{action.action_type}")
    return action_class


def _new_budget_reservation(
    action,
    *,
    attempt,
    ledger,
    action_class,
):
    return AccountBehaviorBudgetReservation(
        ledger_id=ledger.id,
        task_id=action.task_id,
        action_id=action.id,
        attempt_id=attempt.id,
        action_class=action_class,
    )


def _new_remote_fence(
    action,
    *,
    attempt,
    account,
    policy,
):
    return RemoteInvocationFence(
        tenant_id=action.tenant_id,
        action_id=action.id,
        attempt_id=attempt.id,
        invocation_identity=attempt.id,
        invocation_kind="telegram_gateway",
        domain_keys={"account_id": account.id, "proxy_id": account.proxy_id},
        resilience_policy_revision_id=policy.id,
    )


def _record_resource_ids(
    attempt,
    *,
    lease,
    reservation,
    fence,
    resilience,
    origin,
) -> None:
    attempt.result_snapshot = {
        **(attempt.result_snapshot or {}),
        "account_pool_concurrency_lease_id": lease.id,
        "account_behavior_budget_reservation_id": reservation.id,
        "remote_invocation_fence_id": fence.id,
        "engagement_account_pool_id": origin.account_pool_id,
        "engagement_account_pool_provenance": origin.provenance,
        "engagement_participation_plan_id": origin.participation_plan_id,
        "engagement_binding_set_revision_id": origin.binding.id,
        "engagement_membership_snapshot_set_id": origin.membership_snapshot_set_id,
        "telegram_gateway_timeout_seconds": int(
            resilience.telegram_gateway_timeout_seconds
        ),
        "telegram_connect_timeout_seconds": int(
            resilience.telegram_connect_timeout_seconds
        ),
    }


def recover_stale_concurrency_leases(
    session: Session, limit: int = 100
) -> int:
    from .engagement_lease_recovery import recover_settleable_leases

    return recover_settleable_leases(session, limit=limit, settle=settle_attempt_resources)


__all__ = [
    "RuntimeResourceBlocked",
    "mark_attempt_call_issued",
    "recover_stale_concurrency_leases",
    "reserve_attempt_resources",
    "settle_attempt_resources",
]
