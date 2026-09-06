from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ChannelMessageSourceRevision,
    CrossAdapterSourceJourneyPlanRevision,
    SourceJourneyDecision,
    Task,
)

from .engagement_source_journey_solver import ACTION_RANK, solve_source_journey


POLICY_REVISION = "cross_adapter_source_journey_v1"


@dataclass(frozen=True)
class JourneyDemand:
    task_id: str
    action_class: str
    required_count: int
    candidate_account_ids: tuple[int, ...]
    preferred_account_ids: tuple[int, ...] = ()
    hard_account_ids: tuple[int, ...] = ()
    joint_constraint_hash: str = ""


@dataclass(frozen=True)
class SourceJourneyPlanDecision:
    plan: CrossAdapterSourceJourneyPlanRevision
    account_ids_by_task_action: dict[tuple[str, str], tuple[int, ...]]

    @property
    def achievable(self) -> bool:
        return self.plan.decision != "cross_adapter_journey_unachievable"


def compile_source_journey(
    session: Session,
    source: ChannelMessageSourceRevision,
    *,
    task_day: date,
    demands: list[JourneyDemand],
) -> SourceJourneyPlanDecision:
    constraints = _normalize_demands(demands)
    _validate_tasks(session, source, constraints)
    _lock_source(session, source)
    active = _active_plan(session, source, task_day)
    if active is not None and list(active.adapter_constraints or []) == constraints:
        return _decision(active)
    input_hash = _hash({
        "policy": POLICY_REVISION,
        "tenant_id": source.tenant_id,
        "source_revision_id": source.id,
        "task_day": str(task_day),
        "constraints": constraints,
        "base_plan_id": active.id if active else "",
    })
    rejected = _rejected_plan(
        session,
        source,
        task_day,
        input_hash=input_hash,
    )
    if rejected is not None:
        return _decision(rejected)
    return _create_plan(
        session,
        source,
        task_day,
        constraints=constraints,
        input_hash=input_hash,
        active=active,
    )


def register_source_journey_demand(
    session: Session,
    source: ChannelMessageSourceRevision,
    *,
    task_day: date,
    demand: JourneyDemand,
) -> SourceJourneyPlanDecision:
    _lock_source(session, source)
    active = _active_plan(session, source, task_day)
    demands = {
        _demand_key(item): _constraint_demand(item)
        for item in (active.adapter_constraints or [])
    } if active is not None else {}
    key = (str(demand.task_id), demand.action_class)
    demands[key] = _stable_registered_demand(demands.get(key), demand)
    return compile_source_journey(
        session,
        source,
        task_day=task_day,
        demands=list(demands.values()),
    )


def _stable_registered_demand(
    previous: JourneyDemand | None,
    current: JourneyDemand,
) -> JourneyDemand:
    if previous is None:
        return current
    if (
        previous.required_count != current.required_count
        or set(previous.candidate_account_ids) != set(current.candidate_account_ids)
        or set(previous.hard_account_ids) != set(current.hard_account_ids)
        or previous.joint_constraint_hash != current.joint_constraint_hash
    ):
        return current
    return JourneyDemand(
        current.task_id,
        current.action_class,
        current.required_count,
        current.candidate_account_ids,
        previous.preferred_account_ids,
        current.hard_account_ids,
        current.joint_constraint_hash,
    )


def _normalize_demands(demands: list[JourneyDemand]) -> list[dict]:
    normalized: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    for demand in demands:
        if demand.action_class not in ACTION_RANK:
            raise ValueError("source_journey_action_class_unsupported")
        demand_key = (str(demand.task_id), demand.action_class)
        if demand_key in seen_keys:
            raise ValueError("source_journey_duplicate_task_adapter_demand")
        seen_keys.add(demand_key)
        required = int(demand.required_count)
        if required < 0:
            raise ValueError("source_journey_required_count_invalid")
        candidates = sorted({
            int(account_id)
            for account_id in demand.candidate_account_ids
            if int(account_id) > 0
        })
        preferred = sorted({
            int(account_id)
            for account_id in demand.preferred_account_ids
            if int(account_id) in candidates
        })
        normalized.append({
            "task_id": str(demand.task_id),
            "action_class": demand.action_class,
            "required_count": required,
            "candidate_account_ids": candidates,
            "preferred_account_ids": preferred,
            **_joint_constraint_fields(demand, candidates),
        })
    return sorted(
        normalized,
        key=lambda item: (ACTION_RANK[item["action_class"]], item["task_id"]),
    )


def _joint_constraint_fields(demand: JourneyDemand, candidates: list[int]) -> dict:
    hard = sorted(set(demand.hard_account_ids))
    if not set(hard).issubset(candidates):
        raise ValueError("source_journey_hard_account_not_eligible")
    if hard and not demand.joint_constraint_hash:
        raise ValueError("source_journey_joint_constraint_hash_missing")
    return {"hard_account_ids": hard, "joint_constraint_hash": demand.joint_constraint_hash}


def _lock_source(session: Session, source: ChannelMessageSourceRevision) -> None:
    session.execute(select(ChannelMessageSourceRevision.id).where(
        ChannelMessageSourceRevision.id == source.id,
        ChannelMessageSourceRevision.tenant_id == source.tenant_id,
    ).with_for_update()).scalar_one()


def _validate_tasks(session, source, constraints) -> None:
    task_ids = {item["task_id"] for item in constraints}
    tasks = list(session.scalars(select(Task).where(Task.id.in_(task_ids))))
    if {task.id for task in tasks} != task_ids:
        raise ValueError("source_journey_task_missing")
    if any(task.tenant_id != source.tenant_id for task in tasks):
        raise ValueError("source_journey_cross_tenant_task")
    if any(
        (task.type_config or {}).get("engagement_contract_version")
        != "unified_engagement_v1"
        for task in tasks
    ):
        raise ValueError("source_journey_task_not_unified")


def _constraint_demand(item: dict) -> JourneyDemand:
    return JourneyDemand(
        task_id=str(item["task_id"]),
        action_class=str(item["action_class"]),
        required_count=int(item["required_count"]),
        candidate_account_ids=tuple(int(value) for value in item["candidate_account_ids"]),
        preferred_account_ids=tuple(int(value) for value in item.get("preferred_account_ids", [])),
        hard_account_ids=tuple(int(value) for value in item.get("hard_account_ids", [])),
        joint_constraint_hash=str(item.get("joint_constraint_hash") or ""),
    )


def _active_plan(session, source, task_day):
    return session.scalar(
        select(CrossAdapterSourceJourneyPlanRevision)
        .where(
            CrossAdapterSourceJourneyPlanRevision.tenant_id == source.tenant_id,
            CrossAdapterSourceJourneyPlanRevision.source_revision_id == source.id,
            CrossAdapterSourceJourneyPlanRevision.task_day == task_day,
            CrossAdapterSourceJourneyPlanRevision.state == "active",
        )
        .with_for_update()
    )


def _rejected_plan(session, source, task_day, *, input_hash):
    return session.scalar(
        select(CrossAdapterSourceJourneyPlanRevision).where(
            CrossAdapterSourceJourneyPlanRevision.tenant_id == source.tenant_id,
            CrossAdapterSourceJourneyPlanRevision.source_revision_id == source.id,
            CrossAdapterSourceJourneyPlanRevision.task_day == task_day,
            CrossAdapterSourceJourneyPlanRevision.input_hash == input_hash,
            CrossAdapterSourceJourneyPlanRevision.state == "rejected",
        )
    )


def _create_plan(
    session,
    source,
    task_day,
    *,
    constraints,
    input_hash,
    active,
):
    edge_set, metrics, deficits, decision = solve_source_journey(
        constraints,
        input_hash,
        frozen_edges=list(active.edge_set or []) if active else [],
    )
    rejected = active is not None and decision == "cross_adapter_journey_unachievable"
    if active is not None and not rejected:
        active.state = "superseded"
        session.flush()
    plan = _new_plan(
        source,
        task_day,
        constraints=constraints,
        input_hash=input_hash,
        active=active,
        edge_set=edge_set,
        metrics=metrics,
        deficits=deficits,
        decision=decision,
        plan_revision=_next_plan_revision(session, source, task_day),
        state="rejected" if rejected else "active",
    )
    session.add(plan)
    session.flush()
    _persist_decisions(session, plan)
    return _decision(plan)


def _new_plan(
    source,
    task_day,
    *,
    constraints,
    input_hash,
    active,
    edge_set,
    metrics,
    deficits,
    decision,
    plan_revision,
    state,
):
    task_ids = sorted({item["task_id"] for item in constraints})
    return CrossAdapterSourceJourneyPlanRevision(
        tenant_id=source.tenant_id,
        source_revision_id=source.id,
        task_day=task_day,
        plan_revision=plan_revision,
        source_task_set_hash=_hash(task_ids),
        policy_revision=POLICY_REVISION,
        adapter_constraints=constraints,
        hard_constraint_hash=_hash(constraints),
        objective_policy={
            "priority": [
                "preserve_hard_quantity",
                "minimize_reaction_comment_overlap",
                "minimize_triple_overlap",
            ]
        },
        edge_set=edge_set,
        edge_set_hash=_hash(edge_set),
        overlap_metrics=metrics,
        deficits=deficits,
        decision=decision,
        input_hash=input_hash,
        supersedes_plan_id=active.id if active else None,
        state=state,
    )


def _next_plan_revision(session, source, task_day) -> int:
    current = session.scalar(select(func.max(
        CrossAdapterSourceJourneyPlanRevision.plan_revision
    )).where(
        CrossAdapterSourceJourneyPlanRevision.tenant_id == source.tenant_id,
        CrossAdapterSourceJourneyPlanRevision.source_revision_id == source.id,
        CrossAdapterSourceJourneyPlanRevision.task_day == task_day,
    ))
    return int(current or 0) + 1


def _demand_key(item) -> tuple[str, str]:
    return str(item["task_id"]), str(item["action_class"])


def _persist_decisions(session, plan) -> None:
    sorted_edges = sorted(
        plan.edge_set or [],
        key=lambda edge: (int(edge["account_id"]), str(edge.get("task_id", "")), str(edge.get("action_class", ""))),
    )
    session.add_all(
        SourceJourneyDecision(
            tenant_id=plan.tenant_id,
            plan_id=plan.id,
            task_id=edge["task_id"],
            account_id=edge["account_id"],
            action_class=edge["action_class"],
            journey_class=edge["journey_class"],
        )
        for edge in sorted_edges
    )
    session.flush()


def _decision(plan) -> SourceJourneyPlanDecision:
    account_ids: dict[tuple[str, str], list[int]] = {}
    for edge in plan.edge_set or []:
        key = (str(edge["task_id"]), str(edge["action_class"]))
        account_ids.setdefault(key, []).append(int(edge["account_id"]))
    return SourceJourneyPlanDecision(
        plan=plan,
        account_ids_by_task_action={
            key: tuple(sorted(values)) for key, values in account_ids.items()
        },
    )


def _hash(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


__all__ = [
    "JourneyDemand",
    "SourceJourneyPlanDecision",
    "compile_source_journey",
    "register_source_journey_demand",
]
