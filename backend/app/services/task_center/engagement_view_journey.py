"""Compile source journeys over the whole view graph before allocation commit."""

from __future__ import annotations

import hashlib
import json
from collections import Counter

from sqlalchemy import select

from app.models import ChannelMessageSourceRevision, CrossAdapterSourceJourneyPlanRevision

from .engagement_bipartite_matching import match_bipartite_capacity
from .engagement_source_journey import JourneyDemand, register_source_journey_demand
from .engagement_view_allocation_solver import UNACHIEVABLE, rebuild_allocation_draft


def compile_view_source_journeys(
    session, task, ledger, *, sources, draft, participation, frozen_edges,
):
    if draft.decision == UNACHIEVABLE:
        return draft
    revisions = _source_revisions(session, task, sources)
    plans = _source_plans(session, task, ledger, revisions=revisions)
    candidates = _source_candidates(sources, participation)
    edges = _match_joint_graph(
        draft, sources, candidates=candidates, plans=plans,
        frozen_edges=frozen_edges, seed=task.id,
    )
    graph_hash = _hash([
        (edge["account_id"], edge["source_identity"]) for edge in edges
    ])
    bound = _bind_source_journeys(
        session, task, ledger, sources=sources, revisions=revisions,
        candidates=candidates, edges=edges, graph_hash=graph_hash,
    )
    return rebuild_allocation_draft(
        draft, sources, bound, removed_reason="cross_adapter_journey_unallocated",
    )


def _source_revisions(session, task, sources):
    source_ids = {str(source.get("source_revision_id") or "") for source in sources}
    rows = list(session.scalars(select(ChannelMessageSourceRevision).where(
        ChannelMessageSourceRevision.id.in_(source_ids),
        ChannelMessageSourceRevision.tenant_id == task.tenant_id,
    ).order_by(ChannelMessageSourceRevision.id).with_for_update()))
    by_id = {row.id: row for row in rows}
    for source in sources:
        row = by_id.get(source.get("source_revision_id"))
        if row is None or row.channel_message_id != int(source["message_id"]):
            raise ValueError("view_source_revision_unproven")
    return by_id


def _source_plans(session, task, ledger, *, revisions):
    rows = session.scalars(select(CrossAdapterSourceJourneyPlanRevision).where(
        CrossAdapterSourceJourneyPlanRevision.tenant_id == task.tenant_id,
        CrossAdapterSourceJourneyPlanRevision.source_revision_id.in_(revisions),
        CrossAdapterSourceJourneyPlanRevision.task_day == ledger.obligation_local_date,
        CrossAdapterSourceJourneyPlanRevision.state == "active",
    ).order_by(CrossAdapterSourceJourneyPlanRevision.source_revision_id).with_for_update())
    return {row.source_revision_id: row for row in rows}


def _source_candidates(sources, participation):
    cohort = set(map(int, participation.selected_account_ids or []))
    return {
        source["source_identity"]: sorted(
            cohort - set(map(int, source.get("forbidden_account_ids") or []))
        )
        for source in sources
    }


def _match_joint_graph(draft, sources, *, candidates, plans, frozen_edges, seed):
    by_identity = {source["source_identity"]: source for source in sources}
    frozen = [dict(edge) for edge in frozen_edges]
    frozen_keys = {(int(edge["account_id"]), edge["source_identity"]) for edge in frozen}
    account_used = Counter(account for account, _ in frozen_keys)
    source_used = Counter(identity for _, identity in frozen_keys)
    degrees = {
        int(row["account_id"]): int(row["assigned_degree"]) - account_used[int(row["account_id"])]
        for row in draft.account_degrees
    }
    exposures = {
        row["source_identity"]: int(row["assigned_exposure"]) - source_used[row["source_identity"]]
        for row in draft.source_exposures
    }
    available = {
        (account_id, identity)
        for identity, account_ids in candidates.items()
        for account_id in account_ids
        if account_id in degrees and (account_id, identity) not in frozen_keys
    }
    selected = match_bipartite_capacity(
        {key: (value, value) for key, value in degrees.items()}, exposures,
        _edge_objective_costs(available, by_identity, plans=plans, seed=seed),
    )
    if selected is None:
        raise ValueError("cross_adapter_view_graph_hard_constraints_unachievable")
    additions = [_edge(account_id, by_identity[identity]) for account_id, identity in selected]
    return frozen + additions


def _edge_objective_costs(available, sources, *, plans, seed):
    ranked = sorted(available, key=lambda edge: _hash([seed, *edge]))
    loads = {edge: _edge_load(edge, sources, plans=plans) for edge in ranked}
    # Each weight exceeds the maximum summed cost of every lower priority.
    rank_weight = len(ranked) ** 2 + 1
    load_weight = (sum(load for _, load in loads.values()) + 1) * rank_weight
    return {
        edge: loads[edge][0] * load_weight + loads[edge][1] * rank_weight + rank
        for rank, edge in enumerate(ranked)
    }


def _edge_load(edge, sources, *, plans):
    account_id, identity = edge
    plan = plans.get(sources[identity].get("source_revision_id"))
    classes = [
        item["action_class"] for item in (plan.edge_set or [])
        if int(item["account_id"]) == account_id and item["action_class"] != "view"
    ] if plan else []
    return int("authored_comment" in classes and "reaction" in classes), len(classes)


def _bind_source_journeys(
    session, task, ledger, *, sources, revisions, candidates, edges, graph_hash,
):
    result = []
    for source in sources:
        selected = [edge for edge in edges if edge["source_identity"] == source["source_identity"]]
        if not selected:
            continue
        if all(edge.get("source_journey_plan_id") for edge in selected):
            result.extend(selected)
            continue
        account_ids = tuple(sorted(int(edge["account_id"]) for edge in selected))
        journey = register_source_journey_demand(
            session, revisions[source["source_revision_id"]],
            task_day=ledger.obligation_local_date,
            demand=JourneyDemand(
                task_id=task.id, action_class="view", required_count=len(account_ids),
                candidate_account_ids=tuple(candidates[source["source_identity"]]),
                hard_account_ids=account_ids, joint_constraint_hash=graph_hash,
            ),
        )
        if not journey.achievable:
            raise ValueError("cross_adapter_view_graph_registration_conflict")
        actual = journey.account_ids_by_task_action.get((task.id, "view"), ())
        if tuple(actual) != account_ids:
            raise ValueError("cross_adapter_view_graph_witness_mismatch")
        result.extend({**edge, "source_journey_plan_id": journey.plan.id} for edge in selected)
    return result


def _edge(account_id, source):
    return {
        "account_id": int(account_id), "message_id": int(source["message_id"]),
        "source_identity": source["source_identity"],
        "source_revision_id": source["source_revision_id"],
    }


def _hash(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
