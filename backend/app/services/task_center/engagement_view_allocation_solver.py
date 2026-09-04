from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .engagement_bipartite_matching import match_bipartite_capacity


ALGORITHM_REVISION = "view_bipartite_graph_v2"
NATURAL_MODE = "natural_auto"
EVERY_MESSAGE_MODE = "every_active_message"
UNACHIEVABLE = "view_allocation_unachievable"


@dataclass(frozen=True)
class AllocationDraft:
    account_degrees: list[dict]
    source_exposures: list[dict]
    edges: list[dict]
    unallocated_sources: list[dict]
    decision: str


def initial_allocation_draft(
    task_id: str,
    account_ids: list[int],
    sources: list[dict],
    *,
    config: dict,
) -> AllocationDraft:
    if not sources:
        return AllocationDraft([], [], [], [], "no_active_sources")
    caps = _degree_caps(task_id, account_ids, len(sources), config=config)
    desired = _desired_source_exposures(
        len(account_ids), len(sources), caps, config=config
    )
    if desired is None:
        return _unachievable_draft(caps, sources, "joint_capacity_infeasible")
    minimum_degree = min(
        len(sources), int(config.get("per_account_source_degree_min") or 2)
    )
    edges = _allocate_edges(
        task_id,
        account_ids,
        caps,
        sources=sources,
        desired=desired,
        minimum_degree=minimum_degree,
    )
    if len(edges) != sum(desired):
        return _unachievable_draft(caps, sources, "simple_graph_infeasible")
    return _draft_from_edges(caps, sources, edges)


def successor_allocation_draft(
    task_id: str,
    existing_degrees: list[dict],
    existing_edges: list[dict],
    *,
    prior_sources: list[dict],
    prior_missing: list[dict],
    sources: list[dict],
    config: dict,
) -> AllocationDraft:
    known = {item["source_identity"] for item in prior_sources}
    new_sources = [item for item in sources if item["source_identity"] not in known]
    degrees = [dict(item) for item in existing_degrees]
    edges = [dict(item) for item in existing_edges]
    if allocation_mode(config) == EVERY_MESSAGE_MODE:
        for degree in degrees:
            degree["degree_cap"] = len(sources)
    additions = _dynamic_edges(
        task_id,
        degrees,
        edges,
        new_sources=new_sources,
        total_source_count=len(sources),
        config=config,
    )
    if additions is None:
        missing = list(prior_missing)
        missing.extend(
            _unallocated(source, 1, "pending_first_full_day")
            for source in new_sources
        )
        return _draft_from_edges(degrees, sources, edges, unallocated=missing)
    return _draft_from_edges(
        degrees,
        sources,
        edges + additions,
        unallocated=list(prior_missing),
    )


def allocation_mode(config: dict) -> str:
    if config.get("every_active_message"):
        return EVERY_MESSAGE_MODE
    return str(config.get("view_exposure_mode") or NATURAL_MODE)


def _degree_caps(
    task_id: str,
    account_ids: list[int],
    source_count: int,
    *,
    config: dict,
) -> list[dict]:
    if allocation_mode(config) == EVERY_MESSAGE_MODE:
        return [_degree(account_id, source_count) for account_id in account_ids]
    lower = int(config.get("per_account_source_degree_min") or 2)
    upper = int(config.get("per_account_source_degree_max") or 4)
    return [
        _degree(
            account_id,
            lower + _stable_int(task_id, account_id) % (upper - lower + 1),
        )
        for account_id in account_ids
    ]


def _desired_source_exposures(
    account_count: int,
    source_count: int,
    degrees: list[dict],
    *,
    config: dict,
) -> list[int] | None:
    if allocation_mode(config) == EVERY_MESSAGE_MODE:
        return [account_count] * source_count
    if config.get("view_exposure_mode") == "explicit_per_source":
        target = _explicit_source_target(account_count, config)
        total = target * source_count
    else:
        total = sum(
            min(source_count, int(item["degree_cap"])) for item in degrees
        )
    minimum = account_count * min(
        source_count, int(config.get("per_account_source_degree_min") or 2)
    )
    capacity = sum(
        min(source_count, int(item["degree_cap"])) for item in degrees
    )
    if total < max(source_count, minimum) or total > capacity:
        return None
    exposures = [0] * source_count
    for index in range(total):
        exposures[index % source_count] += 1
    return exposures if max(exposures, default=0) <= account_count else None


def _allocate_edges(
    task_id: str,
    account_ids: list[int],
    degrees: list[dict],
    *,
    sources: list[dict],
    desired: list[int],
    minimum_degree: int,
) -> list[dict]:
    caps = {
        int(item["account_id"]): min(len(sources), int(item["degree_cap"]))
        for item in degrees
    }
    row_minimums = {account_id: minimum_degree for account_id in account_ids}
    return _solve_edges(
        task_id,
        account_ids,
        caps,
        row_minimums=row_minimums,
        sources=sources,
        desired=desired,
        existing_edges=[],
    )


def _dynamic_edges(
    task_id: str,
    degrees: list[dict],
    existing_edges: list[dict],
    *,
    new_sources: list[dict],
    total_source_count: int,
    config: dict,
) -> list[dict] | None:
    if not new_sources:
        return []
    account_ids = [int(item["account_id"]) for item in degrees]
    assigned = {
        int(item["account_id"]): int(item.get("assigned_degree") or 0)
        for item in degrees
    }
    caps = {
        int(item["account_id"]): int(item["degree_cap"])
        - assigned[int(item["account_id"])]
        for item in degrees
    }
    minimum = min(
        total_source_count,
        int(config.get("per_account_source_degree_min") or 2),
    )
    row_minimums = {
        account_id: max(0, minimum - assigned[account_id])
        for account_id in account_ids
    }
    desired = _dynamic_exposures(
        len(account_ids),
        len(new_sources),
        row_minimums=row_minimums,
        caps=caps,
        config=config,
    )
    if desired is None:
        return None
    additions = _solve_edges(
        task_id,
        account_ids,
        caps,
        row_minimums=row_minimums,
        sources=new_sources,
        desired=desired,
        existing_edges=existing_edges,
    )
    return additions or None


def _dynamic_exposures(
    account_count: int,
    source_count: int,
    *,
    row_minimums: dict[int, int],
    caps: dict[int, int],
    config: dict,
) -> list[int] | None:
    if allocation_mode(config) == EVERY_MESSAGE_MODE:
        desired = [account_count] * source_count
    elif config.get("view_exposure_mode") == "explicit_per_source":
        desired = [_explicit_source_target(account_count, config)] * source_count
    else:
        total = max(source_count, sum(row_minimums.values()))
        desired = [0] * source_count
        for index in range(total):
            desired[index % source_count] += 1
    if sum(desired) < sum(row_minimums.values()):
        return None
    if sum(desired) > sum(caps.values()):
        return None
    return desired if max(desired, default=0) <= account_count else None


def _solve_edges(
    task_id: str,
    account_ids: list[int],
    caps: dict[int, int],
    *,
    row_minimums: dict[int, int],
    sources: list[dict],
    desired: list[int],
    existing_edges: list[dict],
) -> list[dict]:
    used = _used_edges(existing_edges, sources)
    by_identity = {source["source_identity"]: source for source in sources}
    ranked = sorted(
        ((account_id, identity) for account_id in account_ids for identity in by_identity
         if (account_id, identity) not in used),
        key=lambda edge: _stable_int(task_id, *edge),
    )
    selected = match_bipartite_capacity(
        {key: (row_minimums[key], caps[key]) for key in account_ids},
        {source["source_identity"]: count for source, count in zip(sources, desired)},
        {edge: rank for rank, edge in enumerate(ranked)},
    )
    if selected is None:
        return []
    return [_edge(account_id, by_identity[identity]) for account_id, identity in selected]


def _used_edges(existing_edges: list[dict], sources: list[dict]) -> set:
    used = {
        (int(edge["account_id"]), edge["source_identity"])
        for edge in existing_edges
    }
    used.update(
        (int(account_id), source["source_identity"])
        for source in sources
        for account_id in source.get("forbidden_account_ids") or []
    )
    return used


def _draft_from_edges(
    degrees: list[dict],
    sources: list[dict],
    edges: list[dict],
    *,
    unallocated: list[dict] | None = None,
) -> AllocationDraft:
    account_counts = _counts(edges, "account_id")
    source_counts = _counts(edges, "source_identity")
    normalized_degrees = [
        {
            **item,
            "assigned_degree": account_counts.get(str(item["account_id"]), 0),
        }
        for item in degrees
    ]
    exposures = [
        {
            **source,
            "assigned_exposure": source_counts.get(source["source_identity"], 0),
        }
        for source in sources
    ]
    missing = list(unallocated or [])
    decision = "partially_serviceable" if missing else "achievable"
    return AllocationDraft(normalized_degrees, exposures, edges, missing, decision)


def rebuild_allocation_draft(
    draft: AllocationDraft,
    sources: list[dict],
    edges: list[dict],
    *,
    removed_reason: str,
) -> AllocationDraft:
    if draft.decision == UNACHIEVABLE:
        return draft
    desired = _counts(draft.edges, "source_identity")
    accepted = _counts(edges, "source_identity")
    missing = list(draft.unallocated_sources)
    for source in sources:
        identity = source["source_identity"]
        deficit = desired.get(identity, 0) - accepted.get(identity, 0)
        if deficit > 0:
            missing.append(_unallocated(source, deficit, removed_reason))
    return _draft_from_edges(
        draft.account_degrees,
        sources,
        edges,
        unallocated=missing,
    )


def _unachievable_draft(
    degrees: list[dict], sources: list[dict], reason: str
) -> AllocationDraft:
    missing = [_unallocated(source, 1, reason) for source in sources]
    exposures = [{**source, "assigned_exposure": 0} for source in sources]
    return AllocationDraft(degrees, exposures, [], missing, UNACHIEVABLE)


def _explicit_source_target(account_count: int, config: dict) -> int:
    target = config.get("per_source_exposure_target")
    if target is not None:
        return min(account_count, int(target))
    ratio = int(config.get("per_source_exposure_ratio_bps") or 0)
    return min(account_count, (account_count * ratio + 5000) // 10000)


def _degree(account_id: int, cap: int) -> dict:
    return {"account_id": account_id, "degree_cap": cap, "assigned_degree": 0}


def _edge(account_id: int, source: dict) -> dict:
    return {
        "account_id": account_id,
        "message_id": source["message_id"],
        "source_identity": source["source_identity"],
        "source_revision_id": source.get("source_revision_id"),
    }


def _unallocated(source: dict, count: int, reason: str) -> dict:
    return {
        "source_identity": source["source_identity"],
        "message_id": source["message_id"],
        "missing_exposure": count,
        "reason": reason,
    }


def _counts(items: list[dict], key: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in items:
        value = str(item[key])
        result[value] = result.get(value, 0) + 1
    return result


def _stable_int(*parts: object) -> int:
    return int(_hash(parts)[:16], 16)


def _hash(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "ALGORITHM_REVISION",
    "UNACHIEVABLE",
    "AllocationDraft",
    "allocation_mode",
    "initial_allocation_draft",
    "rebuild_allocation_draft",
    "successor_allocation_draft",
]
