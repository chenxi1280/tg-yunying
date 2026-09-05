from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ChannelMessage,
    ChannelMessageSourceRevision,
    OperationTarget,
    PlanningAdmissionSnapshot,
    ReactionCapacityAllocationEpoch,
    Task,
    TaskDayLedger,
)

from .engagement_binding import UNIFIED_ENGAGEMENT_CONTRACT_VERSION
from .engagement_participation import ensure_source_participation_plan
from .engagement_participation import apply_journey_participation_selection
from .engagement_planning_admission import ensure_planning_admission_snapshot
from .engagement_portfolio import reserve_portfolio_units
from .engagement_source_journey import JourneyDemand, register_source_journey_demand
from .pacing_quantity import deterministic_quantity_with_jitter
from .album_reaction_facts import album_extra_units
from .reaction_source_identity import reaction_source_identity as _source_identity


POLICY_REVISION = "reaction_capacity_round_robin_v1"


def ensure_reaction_capacity_epoch(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    messages: list[ChannelMessage],
    target: OperationTarget,
) -> ReactionCapacityAllocationEpoch | None:
    if not _unified(task):
        return None
    config = task.type_config or {}
    cap = int(config.get("daily_reaction_cap") or 0)
    if cap <= 0:
        raise ValueError("daily_reaction_cap_must_be_positive")
    cap = max(0, cap - album_extra_units(session, task, ledger))
    demands = _source_demands(task, messages)
    previous = _active_epoch(session, task, ledger)
    if _unchanged_epoch(previous, cap=cap, demands=demands):
        return previous
    allocations, admission_ids = _allocate_sources(
        session,
        task,
        ledger,
        demands=demands,
        cap=cap,
        previous=previous,
        target=target,
    )
    allocations = _reserve_portfolio_allocations(
        session, task, ledger, allocations=allocations,
    )
    signature = _hash(
        {
            "cap": cap,
            "demands": demands,
            "allocations": allocations,
            "policy": POLICY_REVISION,
        }
    )
    return _append_epoch(
        session,
        task,
        ledger,
        previous=previous,
        cap=cap,
        demands=demands,
        allocations=allocations,
        admission_ids=admission_ids,
        signature=signature,
    )


def _unchanged_epoch(
    previous: ReactionCapacityAllocationEpoch | None,
    *,
    cap: int,
    demands: list[dict],
) -> bool:
    return bool(
        previous is not None
        and int(previous.daily_reaction_cap) == cap
        and list(previous.source_demands or []) == demands
    )


def allocated_account_ids_by_message(
    epoch: ReactionCapacityAllocationEpoch,
) -> dict[int, list[int]]:
    return {
        int(item["channel_message_id"]): [
            int(account_id) for account_id in item["allocated_account_ids"]
        ]
        for item in epoch.source_allocations or []
    }


def reaction_allocations_for_messages(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    messages: list[ChannelMessage],
    target: OperationTarget,
) -> dict[int, list[int]] | None:
    epoch = ensure_reaction_capacity_epoch(
        session,
        task,
        ledger,
        messages=messages,
        target=target,
    )
    if epoch is None:
        return None
    return allocated_account_ids_by_message(epoch)


def reaction_admissible_account_ids(
    session: Session, epoch: ReactionCapacityAllocationEpoch, *,
    task: Task, ledger: TaskDayLedger, target: OperationTarget,
) -> set[int]:
    _, _, snapshot_ids = _reaction_candidates(
        session, task, ledger, demands=list(epoch.source_demands or []), target=target,
    )
    rows = session.scalars(select(PlanningAdmissionSnapshot).where(
        PlanningAdmissionSnapshot.id.in_(snapshot_ids)
    ))
    return {
        int(account_id)
        for row in rows
        for account_id in row.admissible_account_ids or []
    }


def _source_demands(task: Task, messages: list[ChannelMessage]) -> list[dict]:
    config = task.type_config or {}
    base = int(config.get("target_likes_per_message") or 1)
    jitter = float(config.get("like_count_jitter") or 0)
    demands = []
    for message in messages:
        source_identity = _source_identity(message)
        target = deterministic_quantity_with_jitter(
            base, jitter, seed_id=f"like:{task.id}:{source_identity}"
        )
        demands.append(
            {
                "channel_message_id": message.id,
                "source_identity": source_identity,
                "source_revision_id": message.current_source_revision_id,
                "published_at": message.published_at.isoformat() if message.published_at else "",
                "required_count": target,
                **({"album_id": message.grouped_id} if message.grouped_id else {}),
            }
        )
    return sorted(demands, key=_source_order)


def _allocate_sources(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    demands: list[dict],
    cap: int,
    previous: ReactionCapacityAllocationEpoch | None,
    target: OperationTarget,
) -> tuple[list[dict], list[str]]:
    plans, candidates, admission_ids = _reaction_candidates(
        session, task, ledger, demands=demands, target=target,
    )
    allocated = _round_robin_allocations(
        candidates,
        cap=cap,
        previous=previous,
        source_ids={row["source_identity"]: int(row["channel_message_id"]) for row in demands},
        demand_limits={int(row["channel_message_id"]): int(row["required_count"]) for row in demands},
    )
    allocated, journey_plan_ids = _apply_reaction_journeys(
        session,
        task,
        ledger,
        demands=demands,
        plans=plans,
        candidates=candidates,
        allocated=allocated,
    )
    return _allocation_rows(
        demands, allocated, journey_plan_ids=journey_plan_ids,
    ), admission_ids


def _reaction_candidates(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    demands: list[dict],
    target: OperationTarget,
) -> tuple[dict[int, object], dict[int, list[int]], list[str]]:
    plans: dict[int, object] = {}
    candidates: dict[int, list[int]] = {}
    admission_ids: list[str] = []
    for demand in demands:
        plan = ensure_source_participation_plan(
            session,
            task,
            ledger,
            source_identity=demand["source_identity"],
            required_count=int(demand["required_count"]),
        )
        if plan is None:
            candidates[int(demand["channel_message_id"])] = []
            continue
        admission = ensure_planning_admission_snapshot(
            session,
            task,
            plan,
            planning_horizon=demand["source_identity"],
            target=target,
            candidate_account_ids=list(plan.policy_eligible_account_ids or []),
        )
        message_id = int(demand["channel_message_id"])
        plans[message_id] = plan
        candidates[message_id] = [
            int(item) for item in plan.policy_eligible_account_ids or []
        ]
        admission_ids.append(admission.id)
    return plans, candidates, admission_ids


def _round_robin_allocations(
    candidates: dict[int, list[int]],
    *,
    cap: int,
    previous: ReactionCapacityAllocationEpoch | None,
    source_ids: dict[str, int],
    demand_limits: dict[int, int],
) -> dict[int, list[int]]:
    allocated = _preserved_allocations(previous, candidates, source_ids=source_ids)
    for ordinal in range(max((len(ids) for ids in candidates.values()), default=0)):
        for message_id, ids in candidates.items():
            if sum(len(items) for items in allocated.values()) >= cap:
                return allocated
            if len(allocated[message_id]) >= demand_limits[message_id]:
                continue
            if ordinal < len(ids) and ids[ordinal] not in allocated[message_id]:
                allocated[message_id].append(ids[ordinal])
    return allocated


def _apply_reaction_journeys(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    demands: list[dict],
    plans: dict[int, object],
    candidates: dict[int, list[int]],
    allocated: dict[int, list[int]],
) -> tuple[dict[int, list[int]], dict[int, str]]:
    journey_plan_ids: dict[int, str] = {}
    for demand in demands:
        message_id = int(demand["channel_message_id"])
        plan = plans.get(message_id)
        if plan is None:
            allocated[message_id] = []
            continue
        journey = register_source_journey_demand(
            session,
            _source_revision(session, demand),
            task_day=ledger.obligation_local_date,
            demand=JourneyDemand(
                task.id, "reaction", len(allocated[message_id]),
                tuple(candidates[message_id]),
                tuple(int(item) for item in plan.selected_account_ids or []),
            ),
        )
        journey_plan_ids[message_id] = journey.plan.id
        if not journey.achievable:
            allocated[message_id] = []
            continue
        selected = list(
            journey.account_ids_by_task_action.get((task.id, "reaction"), ())
        )
        apply_journey_participation_selection(
            session,
            task,
            plan,
            selected_account_ids=selected,
            journey_plan_id=journey.plan.id,
        )
        allocated[message_id] = selected
    return allocated, journey_plan_ids


def _preserved_allocations(
    previous: ReactionCapacityAllocationEpoch | None,
    candidates: dict[int, list[int]],
    *,
    source_ids: dict[str, int],
) -> dict[int, list[int]]:
    allocated = {message_id: [] for message_id in candidates}
    if previous is None:
        return allocated
    for item in previous.source_allocations or []:
        message_id = source_ids.get(item["source_identity"], int(item["channel_message_id"]))
        if message_id not in allocated:
            continue
        allocated[message_id] = [
            int(account_id) for account_id in item["allocated_account_ids"]
        ]
    return allocated


def _allocation_rows(
    demands: list[dict],
    allocated: dict[int, list[int]],
    *,
    journey_plan_ids: dict[int, str],
) -> list[dict]:
    return [
        {
            "channel_message_id": int(item["channel_message_id"]),
            "source_identity": item["source_identity"],
            "required_count": int(item["required_count"]),
            "allocated_account_ids": allocated[int(item["channel_message_id"])],
            "source_journey_plan_id": journey_plan_ids.get(
                int(item["channel_message_id"]), ""
            ),
        }
        for item in demands
    ]


def _reserve_portfolio_allocations(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    allocations: list[dict],
) -> list[dict]:
    result = []
    for row in allocations:
        account_ids = [int(item) for item in row["allocated_account_ids"]]
        decision = reserve_portfolio_units(
            session,
            task,
            ledger,
            action_class="reaction",
            demand_identity=f"reaction:{row['source_identity']}",
            requested_units_by_account={account_id: 1 for account_id in account_ids},
        )
        accepted = set(decision.allocated_units_by_account)
        result.append(
            {
                **row,
                "allocated_account_ids": [
                    account_id for account_id in account_ids if account_id in accepted
                ],
                "portfolio_plan_id": decision.plan.id,
                "portfolio_deficit_units": decision.deficit_units,
            }
        )
    return result


def _append_epoch(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    previous: ReactionCapacityAllocationEpoch | None,
    cap: int,
    demands: list[dict],
    allocations: list[dict],
    admission_ids: list[str],
    signature: str,
) -> ReactionCapacityAllocationEpoch:
    if previous is not None:
        previous.state = "superseded"
    allocated = sum(len(item["allocated_account_ids"]) for item in allocations)
    required = sum(int(item["required_count"]) for item in demands)
    epoch = ReactionCapacityAllocationEpoch(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        task_day_ledger_id=ledger.id,
        allocation_revision=(previous.allocation_revision + 1) if previous else 1,
        policy_revision=POLICY_REVISION,
        daily_reaction_cap=cap,
        source_demands=demands,
        source_allocations=allocations,
        planning_admission_snapshot_ids=admission_ids,
        allocated_count=allocated,
        unallocated_count=max(0, required - allocated),
        unallocated_reasons={"reaction_daily_cap_unallocated": max(0, required - allocated)},
        allocation_hash=signature,
        supersedes_epoch_id=previous.id if previous else None,
    )
    session.add(epoch)
    session.flush()
    return epoch


def _active_epoch(
    session: Session, task: Task, ledger: TaskDayLedger
) -> ReactionCapacityAllocationEpoch | None:
    return session.scalar(
        select(ReactionCapacityAllocationEpoch).where(
            ReactionCapacityAllocationEpoch.task_id == task.id,
            ReactionCapacityAllocationEpoch.task_lifecycle_epoch == task.task_lifecycle_epoch,
            ReactionCapacityAllocationEpoch.task_day_ledger_id == ledger.id,
            ReactionCapacityAllocationEpoch.state == "active",
        )
    )


def _source_revision(
    session: Session,
    demand: dict,
) -> ChannelMessageSourceRevision:
    source_id = str(demand.get("source_revision_id") or "")
    source = session.get(ChannelMessageSourceRevision, source_id) if source_id else None
    if source is None or source.channel_message_id != int(demand["channel_message_id"]):
        raise ValueError("reaction_source_revision_unproven")
    return source


def _source_order(item: dict) -> tuple[str, int]:
    return str(item["published_at"]), int(item["channel_message_id"])


def _hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _unified(task: Task) -> bool:
    return (
        (task.type_config or {}).get("engagement_contract_version")
        == UNIFIED_ENGAGEMENT_CONTRACT_VERSION
    )


__all__ = [
    "allocated_account_ids_by_message",
    "ensure_reaction_capacity_epoch",
    "reaction_admissible_account_ids",
    "reaction_allocations_for_messages",
]
