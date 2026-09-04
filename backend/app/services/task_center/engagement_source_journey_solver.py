from __future__ import annotations

import hashlib
import json


ACTION_ORDER = ("authored_comment", "reaction", "view")
ACTION_RANK = {value: index for index, value in enumerate(ACTION_ORDER)}


def solve_source_journey(
    constraints: list[dict],
    seed: str,
    *,
    frozen_edges: list[dict],
) -> tuple[list[dict], dict, list[dict], str]:
    frozen = _frozen_by_demand(frozen_edges, constraints)
    deficits = _hard_deficits(constraints, frozen)
    if deficits:
        return (
            [],
            _overlap_metrics({}, constraints),
            deficits,
            "cross_adapter_journey_unachievable",
        )
    selected = _select_accounts(constraints, seed, frozen=frozen)
    edges = _edges(selected)
    metrics = _overlap_metrics(selected, constraints)
    decision = (
        "feasible_degraded"
        if metrics["reaction_comment_overlap"]
        > metrics["minimum_reaction_comment_overlap"]
        else "feasible"
    )
    return edges, metrics, [], decision


def _hard_deficits(constraints, frozen) -> list[dict]:
    deficits = []
    for item in constraints:
        frozen_ids = frozen.get(_demand_key(item), set())
        available = set(item["candidate_account_ids"]) | frozen_ids
        if len(frozen_ids) > item["required_count"]:
            reason = "frozen_edge_count_exceeds_required_count"
        elif item["required_count"] > len(available):
            reason = "eligible_account_capacity_insufficient"
        else:
            continue
        deficit = {
            "task_id": item["task_id"],
            "action_class": item["action_class"],
            "required_count": item["required_count"],
            "available_count": len(available),
            "reason": reason,
        }
        if frozen_ids:
            deficit["frozen_count"] = len(frozen_ids)
        deficits.append(deficit)
    return deficits


def _select_accounts(constraints, seed, *, frozen) -> dict[tuple[str, str], dict]:
    selected: dict[tuple[str, str], dict] = {}
    reaction_candidates = _candidate_ids(constraints, "reaction")
    for demand in constraints:
        key = _demand_key(demand)
        chosen = _select_for_demand(
            demand,
            seed=seed,
            selected=selected,
            reaction_candidates=reaction_candidates,
            frozen_ids=frozen.get(key, set()),
        )
        selected[key] = {**demand, "account_ids": chosen}
    return selected


def _select_for_demand(
    demand,
    *,
    seed,
    selected,
    reaction_candidates,
    frozen_ids,
):
    action_class = demand["action_class"]
    preferred = set(demand.get("preferred_account_ids") or [])
    candidates = [
        account_id
        for account_id in demand["candidate_account_ids"]
        if account_id not in frozen_ids
    ]
    if action_class == "authored_comment":
        candidates.sort(key=lambda account_id: (
            account_id in reaction_candidates,
            account_id not in preferred,
            _rank(seed, demand, account_id),
        ))
    elif action_class == "reaction":
        comments = _selected_ids(selected, "authored_comment")
        candidates.sort(key=lambda account_id: (
            account_id in comments,
            account_id not in preferred,
            _rank(seed, demand, account_id),
        ))
    else:
        prior = _all_selected_ids(selected)
        candidates.sort(key=lambda account_id: (
            prior.get(account_id, 0),
            account_id not in preferred,
            _rank(seed, demand, account_id),
        ))
    needed = int(demand["required_count"]) - len(frozen_ids)
    return sorted(frozen_ids) + candidates[:needed]


def _edges(selected) -> list[dict]:
    action_sets = {
        action_class: _selected_ids(selected, action_class)
        for action_class in ACTION_ORDER
    }
    edges = [
        {
            "task_id": item["task_id"],
            "action_class": item["action_class"],
            "account_id": account_id,
            "journey_class": _journey_class(account_id, action_sets),
        }
        for item in selected.values()
        for account_id in item["account_ids"]
    ]
    return sorted(edges, key=lambda edge: (
        ACTION_RANK[edge["action_class"]], edge["task_id"], edge["account_id"],
    ))


def _journey_class(account_id, action_sets) -> str:
    commented = account_id in action_sets.get("authored_comment", set())
    reacted = account_id in action_sets.get("reaction", set())
    if commented and reacted:
        return "reaction_and_comment"
    if commented:
        return "comment"
    if reacted:
        return "reaction"
    return "read_only"


def _overlap_metrics(selected, constraints) -> dict:
    comments = _selected_ids(selected, "authored_comment")
    reactions = _selected_ids(selected, "reaction")
    views = _selected_ids(selected, "view")
    eligible = _joint_candidate_ids(constraints, {"authored_comment", "reaction"})
    lower_bound = max(0, len(comments) + len(reactions) - len(eligible))
    return {
        "comment_count": len(comments),
        "reaction_count": len(reactions),
        "view_count": len(views),
        "reaction_comment_overlap": len(comments & reactions),
        "minimum_reaction_comment_overlap": lower_bound,
        "triple_overlap": len(comments & reactions & views),
    }


def _selected_ids(selected, action_class) -> set[int]:
    return {
        int(account_id)
        for item in selected.values()
        if item.get("action_class") == action_class
        for account_id in item.get("account_ids", [])
    }


def _all_selected_ids(selected) -> dict[int, int]:
    counts: dict[int, int] = {}
    for item in selected.values():
        for account_id in item["account_ids"]:
            counts[account_id] = counts.get(account_id, 0) + 1
    return counts


def _joint_candidate_ids(constraints, action_classes) -> set[int]:
    return {
        int(account_id)
        for item in constraints
        if item["action_class"] in action_classes
        for account_id in item["candidate_account_ids"]
    }


def _candidate_ids(constraints, action_class) -> set[int]:
    return {
        int(account_id)
        for item in constraints
        if item["action_class"] == action_class
        for account_id in item["candidate_account_ids"]
    }


def _frozen_by_demand(
    frozen_edges,
    constraints,
) -> dict[tuple[str, str], set[int]]:
    result = {
        _demand_key(item): {int(value) for value in item.get("hard_account_ids", [])}
        for item in constraints
    }
    for edge in frozen_edges:
        key = (str(edge["task_id"]), str(edge["action_class"]))
        if key in result:
            result[key].add(int(edge["account_id"]))
    return result


def _demand_key(item) -> tuple[str, str]:
    return str(item["task_id"]), str(item["action_class"])


def _rank(seed: str, demand: dict, account_id: int) -> str:
    return _hash({
        "seed": seed,
        "task": demand["task_id"],
        "class": demand["action_class"],
        "account": account_id,
    })


def _hash(value) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


__all__ = ["ACTION_ORDER", "ACTION_RANK", "solve_source_journey"]
