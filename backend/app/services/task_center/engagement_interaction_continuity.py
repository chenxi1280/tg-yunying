from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ConversationTurnClaim,
    InteractionContinuityCapacityPlan,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    TgGroup,
)

from .engagement_natural_opportunity import ensure_managed_presence_plan


ACTIVE_ACTION_STATES = frozenset({
    "pending", "claiming", "executing", "retryable_failed", "unknown_after_send",
})


@dataclass(frozen=True)
class InteractionContinuityDecision:
    plan: InteractionContinuityCapacityPlan
    admitted_targets: tuple[dict, ...]


def ensure_interaction_continuity_capacity(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    group: TgGroup,
    *,
    operation_target_id: int,
    reply_targets: list[dict],
) -> InteractionContinuityDecision:
    targets = _unique_claim_targets(reply_targets)
    policy, presence = ensure_managed_presence_plan(
        session, task, ledger, group=group,
    )
    prior_claim_ids = _prior_observed_claim_ids(session, ledger, policy.id)
    existing_slots = _continuity_slots(session, task, ledger)
    existing_by_claim = {
        str(slot.continuity_claim_id): slot for slot in existing_slots
    }
    active_reserved = _unbound_active_slot_count(session, existing_slots)
    new_capacity = max(0, int(presence.remaining_capacity) - active_reserved)
    admitted = _admitted_targets(targets, existing_by_claim, new_capacity)
    _create_claim_slots(
        session, task, ledger,
        operation_target_id=operation_target_id,
        targets=admitted,
        existing_by_claim=existing_by_claim,
    )
    counts = _continuity_counts(
        session, task, ledger, targets, prior_claim_ids=prior_claim_ids,
    )
    plan = _upsert_capacity_plan(
        session, task, ledger, group,
        policy_revision_id=policy.id,
        presence_plan_id=presence.id,
        counts=counts,
    )
    _project_task(task, plan)
    return InteractionContinuityDecision(plan, tuple(admitted))


def refresh_interaction_continuity_settlement(
    session: Session, action: Action,
) -> None:
    slot = session.get(
        TaskGroupDailyMessageSlot, str(action.primary_quantity_slot_id or ""),
    )
    if slot is None or not slot.continuity_claim_id:
        return
    ledger = session.get(TaskDayLedger, slot.task_day_ledger_id)
    task = session.get(Task, action.task_id)
    plan = session.scalar(select(InteractionContinuityCapacityPlan).where(
        InteractionContinuityCapacityPlan.task_day_ledger_id == slot.task_day_ledger_id,
    ))
    if ledger is None or task is None or plan is None:
        raise RuntimeError("interaction_continuity_settlement_owner_missing")
    prior_ids = {
        str(item) for item in (plan.evidence or {}).get("claim_ids") or [] if item
    }
    counts = _continuity_counts(
        session, task, ledger, [], prior_claim_ids=prior_ids,
    )
    evidence = {
        **counts,
        "presence_plan_id": (plan.evidence or {}).get("presence_plan_id"),
    }
    values = _plan_values(counts, plan.decision, evidence, _hash(evidence))
    for key, value in values.items():
        setattr(plan, key, value)
    plan.version = int(plan.version or 1) + 1
    _project_task(task, plan)


def _unique_claim_targets(targets: list[dict]) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for target in targets:
        claim_id = str(target.get("conversation_turn_claim_id") or "")
        if not claim_id or claim_id in seen:
            continue
        seen.add(claim_id)
        result.append(target)
    return result


def _continuity_slots(
    session: Session, task: Task, ledger: TaskDayLedger,
) -> list[TaskGroupDailyMessageSlot]:
    return list(session.scalars(select(TaskGroupDailyMessageSlot).where(
        TaskGroupDailyMessageSlot.task_id == task.id,
        TaskGroupDailyMessageSlot.task_day_ledger_id == ledger.id,
        TaskGroupDailyMessageSlot.continuity_claim_id.is_not(None),
    ).with_for_update()))


def _unbound_active_slot_count(
    session: Session, slots: list[TaskGroupDailyMessageSlot],
) -> int:
    slot_ids = [slot.id for slot in slots if slot.state in {"open", "unknown"}]
    if not slot_ids:
        return 0
    bound_ids = set(session.scalars(select(Action.primary_quantity_slot_id).where(
        Action.primary_quantity_slot_id.in_(slot_ids),
        Action.status.in_(ACTIVE_ACTION_STATES),
    )))
    return sum(1 for slot_id in slot_ids if slot_id not in bound_ids)


def _admitted_targets(
    targets: list[dict],
    existing_by_claim: dict[str, TaskGroupDailyMessageSlot],
    new_capacity: int,
) -> list[dict]:
    existing = [
        target for target in targets
        if str(target["conversation_turn_claim_id"]) in existing_by_claim
    ]
    pending = [target for target in targets if target not in existing]
    return [*existing, *pending[:new_capacity]]


def _create_claim_slots(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    operation_target_id: int,
    targets: list[dict],
    existing_by_claim: dict[str, TaskGroupDailyMessageSlot],
) -> None:
    ordinal = int(session.scalar(select(func.max(TaskGroupDailyMessageSlot.slot_ordinal)).where(
        TaskGroupDailyMessageSlot.task_day_ledger_id == ledger.id,
        TaskGroupDailyMessageSlot.target_operation_target_id == operation_target_id,
    )) or 0)
    for target in targets:
        claim_id = str(target["conversation_turn_claim_id"])
        if claim_id in existing_by_claim:
            continue
        ordinal += 1
        session.add(TaskGroupDailyMessageSlot(
            tenant_id=task.tenant_id,
            task_id=task.id,
            task_day_ledger_id=ledger.id,
            target_operation_target_id=operation_target_id,
            continuity_claim_id=claim_id,
            quantity_credit_eligible=False,
            slot_kind="interaction_continuity",
            slot_ordinal=ordinal,
        ))
    session.flush()


def _continuity_counts(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    targets: list[dict],
    *,
    prior_claim_ids: set[str],
) -> dict:
    claim_ids = sorted(prior_claim_ids | {
        str(target["conversation_turn_claim_id"]) for target in targets
    })
    state_by_claim = dict(session.execute(
        select(ConversationTurnClaim.id, ConversationTurnClaim.state).where(
            ConversationTurnClaim.id.in_(claim_ids),
        )
    ).all()) if claim_ids else {}
    slots = _observed_claim_slots(session, task, ledger, claim_ids)
    slot_claim_ids = {str(slot.continuity_claim_id) for slot in slots}
    served = sum(1 for claim_id in slot_claim_ids if state_by_claim.get(claim_id) == "served")
    unknown = sum(
        1 for claim_id in slot_claim_ids
        if state_by_claim.get(claim_id) == "unknown_after_send"
    )
    remaining = sum(
        1 for claim_id in slot_claim_ids
        if state_by_claim.get(claim_id) in {"claimed", "bound"}
    )
    return {
        "claim_ids": claim_ids,
        "observed": len(claim_ids),
        "max_service": len(slot_claim_ids),
        "protected_reserved": remaining,
        "borrowed": 0,
        "recalled": 0,
        "admitted": len(slot_claim_ids),
        "served": served,
        "unknown": unknown,
        "rejected": max(0, len(claim_ids) - len(slot_claim_ids)),
        "remaining": remaining,
    }


def _prior_observed_claim_ids(
    session: Session, ledger: TaskDayLedger, policy_revision_id: str,
) -> set[str]:
    plan = session.scalar(select(InteractionContinuityCapacityPlan).where(
        InteractionContinuityCapacityPlan.task_day_ledger_id == ledger.id,
        InteractionContinuityCapacityPlan.policy_revision_id == policy_revision_id,
    ))
    evidence = plan.evidence if plan and isinstance(plan.evidence, dict) else {}
    return {str(item) for item in evidence.get("claim_ids") or [] if item}


def _observed_claim_slots(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    claim_ids: list[str],
) -> list[TaskGroupDailyMessageSlot]:
    if not claim_ids:
        return []
    return list(session.scalars(select(TaskGroupDailyMessageSlot).where(
        TaskGroupDailyMessageSlot.task_id == task.id,
        TaskGroupDailyMessageSlot.task_day_ledger_id == ledger.id,
        TaskGroupDailyMessageSlot.continuity_claim_id.in_(claim_ids),
    )))


def _upsert_capacity_plan(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    group: TgGroup,
    *,
    policy_revision_id: str,
    presence_plan_id: str,
    counts: dict[str, int],
) -> InteractionContinuityCapacityPlan:
    evidence = {**counts, "presence_plan_id": presence_plan_id}
    input_hash = _hash(evidence)
    plan = session.scalar(select(InteractionContinuityCapacityPlan).where(
        InteractionContinuityCapacityPlan.task_day_ledger_id == ledger.id,
        InteractionContinuityCapacityPlan.policy_revision_id == policy_revision_id,
    ).with_for_update())
    decision = "capacity_available" if not counts["rejected"] else "capacity_exhausted"
    values = _plan_values(counts, decision, evidence, input_hash)
    if plan is None:
        plan = InteractionContinuityCapacityPlan(
            tenant_id=task.tenant_id, task_id=task.id,
            task_day_ledger_id=ledger.id,
            policy_revision_id=policy_revision_id,
            canonical_peer_id=str(group.tg_peer_id),
            task_day=ledger.obligation_local_date,
            **values,
        )
        session.add(plan)
    elif plan.input_hash != input_hash:
        for key, value in values.items():
            setattr(plan, key, value)
        plan.version = int(plan.version or 1) + 1
    session.flush()
    return plan


def _plan_values(
    counts: dict[str, int], decision: str, evidence: dict, input_hash: str,
) -> dict:
    return {
        "observed_eligible_demand": counts["observed"],
        "max_service_count": counts["max_service"],
        "protected_reserved_count": counts["protected_reserved"],
        "borrowed_count": counts["borrowed"],
        "recalled_count": counts["recalled"],
        "admitted_count": counts["admitted"],
        "served_count": counts["served"],
        "unknown_count": counts["unknown"],
        "rejected_by_capacity_count": counts["rejected"],
        "remaining_capacity": counts["remaining"],
        "decision": decision,
        "evidence": evidence,
        "input_hash": input_hash,
    }


def _project_task(task: Task, plan: InteractionContinuityCapacityPlan) -> None:
    stats = dict(task.stats or {})
    stats["interaction_continuity"] = {
        "plan_id": plan.id,
        "observed_eligible_demand": plan.observed_eligible_demand,
        "max_service_count": plan.max_service_count,
        "protected_reserved_count": plan.protected_reserved_count,
        "borrowed_count": plan.borrowed_count,
        "recalled_count": plan.recalled_count,
        "admitted_count": plan.admitted_count,
        "served_count": plan.served_count,
        "unknown_count": plan.unknown_count,
        "rejected_by_capacity_count": plan.rejected_by_capacity_count,
        "quantity_credit": 0,
    }
    task.stats = stats
    if plan.rejected_by_capacity_count:
        task.last_error = "interaction_continuity_capacity_exhausted"


def _hash(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "InteractionContinuityDecision",
    "ensure_interaction_continuity_capacity",
    "refresh_interaction_continuity_settlement",
]
