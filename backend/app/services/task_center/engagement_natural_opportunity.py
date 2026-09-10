from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ManagedPresencePlan,
    ManagedPresencePolicyRevision,
    NaturalOpportunitySupplyPlanRevision,
    Task,
    TaskDayLedger,
    TgGroup,
)


from .managed_presence_queries import PresenceAction, _external_turns, _managed_actions, _unowned_authored


SAFE_TERMINAL_ACTION_STATES = frozenset({"failed", "skipped", "cancelled"})


@dataclass(frozen=True)
class NaturalOpportunityDecision:
    plan: NaturalOpportunitySupplyPlanRevision
    presence: ManagedPresencePlan
    guaranteed_now_capacity: int
    deficit: int


def ensure_natural_opportunity_plan(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    group: TgGroup,
    required_units: int,
) -> NaturalOpportunityDecision:
    policy, presence = ensure_managed_presence_plan(
        session, task, ledger, group=group,
    )
    plan = _upsert_opportunity_plan(
        session, task, ledger, group=group, presence=presence,
        required_units=max(0, int(required_units)),
    )
    _project_task(task, plan, presence)
    return NaturalOpportunityDecision(
        plan=plan,
        presence=presence,
        guaranteed_now_capacity=int(presence.remaining_capacity),
        deficit=int(plan.deficit),
    )


def ensure_managed_presence_plan(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    group: TgGroup,
) -> tuple[ManagedPresencePolicyRevision, ManagedPresencePlan]:
    policy = _presence_policy(session, task.tenant_id)
    evidence = _presence_evidence(
        session, task, ledger, group=group, policy=policy,
    )
    presence = _upsert_presence_plan(
        session, task, ledger, group=group, policy=policy, evidence=evidence,
    )
    return policy, presence


def _presence_evidence(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    group: TgGroup,
    policy: ManagedPresencePolicyRevision,
) -> dict:
    turns = _external_turns(session, task, ledger, group=group)
    actions = _managed_actions(session, task, ledger, group=group)
    unowned = _unowned_authored(session, task, ledger, group=group)
    visible = [action for action in actions if _visible_confirmed(action)]
    protected = [
        action for action in actions
        if not _visible_confirmed(action)
        and str(action.status or "") not in SAFE_TERMINAL_ACTION_STATES
    ]
    latest_human_at = max(turns, default=None)
    managed_times = [
        *(_action_time(action) for action in [*visible, *protected]),
        *(_naive(observed_at) for observed_at in unowned),
    ]
    trailing = sum(
        1 for occurred_at in managed_times
        if latest_human_at is None or occurred_at > _naive(latest_human_at)
    )
    allowed = min(
        int(policy.absolute_daily_authored_cap),
        int(policy.bootstrap_allowance)
        + len(turns) * int(policy.managed_to_external_ratio_bps) // 10000,
    )
    visible_count = len(visible) + len(unowned)
    occupied = visible_count + len(protected)
    remaining = min(
        max(0, allowed - occupied),
        max(0, int(policy.max_consecutive_system_turns) - trailing),
    )
    return {
        "external_human_turn_count": len(turns),
        "visible_managed_authored_count": visible_count,
        "unowned_managed_authored_count": len(unowned),
        "planned_managed_authored_count": len(protected),
        "trailing_managed_turn_count": trailing,
        "allowed_managed_authored": allowed,
        "remaining_capacity": remaining,
        "latest_human_at": latest_human_at.isoformat() if latest_human_at else None,
        "policy_revision": int(policy.revision),
    }


def _upsert_presence_plan(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    group: TgGroup,
    policy: ManagedPresencePolicyRevision,
    evidence: dict,
) -> ManagedPresencePlan:
    current = session.scalar(select(ManagedPresencePlan).where(
        ManagedPresencePlan.task_day_ledger_id == ledger.id,
        ManagedPresencePlan.policy_revision_id == policy.id,
    ).with_for_update())
    payload_hash = _hash(evidence)
    values = {
        key: int(evidence[key])
        for key in (
            "external_human_turn_count", "visible_managed_authored_count",
            "planned_managed_authored_count", "trailing_managed_turn_count",
            "allowed_managed_authored", "remaining_capacity",
        )
    }
    if current is None:
        current = ManagedPresencePlan(
            tenant_id=task.tenant_id, task_id=task.id,
            task_day_ledger_id=ledger.id, policy_revision_id=policy.id,
            canonical_peer_id=str(group.tg_peer_id),
            task_day=ledger.obligation_local_date,
            decision="capacity_available" if values["remaining_capacity"] else "capacity_exhausted",
            evidence=evidence, input_hash=payload_hash, **values,
        )
        session.add(current)
    elif current.input_hash != payload_hash:
        for key, value in values.items():
            setattr(current, key, value)
        current.decision = (
            "capacity_available" if values["remaining_capacity"] else "capacity_exhausted"
        )
        current.evidence = evidence
        current.input_hash = payload_hash
        current.version = int(current.version or 1) + 1
    session.flush()
    return current


def _upsert_opportunity_plan(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    group: TgGroup,
    presence: ManagedPresencePlan,
    required_units: int,
) -> NaturalOpportunitySupplyPlanRevision:
    payload = {
        "required_units": required_units,
        "presence_plan_id": presence.id,
        "presence_version": presence.version,
        "remaining_capacity": presence.remaining_capacity,
    }
    payload_hash = _hash(payload)
    current = session.scalar(select(NaturalOpportunitySupplyPlanRevision).where(
        NaturalOpportunitySupplyPlanRevision.task_day_ledger_id == ledger.id,
        NaturalOpportunitySupplyPlanRevision.state == "active",
    ).with_for_update())
    if current is not None and current.input_hash == payload_hash:
        return current
    revision = int(current.plan_revision or 0) + 1 if current else 1
    if current is not None:
        current.state = "superseded"
        session.flush()
    guaranteed = int(presence.remaining_capacity)
    deficit = max(0, required_units - guaranteed)
    plan = NaturalOpportunitySupplyPlanRevision(
        tenant_id=task.tenant_id, task_id=task.id,
        task_day_ledger_id=ledger.id, canonical_peer_id=str(group.tg_peer_id),
        plan_revision=revision, required_capacity=required_units,
        guaranteed_now_capacity=guaranteed, forecast_conditional_capacity=0,
        deficit=deficit,
        commitment_status=(
            "guaranteed_achievable" if deficit == 0 else "opportunity_unproven"
        ),
        evidence=payload, input_hash=payload_hash,
    )
    session.add(plan)
    session.flush()
    return plan


def _presence_policy(
    session: Session,
    tenant_id: int,
) -> ManagedPresencePolicyRevision:
    policy = session.scalar(select(ManagedPresencePolicyRevision).where(
        ManagedPresencePolicyRevision.tenant_id == tenant_id,
        ManagedPresencePolicyRevision.state == "active",
    ))
    if policy is None:
        raise RuntimeError("managed_presence_policy_missing")
    return policy


def _project_task(
    task: Task,
    plan: NaturalOpportunitySupplyPlanRevision,
    presence: ManagedPresencePlan,
) -> None:
    stats = dict(task.stats or {})
    stats["natural_opportunity"] = {
        "plan_id": plan.id,
        "commitment_status": plan.commitment_status,
        "required_capacity": plan.required_capacity,
        "guaranteed_now_capacity": plan.guaranteed_now_capacity,
        "deficit": plan.deficit,
        "presence_plan_id": presence.id,
        "presence_remaining_capacity": presence.remaining_capacity,
    }
    task.stats = stats
    if plan.deficit:
        task.last_error = "natural_opportunity_plan_unproven"
    elif task.last_error == "natural_opportunity_plan_unproven":
        task.last_error = ""


def _visible_confirmed(action: PresenceAction) -> bool:
    return (
        str(action.status or "") == "success"
        and str(action.visibility_status or "")
        == "visible_confirmed"
    )


def _action_time(action: PresenceAction) -> datetime:
    value = action.executed_at or action.scheduled_at
    return _naive(value)


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


def _hash(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "NaturalOpportunityDecision",
    "ensure_managed_presence_plan",
    "ensure_natural_opportunity_plan",
]
