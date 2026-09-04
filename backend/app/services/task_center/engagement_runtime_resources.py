from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session, object_session

from app.models import (
    AccountBehaviorBudgetLedger,
    AccountBehaviorBudgetPolicyRevision,
    AccountBehaviorBudgetReservation,
    AccountExternalUseHold,
    AccountPoolConcurrencyLease,
    AccountPoolConcurrencyPolicyRevision,
    Action,
    ExecutionAttempt,
    ExecutionResiliencePolicyRevision,
    RemoteInvocationFence,
    Task,
    TaskAccountGroupBindingSetRevision,
    TaskDayLedger,
    TgAccount,
)
from app.services._common import _now
from app.timezone import as_beijing

from .engagement_binding import ENGAGEMENT_TASK_TYPES
from .engagement_action_classes import ACTION_CLASS_BY_TYPE
from .engagement_account_origin import (
    FrozenAccountOrigin,
    resolve_frozen_account_origin,
)
from .engagement_activity_scope import ActivityScope, action_activity_scope
from .engagement_runtime_domains import (
    ACTIVE_DOMAIN_LEASE_STATES,
    new_pool_lease,
    proxy_capacity_blocker,
    proxy_domain_keys,
)
from .engagement_runtime_circuit import circuit_blocker
from .engagement_runtime_settlement import (
    locked_ledger_by_id,
    move_counter,
    settle_resource_set,
)


ACTIVE_LEASE_STATES = ACTIVE_DOMAIN_LEASE_STATES
ACTIVE_FENCE_STATES = ("reserved", "active", "remote_unknown")
DEFAULT_RESOURCE_RETRY_SECONDS = 30
@dataclass(frozen=True)
class RuntimeResourceBlocked(Exception):
    code: str
    detail: str
    retry_after_seconds: int = DEFAULT_RESOURCE_RETRY_SECONDS


@dataclass(frozen=True)
class RuntimeResourceContext:
    account: TgAccount
    origin: FrozenAccountOrigin
    pool_policy: AccountPoolConcurrencyPolicyRevision
    resilience: ExecutionResiliencePolicyRevision
    ledger: AccountBehaviorBudgetLedger
    action_class: str
    proxy_route_key: str
    proxy_egress_key: str


def reserve_attempt_resources(
    session: Session, action: Action, attempt: ExecutionAttempt
) -> None:
    if not _uses_unified_engagement_contract(session, action):
        return
    context = _runtime_resource_context(session, action)
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


def _runtime_resource_context(
    session: Session,
    action: Action,
) -> RuntimeResourceContext:
    account = _locked_account(session, action)
    try:
        origin = resolve_frozen_account_origin(session, action, account)
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
        task_day=_task_day(session, action),
    )
    return RuntimeResourceContext(
        account=account,
        origin=origin,
        pool_policy=pool_policy,
        resilience=resilience,
        ledger=ledger,
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
    _assert_behavior_capacity(context.ledger, context.action_class)
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
    if action.task_type not in ENGAGEMENT_TASK_TYPES:
        return False
    task = session.get(Task, action.task_id)
    if task is None:
        raise RuntimeResourceBlocked("engagement_task_missing", "互动任务不存在")
    return (
        (task.type_config or {}).get("engagement_contract_version")
        == "unified_engagement_v1"
    )


def mark_attempt_call_issued(session: Session, attempt: ExecutionAttempt) -> None:
    lease, reservation, fence = _attempt_resources(session, attempt.id)
    if lease is None:
        return
    lease.state = "call_issued"
    reservation.state = "call_issued"
    fence.state = "active"
    fence.started_at = _now()
    ledger = locked_ledger_by_id(session, reservation.ledger_id)
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


def _assert_pool_capacity(
    session: Session,
    action: Action,
    *,
    account: TgAccount,
    binding: TaskAccountGroupBindingSetRevision,
    policy: AccountPoolConcurrencyPolicyRevision,
    pool_id: int,
) -> None:
    if pool_id not in {int(item) for item in binding.account_group_ids or []}:
        raise RuntimeResourceBlocked("account_outside_frozen_binding", "账号不属于冻结任务绑定")
    if _lease_count(session, pool_id, account_id=account.id) >= 1:
        raise RuntimeResourceBlocked("account_remote_inflight", "账号已有远端调用在途", 5)
    if _lease_count(session, pool_id) >= policy.hard_remote_inflight_limit:
        raise RuntimeResourceBlocked("account_pool_remote_inflight_full", "账号组物理并发已满", 5)
    task_count = _lease_count(session, pool_id, task_id=action.task_id)
    if task_count >= binding.concurrency_limit_per_group:
        raise RuntimeResourceBlocked("task_group_share_full", "任务在该账号组的并发份额已满", 5)


def _lease_count(
    session: Session,
    pool_id: int,
    *,
    account_id: int | None = None,
    task_id: str | None = None,
) -> int:
    query = select(func.count(AccountPoolConcurrencyLease.id)).where(
        AccountPoolConcurrencyLease.account_pool_id == pool_id,
        AccountPoolConcurrencyLease.state.in_(ACTIVE_LEASE_STATES),
    )
    if account_id is not None:
        query = query.where(AccountPoolConcurrencyLease.account_id == account_id)
    if task_id is not None:
        query = query.where(AccountPoolConcurrencyLease.task_id == task_id)
    return int(session.scalar(query) or 0)


def _assert_proxy_capacity(
    session: Session,
    action: Action,
    policy: ExecutionResiliencePolicyRevision,
    *,
    proxy_route_key: str,
    proxy_egress_key: str,
) -> None:
    blocker = proxy_capacity_blocker(
        session,
        tenant_id=action.tenant_id,
        policy=policy,
        route_key=proxy_route_key,
        egress_key=proxy_egress_key,
    )
    if blocker is not None:
        raise RuntimeResourceBlocked(blocker[0], blocker[1], 5)


def _assert_circuit_available(
    session: Session,
    action: Action,
    *,
    proxy_route_key: str,
    proxy_egress_key: str,
) -> None:
    blocker = circuit_blocker(
        session,
        tenant_id=action.tenant_id,
        account_id=int(action.account_id or 0),
        route_key=proxy_route_key,
        egress_key=proxy_egress_key,
    )
    if blocker is not None:
        raise RuntimeResourceBlocked(blocker[0], blocker[1], 30)


def _assert_negative_outcome_circuit(
    session: Session,
    action: Action,
    *,
    context: RuntimeResourceContext,
) -> None:
    from .negative_outcome_circuit import (
        NegativeOutcomeBlocked,
        assert_negative_outcome_circuit_clear,
    )

    scope = action_activity_scope(session, action)
    if action.task_type not in {"group_ai_chat", "channel_comment"}:
        return
    if not scope.canonical_peer_id:
        return
    try:
        assert_negative_outcome_circuit_clear(
            session,
            tenant_id=action.tenant_id,
            peer_id=scope.canonical_peer_id,
            account_id=context.account.id,
            route=action.task_type,
            action_kind=(
                "response" if (action.payload or {}).get("conversation_turn_claim_id")
                or ((action.payload or {}).get("comment_mode") == "reply"
                    and (action.payload or {}).get("reply_target_source") == "channel_comment")
                else "proactive"
            ),
        )
    except NegativeOutcomeBlocked as exc:
        raise RuntimeResourceBlocked(
            "negative_outcome_policy_blocked", exc.details or exc.reason, 30
        )


def _assert_behavior_capacity(
    ledger: AccountBehaviorBudgetLedger, action_class: str
) -> None:
    budgets = dict(ledger.action_budgets or {})
    limit = int(budgets.get(action_class) or 0)
    if limit <= 0:
        raise RuntimeResourceBlocked(
            "behavior_budget_action_class_unconfigured",
            f"行为预算未配置动作类型:{action_class}",
        )
    states = dict((ledger.counters or {}).get(action_class) or {})
    occupied_states = ("reserved", "call_issued", "unknown", "confirmed", "unowned")
    occupied = sum(int(states.get(key) or 0) for key in occupied_states)
    if occupied >= limit:
        raise RuntimeResourceBlocked(
            "account_behavior_budget_exhausted",
            f"账号当日 {action_class} 行为预算已满",
        )
    total_limit = int(
        budgets.get("total")
        or sum(int(value or 0) for key, value in budgets.items() if key != "total")
    )
    total_occupied = _total_behavior_occupancy(ledger)
    if total_occupied >= total_limit:
        raise RuntimeResourceBlocked(
            "account_behavior_total_budget_exhausted",
            "账号当日跨任务总行为预算已满",
        )

    # Active floor reserve: prevent passive likes/views from starving high-priority active conversation
    if action_class in ("passive_operation", "visible_reaction"):
        active_floor_reserve = int(budgets.get("authored_content") or 10)
        max_passive_allowed = max(0, total_limit - active_floor_reserve)
        if total_occupied >= max_passive_allowed:
            raise RuntimeResourceBlocked(
                "account_behavior_passive_budget_exhausted",
                f"被动操作已达安全水位({total_occupied}/{max_passive_allowed})，保留 {active_floor_reserve} 次额度供主动发言使用",
            )


def _assert_external_use_hold(
    session: Session,
    action: Action,
    *,
    account_id: int,
    action_class: str,
) -> None:
    holds = list(session.scalars(select(AccountExternalUseHold).where(
        AccountExternalUseHold.tenant_id == action.tenant_id,
        AccountExternalUseHold.account_id == account_id,
        AccountExternalUseHold.state == "active",
        AccountExternalUseHold.expires_at > _now(),
    ).order_by(AccountExternalUseHold.expires_at.desc())))
    if not holds:
        return
    scope = action_activity_scope(session, action)
    if not scope.canonical_peer_id:
        raise RuntimeResourceBlocked(
            "account_external_use_scope_unproven",
            "动作缺少 peer，无法证明不与账号外部使用冲突",
            30,
        )
    hold = next(
        (item for item in holds if _external_hold_collides(item, action_class, scope)),
        None,
    )
    if hold is None:
        return
    raise RuntimeResourceBlocked(
        "account_external_use_hold",
        f"账号刚发生未归属的 {action_class} 外发，自动化暂停至 {hold.expires_at.isoformat()}",
        30,
    )


def _external_hold_collides(
    hold: AccountExternalUseHold,
    action_class: str,
    scope: ActivityScope,
) -> bool:
    if action_class not in set(hold.collision_action_classes or []):
        return False
    if hold.canonical_peer_id != scope.canonical_peer_id:
        return False
    held_source = str(hold.canonical_source_identity or "")
    action_source = str(scope.canonical_source_identity or "")
    return not held_source or not action_source or held_source == action_source


def _total_behavior_occupancy(ledger: AccountBehaviorBudgetLedger) -> int:
    occupied_states = ("reserved", "call_issued", "unknown", "confirmed", "unowned")
    return sum(
        sum(int(states.get(state) or 0) for state in occupied_states)
        for states in (ledger.counters or {}).values()
        if isinstance(states, dict)
    )


def _assert_portfolio_capacity(
    session: Session,
    action: Action,
    ledger: AccountBehaviorBudgetLedger,
    *,
    account_id: int,
    action_class: str,
) -> None:
    from .engagement_portfolio import task_account_portfolio_allowance

    allowance = task_account_portfolio_allowance(
        session,
        task_id=action.task_id,
        task_day=ledger.task_day,
        account_id=account_id,
        action_class=action_class,
    )
    if allowance is None:
        return
    account_allowance, _task_total = allowance
    used = int(
        session.scalar(
            select(func.sum(AccountBehaviorBudgetReservation.amount)).where(
                AccountBehaviorBudgetReservation.ledger_id == ledger.id,
                AccountBehaviorBudgetReservation.task_id == action.task_id,
                AccountBehaviorBudgetReservation.action_class == action_class,
                AccountBehaviorBudgetReservation.state.in_(
                    ("reserved", "call_issued", "unknown", "confirmed")
                ),
            )
        )
        or 0
    )
    if used >= account_allowance:
        raise RuntimeResourceBlocked(
            "task_account_portfolio_capacity_exhausted",
            f"任务在账号 {account_id} 的 {action_class} 组合预算已满",
        )


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
        "telegram_gateway_timeout_seconds": int(
            resilience.telegram_gateway_timeout_seconds
        ),
        "telegram_connect_timeout_seconds": int(
            resilience.telegram_connect_timeout_seconds
        ),
    }


def _attempt_resources(session: Session, attempt_id: str):
    lease = session.scalar(
        select(AccountPoolConcurrencyLease)
        .where(AccountPoolConcurrencyLease.attempt_id == attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    reservation = session.scalar(
        select(AccountBehaviorBudgetReservation)
        .where(AccountBehaviorBudgetReservation.attempt_id == attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    fence = session.scalar(
        select(RemoteInvocationFence)
        .where(RemoteInvocationFence.attempt_id == attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if lease is None and reservation is None and fence is None:
        return None, None, None
    if lease is None or reservation is None or fence is None:
        raise RuntimeError("engagement_runtime_resource_set_incomplete")
    return lease, reservation, fence


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
