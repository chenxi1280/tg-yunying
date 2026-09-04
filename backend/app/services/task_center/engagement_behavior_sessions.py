from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountBehaviorBudgetLedger,
    AccountBehaviorBudgetPolicyRevision,
    AccountBehaviorSessionPlan,
    AccountPacingReservation,
    Task,
    TgAccount,
)
from app.timezone import as_beijing


UNIFIED_ENGAGEMENT_CONTRACT = "unified_engagement_v1"
MIN_SESSION_COUNT = 2
MAX_SESSION_COUNT = 4
MIN_SESSION_MINUTES = 15
MAX_SESSION_MINUTES = 45
DEFAULT_MIN_SESSIONS = 2
DEFAULT_MAX_SESSIONS = 4
DEFAULT_MIN_MINUTES = 15
DEFAULT_MAX_MINUTES = 45
CHRONOTYPES = (
    ("morning", 7, 22),
    ("balanced", 9, 23),
    ("evening", 11, 24),
)


def behavior_session_not_before(
    session: Session,
    *,
    task_id: str | None = None,
    tenant_id: int | None = None,
    engagement_contract_version: str | None = None,
    account_id: int,
    desired_at: datetime,
    deadline_at: datetime | None,
) -> datetime | None:
    desired_at = _wall(desired_at)
    if desired_at is None:
        raise ValueError("behavior_session_desired_at_required")
    contract_version, resolved_tenant_id = _resolve_contract(
        session,
        task_id=task_id,
        tenant_id=tenant_id,
        engagement_contract_version=engagement_contract_version,
    )
    if contract_version != UNIFIED_ENGAGEMENT_CONTRACT:
        return desired_at
    deadline = _wall(deadline_at)
    for task_day in _candidate_task_days(desired_at, deadline):
        plan = ensure_behavior_session_plan(
            session,
            tenant_id=resolved_tenant_id,
            account_id=account_id,
            task_day=task_day,
        )
        candidate = _window_not_before(plan, desired_at, deadline)
        if candidate is not None:
            return candidate
    return None


def _candidate_task_days(desired_at: datetime, deadline: datetime | None) -> tuple[date, ...]:
    current = desired_at.date()
    next_day = current + timedelta(days=1)
    if deadline is not None and deadline.date() <= current:
        return (current,)
    return current, next_day


def _window_not_before(
    plan: AccountBehaviorSessionPlan,
    desired_at: datetime,
    deadline: datetime | None,
) -> datetime | None:
    for item in plan.windows or []:
        start = datetime.fromisoformat(str(item["start_at"]))
        end = datetime.fromisoformat(str(item["end_at"]))
        if start <= desired_at < end:
            return desired_at
        if desired_at < start and (deadline is None or start < deadline):
            return start
    return None


def ensure_behavior_session_plan(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    task_day: date,
) -> AccountBehaviorSessionPlan:
    existing = session.scalar(
        select(AccountBehaviorSessionPlan).where(
            AccountBehaviorSessionPlan.tenant_id == tenant_id,
            AccountBehaviorSessionPlan.account_id == account_id,
            AccountBehaviorSessionPlan.task_day == task_day,
            AccountBehaviorSessionPlan.state == "active",
        )
    )
    if existing is not None:
        return existing
    account = session.get(TgAccount, account_id)
    if account is None or account.tenant_id != tenant_id:
        raise RuntimeError("behavior_session_account_missing")
    policy = _policy(session, tenant_id, account.account_identity)
    seed = _seed(tenant_id, account_id, task_day, policy.id)
    chronotype, awake_start, awake_end = CHRONOTYPES[
        _sample(seed, "chronotype", len(CHRONOTYPES))
    ]
    session_count = _session_count(policy, seed)
    windows = _windows(
        task_day,
        seed,
        session_count=session_count,
        awake_start=awake_start,
        awake_end=awake_end,
        policy=policy,
    )
    plan = AccountBehaviorSessionPlan(
        tenant_id=tenant_id,
        account_id=account_id,
        task_day=task_day,
        policy_revision_id=policy.id,
        chronotype=chronotype,
        weekday_class="weekend" if task_day.weekday() >= 5 else "weekday",
        windows=windows,
        visible_action_capacity=dict(policy.action_budgets or {}),
        rest_debt=0,
        wake_policy={"daily_limit": int(policy.wake_budget or 0)},
        seed=seed,
    )
    session.add(plan)
    session.flush()
    return plan


def reserve_behavior_session_wake(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    desired_at: datetime,
) -> bool:
    desired = _wall(desired_at)
    if desired is None:
        raise ValueError("behavior_session_wake_desired_at_required")
    plan = ensure_behavior_session_plan(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        task_day=desired.date(),
    )
    if _inside_window(plan, desired):
        return False
    policy = session.get(AccountBehaviorBudgetPolicyRevision, plan.policy_revision_id)
    if policy is None or int(policy.wake_budget or 0) <= 0:
        return False
    ledger = _wake_ledger(session, plan, policy)
    if int(ledger.wake_count or 0) >= int(policy.wake_budget):
        return False
    ledger.wake_count = int(ledger.wake_count or 0) + 1
    ledger.version = int(ledger.version or 1) + 1
    return True


def behavior_session_wake_available(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    desired_at: datetime,
    pending_policy_version: str,
) -> bool:
    desired = _wall(desired_at)
    if desired is None:
        raise ValueError("behavior_session_wake_desired_at_required")
    plan = ensure_behavior_session_plan(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        task_day=desired.date(),
    )
    if _inside_window(plan, desired):
        return False
    policy = session.get(AccountBehaviorBudgetPolicyRevision, plan.policy_revision_id)
    if policy is None or int(policy.wake_budget or 0) <= 0:
        return False
    ledger = _existing_wake_ledger(session, plan)
    consumed = int(ledger.wake_count or 0) if ledger else 0
    pending = int(session.scalar(
        select(func.count(AccountPacingReservation.id)).where(
            AccountPacingReservation.tenant_id == tenant_id,
            AccountPacingReservation.account_id == account_id,
            AccountPacingReservation.policy_version == pending_policy_version,
            AccountPacingReservation.state.in_(("reserved", "bound")),
        )
    ) or 0)
    return consumed + pending < int(policy.wake_budget)


def _inside_window(plan: AccountBehaviorSessionPlan, desired_at: datetime) -> bool:
    return any(
        datetime.fromisoformat(str(item["start_at"]))
        <= desired_at
        < datetime.fromisoformat(str(item["end_at"]))
        for item in plan.windows or []
    )


def _wake_ledger(
    session: Session,
    plan: AccountBehaviorSessionPlan,
    policy: AccountBehaviorBudgetPolicyRevision,
) -> AccountBehaviorBudgetLedger:
    statement = select(AccountBehaviorBudgetLedger).where(
        AccountBehaviorBudgetLedger.tenant_id == plan.tenant_id,
        AccountBehaviorBudgetLedger.account_id == plan.account_id,
        AccountBehaviorBudgetLedger.task_day == plan.task_day,
    )
    if session.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update()
    ledger = session.scalar(statement)
    if ledger is not None:
        return ledger
    ledger = AccountBehaviorBudgetLedger(
        tenant_id=plan.tenant_id,
        account_id=plan.account_id,
        task_day=plan.task_day,
        policy_revision_id=policy.id,
        action_budgets=dict(policy.action_budgets or {}),
        counters={},
    )
    session.add(ledger)
    session.flush()
    return ledger


def _existing_wake_ledger(
    session: Session,
    plan: AccountBehaviorSessionPlan,
) -> AccountBehaviorBudgetLedger | None:
    return session.scalar(select(AccountBehaviorBudgetLedger).where(
        AccountBehaviorBudgetLedger.tenant_id == plan.tenant_id,
        AccountBehaviorBudgetLedger.account_id == plan.account_id,
        AccountBehaviorBudgetLedger.task_day == plan.task_day,
    ))


def _resolve_contract(
    session: Session,
    *,
    task_id: str | None,
    tenant_id: int | None,
    engagement_contract_version: str | None,
) -> tuple[str, int]:
    if engagement_contract_version is not None:
        if tenant_id is None:
            raise ValueError("behavior_session_tenant_id_required")
        return engagement_contract_version, tenant_id
    task = session.get(Task, task_id) if task_id else None
    if task is None:
        raise ValueError("behavior_session_task_missing")
    return str((task.type_config or {}).get("engagement_contract_version") or ""), task.tenant_id


def _policy(
    session: Session,
    tenant_id: int,
    account_class: str,
) -> AccountBehaviorBudgetPolicyRevision:
    policy = session.scalar(
        select(AccountBehaviorBudgetPolicyRevision).where(
            AccountBehaviorBudgetPolicyRevision.tenant_id == tenant_id,
            AccountBehaviorBudgetPolicyRevision.account_class == account_class,
            AccountBehaviorBudgetPolicyRevision.state == "active",
        )
    )
    if policy is None:
        raise RuntimeError("account_behavior_budget_policy_missing")
    return policy


def _session_count(
    policy: AccountBehaviorBudgetPolicyRevision,
    seed: str,
) -> int:
    config = dict(policy.session_budget or {})
    minimum = max(
        MIN_SESSION_COUNT,
        int(config.get("min_sessions", DEFAULT_MIN_SESSIONS)),
    )
    maximum = min(
        MAX_SESSION_COUNT,
        int(config.get("max_sessions", DEFAULT_MAX_SESSIONS)),
    )
    if minimum > maximum:
        raise RuntimeError("account_behavior_session_policy_invalid")
    return minimum + _sample(seed, "session-count", maximum - minimum + 1)


def _windows(
    task_day: date,
    seed: str,
    *,
    session_count: int,
    awake_start: int,
    awake_end: int,
    policy: AccountBehaviorBudgetPolicyRevision,
) -> list[dict]:
    config = dict(policy.session_budget or {})
    minimum = max(
        MIN_SESSION_MINUTES,
        int(config.get("min_minutes", DEFAULT_MIN_MINUTES)),
    )
    maximum = min(
        MAX_SESSION_MINUTES,
        int(config.get("max_minutes", DEFAULT_MAX_MINUTES)),
    )
    if minimum > maximum:
        raise RuntimeError("account_behavior_session_duration_policy_invalid")
    day_start = datetime.combine(task_day, datetime.min.time())
    awake_minutes = (awake_end - awake_start) * 60
    segment_minutes = awake_minutes // session_count
    result = []
    for index in range(session_count):
        duration = minimum + _sample(
            seed,
            f"duration:{index}",
            maximum - minimum + 1,
        )
        slack = max(1, segment_minutes - duration)
        offset = _sample(seed, f"offset:{index}", slack)
        start = day_start + timedelta(
            hours=awake_start,
            minutes=index * segment_minutes + offset,
        )
        result.append(
            {
                "ordinal": index + 1,
                "start_at": start.isoformat(),
                "end_at": (start + timedelta(minutes=duration)).isoformat(),
                "capacity": dict(policy.action_budgets or {}),
            }
        )
    return result


def _seed(tenant_id: int, account_id: int, task_day: date, policy_id: str) -> str:
    raw = f"{tenant_id}:{account_id}:{task_day.isoformat()}:{policy_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _sample(seed: str, purpose: str, size: int) -> int:
    if size <= 1:
        return 0
    digest = hashlib.sha256(f"{seed}:{purpose}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % size


def _wall(value: datetime | None) -> datetime | None:
    return as_beijing(value)


__all__ = [
    "behavior_session_not_before",
    "behavior_session_wake_available",
    "ensure_behavior_session_plan",
    "reserve_behavior_session_wake",
]
