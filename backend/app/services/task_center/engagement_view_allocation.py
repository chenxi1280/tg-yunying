from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ChannelMessage,
    ChannelViewDailyMessageTarget,
    PlanningAdmissionSnapshot,
    Task,
    TaskDayLedger,
    TaskParticipationUnitPlan,
    ViewAccountSourceAllocationPlan,
)

from .engagement_view_allocation_solver import (
    ALGORITHM_REVISION,
    UNACHIEVABLE,
    allocation_mode,
    initial_allocation_draft,
    rebuild_allocation_draft,
    successor_allocation_draft,
)
from .channel_source_message_persistence import ensure_channel_message_source_revision
from .engagement_portfolio import reserve_portfolio_units
from .engagement_view_journey import compile_view_source_journeys


def ensure_view_allocation_plan(
    session: Session,
    task: Task,
    *,
    ledger: TaskDayLedger,
    participation_plan: TaskParticipationUnitPlan,
    admission_snapshot: PlanningAdmissionSnapshot,
    messages: list[ChannelMessage],
    forbidden_account_ids_by_message: dict[int, set[int]],
    config: dict,
) -> ViewAccountSourceAllocationPlan:
    for message in messages:
        if not message.current_source_revision_id:
            ensure_channel_message_source_revision(session, message)
    sources = _sources(messages, forbidden_account_ids_by_message)
    existing = _active_plan(session, task, ledger)
    sources, reusable = _merge_plan_sources(existing, sources)
    if reusable:
        return existing
    draft, revision = _allocation_draft(
        session, task,
        sources=sources,
        existing=existing,
        participation=participation_plan,
        config=config,
    )
    draft = compile_view_source_journeys(
        session, task, ledger,
        sources=sources,
        draft=draft,
        participation=participation_plan,
        frozen_edges=list(existing.edge_set or []) if existing else [],
    )
    draft = _reserve_view_portfolio(
        session, task, ledger, sources=sources, draft=draft,
    )
    plan = _new_plan(
        task,
        ledger=ledger,
        participation=participation_plan,
        admission=admission_snapshot,
        sources=sources,
        draft=draft,
        mode=allocation_mode(config),
        revision=revision,
        supersedes=existing,
    )
    session.add(plan)
    session.flush()
    return plan


def _merge_plan_sources(existing, sources: list[dict]) -> tuple[list[dict], bool]:
    if existing is None or getattr(existing, "decision", None) != "achievable" or not (existing.edge_set or []):
        return sources, False
    merged, changed = _merge_new_sources(existing.source_set or [], sources)
    journey_wired = all(
        edge.get("source_journey_plan_id") and "portfolio_reserved_units" in edge
        for edge in existing.edge_set or []
    )
    return merged, not changed and journey_wired


def _allocation_draft(
    session: Session,
    task: Task,
    *,
    sources: list[dict],
    existing: ViewAccountSourceAllocationPlan | None,
    participation: TaskParticipationUnitPlan,
    config: dict,
):
    if existing is None or getattr(existing, "decision", None) != "achievable" or not (existing.edge_set or []):
        draft = initial_allocation_draft(
            task.id,
            [int(item) for item in participation.selected_account_ids or []],
            sources,
            config=config,
        )
        if existing is not None:
            existing.state = "superseded"
            session.flush()
        return draft, (existing.allocation_revision + 1) if existing else 1
    draft = successor_allocation_draft(
        task.id,
        existing.account_degrees or [],
        existing.edge_set or [],
        prior_sources=existing.source_set or [],
        prior_missing=existing.unallocated_sources or [],
        sources=sources,
        config=config,
    )
    existing.state = "superseded"
    session.flush()
    return draft, existing.allocation_revision + 1


def _reserve_view_portfolio(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    sources: list[dict],
    draft,
):
    reserved_edges = []
    edges_by_source = {
        source["source_identity"]: [
            edge
            for edge in draft.edges
            if edge["source_identity"] == source["source_identity"]
        ]
        for source in sources
    }
    for source in sources:
        edges = edges_by_source[source["source_identity"]]
        decision = reserve_portfolio_units(
            session,
            task,
            ledger,
            action_class="view",
            demand_identity=f"view:{source['source_identity']}",
            requested_units_by_account={
                int(edge["account_id"]): 1 for edge in edges
            },
        )
        reserved_edges.extend(
            {
                **edge,
                "portfolio_plan_id": decision.plan.id,
                "portfolio_reserved_units": int(
                    decision.allocated_units_by_account.get(int(edge["account_id"]), 0)
                ),
            }
            for edge in edges
        )
    return rebuild_allocation_draft(
        draft,
        sources,
        reserved_edges,
        removed_reason="portfolio_behavior_budget_unallocated",
    )


def allocation_account_ids_by_message(
    plan: ViewAccountSourceAllocationPlan,
    *,
    serviceable_only: bool = False,
) -> dict[int, set[int]]:
    result: dict[int, set[int]] = {}
    for edge in plan.edge_set or []:
        if serviceable_only and int(edge.get("portfolio_reserved_units") or 0) <= 0:
            continue
        result.setdefault(int(edge["message_id"]), set()).add(
            int(edge["account_id"])
        )
    return result


def apply_view_allocation_targets(
    plan: ViewAccountSourceAllocationPlan,
    targets: dict[int, ChannelViewDailyMessageTarget],
) -> None:
    exposures = {
        int(item["message_id"]): int(item["assigned_exposure"])
        for item in plan.source_exposures or []
    }
    for message_id, target in targets.items():
        exposure = exposures.get(int(message_id), 0)
        target.daily_target_snapshot = exposure
        total = int(target.total_target_snapshot or 0)
        baseline = int(target.lifetime_confirmed_at_attach or 0)
        remaining = max(0, total - baseline) if total > 0 else exposure
        target.effective_target_snapshot = min(exposure, remaining)


def _merge_new_sources(
    existing: list[dict], observed: list[dict]
) -> tuple[list[dict], bool]:
    known = {item["source_identity"] for item in existing}
    additions = [item for item in observed if item["source_identity"] not in known]
    return existing + additions, bool(additions)


def _new_plan(
    task: Task,
    *,
    ledger: TaskDayLedger,
    participation: TaskParticipationUnitPlan,
    admission: PlanningAdmissionSnapshot,
    sources: list[dict],
    draft,
    mode: str,
    revision: int,
    supersedes: ViewAccountSourceAllocationPlan | None,
) -> ViewAccountSourceAllocationPlan:
    seed = _hash(
        {
            "task": task.id,
            "day": str(ledger.obligation_local_date),
            "algorithm": ALGORITHM_REVISION,
        }
    )
    payload = {
        "degrees": draft.account_degrees,
        "sources": draft.source_exposures,
        "edges": draft.edges,
    }
    return ViewAccountSourceAllocationPlan(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        task_day_ledger_id=ledger.id,
        participation_plan_id=participation.id,
        planning_admission_snapshot_id=admission.id,
        allocation_revision=revision,
        allocation_mode=mode,
        algorithm_revision=ALGORITHM_REVISION,
        source_set=sources,
        source_set_hash=_hash(sources),
        account_degrees=draft.account_degrees,
        source_exposures=draft.source_exposures,
        edge_set=draft.edges,
        edge_count=len(draft.edges),
        unallocated_sources=draft.unallocated_sources,
        decision=draft.decision,
        allocation_seed=seed,
        allocation_hash=_hash(payload),
        supersedes_plan_id=supersedes.id if supersedes else None,
    )


def _active_plan(
    session: Session, task: Task, ledger: TaskDayLedger
) -> ViewAccountSourceAllocationPlan | None:
    return session.scalar(
        select(ViewAccountSourceAllocationPlan).where(
            ViewAccountSourceAllocationPlan.task_id == task.id,
            ViewAccountSourceAllocationPlan.task_lifecycle_epoch
            == task.task_lifecycle_epoch,
            ViewAccountSourceAllocationPlan.task_day_ledger_id == ledger.id,
            ViewAccountSourceAllocationPlan.state == "active",
        )
    )


def _sources(
    messages: list[ChannelMessage],
    forbidden_account_ids_by_message: dict[int, set[int]],
) -> list[dict]:
    ordered = sorted(
        messages, key=lambda item: (item.published_at or item.created_at, item.id)
    )
    return [
        {
            "message_id": item.id,
            "remote_message_id": item.message_id,
            "source_revision_id": item.current_source_revision_id,
            "source_identity": f"message:{item.id}:remote:{item.message_id}",
            "forbidden_account_ids": sorted(
                int(account_id)
                for account_id in forbidden_account_ids_by_message.get(item.id, set())
            ),
        }
        for item in ordered
    ]


def _hash(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "UNACHIEVABLE",
    "allocation_account_ids_by_message",
    "apply_view_allocation_targets",
    "ensure_view_allocation_plan",
]
