from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountBehaviorBudgetPolicyRevision,
    AccountPoolConcurrencyPolicyRevision,
    ExecutionResiliencePolicyRevision,
    ExternalAccountUsePolicyRevision,
    ManagedPresencePolicyRevision,
)

from .engagement_binding import EngagementBindingSpec
from .engagement_policy_initialization import ensure_runtime_policy


DEFAULT_POOL_REMOTE_INFLIGHT_LIMIT = 5
DEFAULT_ACTION_BUDGETS = {
    "total": 60,
    "authored_message": 10,
    "authored_comment": 10,
    "reaction": 50,
    "view": 20,
}
DEFAULT_SESSION_BUDGET = {
    "min_sessions": 2,
    "max_sessions": 4,
    "min_minutes": 15,
    "max_minutes": 45,
}
DEFAULT_PAIR_GAP_POLICY = {
    "authored_to_authored_seconds": 300,
    "passive_to_authored_seconds": 300,
}
DEFAULT_EXTERNAL_USE_HOLD_SECONDS = {
    "authored_message": 600,
    "authored_comment": 600,
    "reaction": 300,
}
DEFAULT_EXTERNAL_USE_COLLISIONS = {
    "authored_message": ["authored_message", "reaction"],
    "authored_comment": ["authored_comment", "reaction"],
    "reaction": ["authored_message", "authored_comment", "reaction"],
}


def ensure_engagement_runtime_policies(
    session: Session,
    *,
    tenant_id: int,
    binding: EngagementBindingSpec,
) -> None:
    _ensure_resilience_policy(session, tenant_id)
    _ensure_behavior_policy(session, tenant_id)
    _ensure_presence_policy(session, tenant_id)
    _ensure_external_use_policy(session, tenant_id)
    _ensure_visibility_policy(session, tenant_id)
    for pool_id in binding.group_ids:
        policy = _ensure_pool_policy(session, tenant_id, pool_id)
        _ensure_fleet_policy(session, tenant_id, pool_id)
        if binding.concurrency_limit_per_group > policy.hard_remote_inflight_limit:
            raise ValueError(
                f"task_group_concurrency_exceeds_pool_limit:{pool_id}:"
                f"{binding.concurrency_limit_per_group}>{policy.hard_remote_inflight_limit}"
            )


def ensure_resilience_policy(
    session: Session, tenant_id: int
) -> ExecutionResiliencePolicyRevision:
    return ensure_runtime_policy(session, ExecutionResiliencePolicyRevision,
        scope={"tenant_id": tenant_id}, defaults={})


_ensure_resilience_policy = ensure_resilience_policy


def ensure_behavior_policy(
    session: Session, tenant_id: int, account_class: str = "normal"
) -> AccountBehaviorBudgetPolicyRevision:
    normalized_class = account_class or "normal"
    return ensure_runtime_policy(session, AccountBehaviorBudgetPolicyRevision,
        scope={"tenant_id": tenant_id, "account_class": normalized_class}, defaults={
            "action_budgets": dict(DEFAULT_ACTION_BUDGETS), "session_budget": dict(DEFAULT_SESSION_BUDGET),
            "wake_budget": 2, "pair_gap_policy": dict(DEFAULT_PAIR_GAP_POLICY),
        })


_ensure_behavior_policy = ensure_behavior_policy


def ensure_pool_policy(
    session: Session, tenant_id: int, pool_id: int
) -> AccountPoolConcurrencyPolicyRevision:
    return ensure_runtime_policy(session, AccountPoolConcurrencyPolicyRevision,
        scope={"tenant_id": tenant_id, "account_pool_id": pool_id}, defaults={
            "hard_remote_inflight_limit": DEFAULT_POOL_REMOTE_INFLIGHT_LIMIT,
            "workload_policy": {"task_contention_base_cap_bps": 3000},
        })


_ensure_pool_policy = ensure_pool_policy


def _ensure_presence_policy(
    session: Session,
    tenant_id: int,
) -> ManagedPresencePolicyRevision:
    policy = session.scalar(
        select(ManagedPresencePolicyRevision).where(
            ManagedPresencePolicyRevision.tenant_id == tenant_id,
            ManagedPresencePolicyRevision.state == "active",
        )
    )
    if policy is not None:
        return policy
    policy = ManagedPresencePolicyRevision(tenant_id=tenant_id)
    session.add(policy)
    session.flush()
    return policy


def _ensure_external_use_policy(
    session: Session,
    tenant_id: int,
) -> ExternalAccountUsePolicyRevision:
    policy = session.scalar(select(ExternalAccountUsePolicyRevision).where(
        ExternalAccountUsePolicyRevision.tenant_id == tenant_id,
        ExternalAccountUsePolicyRevision.state == "active",
    ))
    if policy is not None:
        return policy
    policy = ExternalAccountUsePolicyRevision(
        tenant_id=tenant_id,
        hold_seconds_by_class=dict(DEFAULT_EXTERNAL_USE_HOLD_SECONDS),
        collision_classes_by_class=dict(DEFAULT_EXTERNAL_USE_COLLISIONS),
    )
    session.add(policy)
    session.flush()
    return policy


def _ensure_visibility_policy(session: Session, tenant_id: int) -> None:
    from .post_send_visibility import ensure_visibility_policy

    ensure_visibility_policy(session, tenant_id)


def _ensure_fleet_policy(session: Session, tenant_id: int, pool_id: int) -> None:
    from .engagement_fleet_activity import ensure_fleet_activity_policy

    ensure_fleet_activity_policy(
        session,
        tenant_id=tenant_id,
        account_pool_id=pool_id,
    )


__all__ = [
    "DEFAULT_ACTION_BUDGETS",
    "DEFAULT_POOL_REMOTE_INFLIGHT_LIMIT",
    "ensure_behavior_policy",
    "ensure_engagement_runtime_policies",
    "ensure_pool_policy",
    "ensure_resilience_policy",
]
