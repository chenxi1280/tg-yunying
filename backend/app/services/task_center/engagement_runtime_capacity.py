from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountBehaviorBudgetLedger, AccountBehaviorBudgetReservation, AccountExternalUseHold,
    AccountPoolConcurrencyLease, AccountPoolConcurrencyPolicyRevision, Action,
    ExecutionResiliencePolicyRevision, TaskAccountGroupBindingSetRevision, TgAccount,
)
from app.services._common import _now
from app.timezone import as_beijing

from .engagement_activity_scope import ActivityScope, action_activity_scope
from .engagement_runtime_circuit import circuit_blocker
from .engagement_runtime_domains import ACTIVE_DOMAIN_LEASE_STATES, proxy_capacity_blocker
from .engagement_runtime_error import RuntimeResourceBlocked
from .engagement_shared_usage import (
    SharedUsageScope, activity_budget_occupancy, activity_budget_source,
    assert_shared_evidence, behavior_occupancy, original_budget_occupancy,
    read_shared_account_usage,
)

if TYPE_CHECKING:
    from .engagement_runtime_resources import RuntimeResourceContext

ACTIVE_LEASE_STATES = ACTIVE_DOMAIN_LEASE_STATES


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
    if _account_lease_count(session, action.tenant_id, account.id) >= 1:
        raise RuntimeResourceBlocked("account_remote_inflight", "账号已有远端调用在途", 5)
    if _lease_count(session, pool_id) >= policy.hard_remote_inflight_limit:
        raise RuntimeResourceBlocked("account_pool_remote_inflight_full", "账号组物理并发已满", 5)
    task_count = _lease_count(session, pool_id, task_id=action.task_id)
    if task_count >= binding.concurrency_limit_per_group:
        raise RuntimeResourceBlocked("task_group_share_full", "任务在该账号组的并发份额已满", 5)


def _account_lease_count(session, tenant_id, account_id):
    return int(session.scalar(select(func.count(AccountPoolConcurrencyLease.id)).where(
        AccountPoolConcurrencyLease.tenant_id == tenant_id,
        AccountPoolConcurrencyLease.account_id == account_id,
        AccountPoolConcurrencyLease.state.in_(ACTIVE_LEASE_STATES),
    )) or 0)


def _lease_count(
    session: Session,
    pool_id: int,
    *,
    task_id: str | None = None,
) -> int:
    query = select(func.count(AccountPoolConcurrencyLease.id)).where(
        AccountPoolConcurrencyLease.account_pool_id == pool_id,
        AccountPoolConcurrencyLease.state.in_(ACTIVE_LEASE_STATES),
    )
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
    ledger: AccountBehaviorBudgetLedger, action_class: str, *, occupied_by_class=None,
) -> None:
    budgets = dict(ledger.action_budgets or {})
    limit = int(budgets.get(action_class) or 0)
    if limit <= 0:
        raise RuntimeResourceBlocked(
            "behavior_budget_action_class_unconfigured",
            f"行为预算未配置动作类型:{action_class}",
        )
    occupancy = behavior_occupancy(ledger) if occupied_by_class is None else occupied_by_class
    occupied = int(occupancy.get(action_class) or 0)
    if occupied >= limit:
        raise RuntimeResourceBlocked(
            "account_behavior_budget_exhausted",
            f"账号当日 {action_class} 行为预算已满",
        )
    total_limit = int(
        budgets.get("total")
        or sum(int(value or 0) for key, value in budgets.items() if key != "total")
    )
    total_occupied = sum(occupancy.values())
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


def _assert_shared_account_capacity(session, action, context):
    scope = SharedUsageScope(action.tenant_id, context.account.id,
        context.ledger.task_day, as_beijing(_now()).date())
    usage = read_shared_account_usage(session, scope)
    assert_shared_evidence(usage)
    _assert_behavior_capacity(context.ledger, context.action_class,
        occupied_by_class=original_budget_occupancy(context.ledger, usage))
    source = activity_budget_source(session, scope, context.budget_policy)
    _assert_behavior_capacity(source, context.action_class,
        occupied_by_class=activity_budget_occupancy(source, usage))


def _assert_call_activity_capacity(session, scope, policy, *, reservation):
    usage = read_shared_account_usage(session, scope, excluded_attempt_id=reservation.attempt_id)
    assert_shared_evidence(usage)
    source = activity_budget_source(session, scope, policy)
    _assert_behavior_capacity(source, reservation.action_class,
        occupied_by_class=activity_budget_occupancy(source, usage))


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
