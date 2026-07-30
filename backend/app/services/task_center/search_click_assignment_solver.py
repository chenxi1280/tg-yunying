from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from fractions import Fraction


@dataclass(frozen=True)
class SearchClickDemand:
    obligation_id: str
    task_id: str
    task_remaining_count: int = 1
    task_deadline_at: datetime | None = None
    task_last_opportunity_at: datetime | None = None
    persistent_task_cursor: int = 0
    task_cursor_version: int = 1


@dataclass(frozen=True)
class SearchClickCandidatePath:
    key: str
    account_id: int
    authorization_id: int
    keyword_hash: str
    proxy_route_id: str
    protocol_sample_version: str
    hard_safe_remaining_capacity: int
    confirmed_click_count_today: int
    last_click_opportunity_at: datetime | None
    persistent_account_cursor: int
    eligible_obligation_ids: tuple[str, ...] = ()
    resource_versions: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class SearchClickAssignmentMatch:
    obligation_id: str
    candidate_key: str
    candidate_unit_ordinal: int


@dataclass(frozen=True)
class SearchClickSolverResult:
    outcome: str
    matches: tuple[SearchClickAssignmentMatch, ...]
    unmatched_obligation_ids: tuple[str, ...]
    solver_problem_hash: str
    solver_input_hash: str
    outcome_hash: str


@dataclass
class _FlowEdge:
    target: int
    reverse_index: int
    capacity: int
    cost: int


def solve_search_click_assignments(
    demands: tuple[SearchClickDemand, ...],
    paths: tuple[SearchClickCandidatePath, ...],
    *,
    solver_problem_hash: str | None = None,
    solver_input_hash: str | None = None,
) -> SearchClickSolverResult:
    ordered_demands = tuple(sorted(demands, key=_demand_order))
    ordered_paths = tuple(sorted(paths, key=_path_order))
    problem_hash = solver_problem_hash or _problem_hash(
        ordered_demands,
        ordered_paths,
    )
    input_hash = solver_input_hash or _input_hash(problem_hash, ordered_paths)
    if not ordered_demands or not ordered_paths:
        return _result(
            ordered_demands,
            (),
            problem_hash=problem_hash,
            input_hash=input_hash,
        )
    max_count = _maximum_matching_count(ordered_demands, ordered_paths)
    matches = _fair_min_cost_matching(
        ordered_demands,
        ordered_paths,
        target_flow=max_count,
    )
    return _result(
        ordered_demands,
        matches,
        problem_hash=problem_hash,
        input_hash=input_hash,
    )


def _maximum_matching_count(
    demands: tuple[SearchClickDemand, ...],
    paths: tuple[SearchClickCandidatePath, ...],
) -> int:
    graph, source, sink, _ = _matching_graph(
        demands,
        paths,
        target_flow=len(demands),
    )
    count = 0
    while _augment_shortest_path(graph, source, sink):
        count += 1
    return count


def _fair_min_cost_matching(
    demands: tuple[SearchClickDemand, ...],
    paths: tuple[SearchClickCandidatePath, ...],
    *,
    target_flow: int,
) -> tuple[SearchClickAssignmentMatch, ...]:
    graph, source, sink, assignment_edges = _matching_graph(
        demands,
        paths,
        target_flow=target_flow,
    )
    for _ in range(target_flow):
        if not _augment_shortest_path(graph, source, sink):
            raise RuntimeError("search_click_solver_optimality_not_proven")
    return _matches_from_edges(assignment_edges)


def _matching_graph(
    demands: tuple[SearchClickDemand, ...],
    paths: tuple[SearchClickCandidatePath, ...],
    *,
    target_flow: int,
) -> tuple[list[list[_FlowEdge]], int, int, list[tuple]]:
    maps = _node_maps(demands, paths)
    task_nodes, demand_nodes, path_nodes, account_nodes = maps
    node_count = 2 + sum(len(item) for item in maps)
    graph: list[list[_FlowEdge]] = [[] for _ in range(node_count)]
    source, sink = 0, node_count - 1
    _add_task_edges(
        graph,
        source=source,
        demands=demands,
        task_nodes=task_nodes,
        target_flow=target_flow,
    )
    for demand in demands:
        _add_edge(
            graph,
            source=task_nodes[demand.task_id],
            target=demand_nodes[demand.obligation_id],
            capacity=1,
            cost=0,
        )
    assignments = _add_assignment_edges(
        graph,
        demands=demands,
        paths=paths,
        demand_nodes=demand_nodes,
        path_nodes=path_nodes,
    )
    for path in paths:
        _add_edge(
            graph,
            source=path_nodes[path.key],
            target=account_nodes[path.account_id],
            capacity=max(0, path.hard_safe_remaining_capacity),
            cost=0,
        )
    for account_id, capacity in _account_capacities(paths).items():
        _add_edge(
            graph,
            source=account_nodes[account_id],
            target=sink,
            capacity=capacity,
            cost=0,
        )
    return graph, source, sink, assignments


def _node_maps(
    demands: tuple[SearchClickDemand, ...],
    paths: tuple[SearchClickCandidatePath, ...],
) -> tuple:
    demand_by_task = {
        item.task_id: item
        for item in reversed(demands)
    }
    task_ids = tuple(sorted(
        demand_by_task,
        key=lambda task_id: _demand_order(demand_by_task[task_id]),
    ))
    task_nodes = {task_id: 1 + index for index, task_id in enumerate(task_ids)}
    demand_offset = 1 + len(task_ids)
    path_offset = demand_offset + len(demands)
    account_offset = path_offset + len(paths)
    demand_nodes = {
        item.obligation_id: demand_offset + index
        for index, item in enumerate(demands)
    }
    path_nodes = {
        item.key: path_offset + index for index, item in enumerate(paths)
    }
    account_nodes = {
        account_id: account_offset + index
        for index, account_id in enumerate(sorted({item.account_id for item in paths}))
    }
    return task_nodes, demand_nodes, path_nodes, account_nodes


def _demand_order(demand: SearchClickDemand) -> tuple:
    return (
        demand.task_deadline_at.isoformat() if demand.task_deadline_at else "",
        demand.task_last_opportunity_at.isoformat()
        if demand.task_last_opportunity_at
        else "",
        demand.persistent_task_cursor,
        demand.task_id,
        demand.obligation_id,
    )


def _add_task_edges(
    graph: list[list[_FlowEdge]],
    *,
    source: int,
    demands: tuple[SearchClickDemand, ...],
    task_nodes: dict[str, int],
    target_flow: int,
) -> None:
    by_task = {
        task_id: [item for item in demands if item.task_id == task_id]
        for task_id in task_nodes
    }
    levels = sorted({
        Fraction(ordinal - 1, max(1, rows[0].task_remaining_count))
        for rows in by_task.values()
        for ordinal in range(1, len(rows) + 1)
    })
    level_rank = {level: index for index, level in enumerate(levels)}
    base = max(2, target_flow + 1)
    fairness_bound = target_flow * (base ** max(1, len(levels)))
    served_weight = fairness_bound + len(task_nodes) + 1
    for task_rank, task_id in enumerate(task_nodes):
        rows = by_task[task_id]
        remaining = max(1, rows[0].task_remaining_count)
        for ordinal in range(1, len(rows) + 1):
            rank = level_rank[Fraction(ordinal - 1, remaining)]
            fairness_cost = base ** rank
            served_bonus = -served_weight + task_rank if ordinal == 1 else 0
            _add_edge(
                graph,
                source=source,
                target=task_nodes[task_id],
                capacity=1,
                cost=served_bonus + fairness_cost,
            )


def _add_assignment_edges(
    graph: list[list[_FlowEdge]],
    *,
    demands: tuple[SearchClickDemand, ...],
    paths: tuple[SearchClickCandidatePath, ...],
    demand_nodes: dict[str, int],
    path_nodes: dict[str, int],
) -> list[tuple[str, str, _FlowEdge]]:
    path_rank = {path.key: index for index, path in enumerate(paths)}
    result = []
    for demand in demands:
        for path in paths:
            if (
                path.eligible_obligation_ids
                and demand.obligation_id not in path.eligible_obligation_ids
            ):
                continue
            edge = _add_edge(
                graph,
                source=demand_nodes[demand.obligation_id],
                target=path_nodes[path.key],
                capacity=1,
                cost=path_rank[path.key],
            )
            result.append((demand.obligation_id, path.key, edge))
    return result


def _add_edge(
    graph: list[list[_FlowEdge]],
    *,
    source: int,
    target: int,
    capacity: int,
    cost: int,
) -> _FlowEdge:
    forward = _FlowEdge(target, len(graph[target]), capacity, cost)
    reverse = _FlowEdge(source, len(graph[source]), 0, -cost)
    graph[source].append(forward)
    graph[target].append(reverse)
    return forward


def _augment_shortest_path(
    graph: list[list[_FlowEdge]],
    source: int,
    sink: int,
) -> bool:
    distance = [None] * len(graph)
    parent: list[tuple[int, int] | None] = [None] * len(graph)
    distance[source] = 0
    for _ in range(len(graph) - 1):
        changed = _relax_edges(graph, distance, parent)
        if not changed:
            break
    if distance[sink] is None:
        return False
    node = sink
    while node != source:
        previous, edge_index = parent[node]
        edge = graph[previous][edge_index]
        edge.capacity -= 1
        graph[node][edge.reverse_index].capacity += 1
        node = previous
    return True


def _relax_edges(
    graph: list[list[_FlowEdge]],
    distance: list[int | None],
    parent: list[tuple[int, int] | None],
) -> bool:
    changed = False
    for source, edges in enumerate(graph):
        if distance[source] is None:
            continue
        for index, edge in enumerate(edges):
            next_distance = distance[source] + edge.cost
            if edge.capacity <= 0 or (
                distance[edge.target] is not None
                and next_distance >= distance[edge.target]
            ):
                continue
            distance[edge.target] = next_distance
            parent[edge.target] = (source, index)
            changed = True
    return changed


def _matches_from_edges(
    assignment_edges: list[tuple[str, str, _FlowEdge]],
) -> tuple[SearchClickAssignmentMatch, ...]:
    matched = sorted(
        (obligation_id, candidate_key)
        for obligation_id, candidate_key, edge in assignment_edges
        if edge.capacity == 0
    )
    ordinals: dict[str, int] = {}
    result = []
    for obligation_id, candidate_key in matched:
        ordinal = ordinals.get(candidate_key, 0) + 1
        ordinals[candidate_key] = ordinal
        result.append(SearchClickAssignmentMatch(
            obligation_id,
            candidate_key,
            ordinal,
        ))
    return tuple(result)


def _account_capacities(
    paths: tuple[SearchClickCandidatePath, ...],
) -> dict[int, int]:
    capacities: dict[int, int] = {}
    for path in paths:
        capacities[path.account_id] = max(
            capacities.get(path.account_id, 0),
            min(1, max(0, int(path.hard_safe_remaining_capacity))),
        )
    return capacities


def _path_order(path: SearchClickCandidatePath) -> tuple:
    last_at = (
        path.last_click_opportunity_at.isoformat()
        if path.last_click_opportunity_at
        else ""
    )
    return (
        -int(path.hard_safe_remaining_capacity),
        int(path.confirmed_click_count_today),
        last_at,
        int(path.persistent_account_cursor),
        path.key,
    )


def _problem_hash(
    demands: tuple[SearchClickDemand, ...],
    paths: tuple[SearchClickCandidatePath, ...],
) -> str:
    value = {
        "demands": [
            (
                item.obligation_id,
                item.task_id,
                item.task_remaining_count,
                item.task_deadline_at.isoformat()
                if item.task_deadline_at
                else None,
                item.task_last_opportunity_at.isoformat()
                if item.task_last_opportunity_at
                else None,
                item.persistent_task_cursor,
                item.task_cursor_version,
            )
            for item in demands
        ],
        "paths": [
            {
                "key": item.key,
                "account_id": item.account_id,
                "authorization_id": item.authorization_id,
                "keyword_hash": item.keyword_hash,
                "proxy_route_id": item.proxy_route_id,
                "protocol_sample_version": item.protocol_sample_version,
                "capacity": item.hard_safe_remaining_capacity,
                "eligible": list(item.eligible_obligation_ids),
                "resource_versions": list(item.resource_versions),
            }
            for item in paths
        ],
    }
    return _hash(value)


def _input_hash(
    problem_hash: str,
    paths: tuple[SearchClickCandidatePath, ...],
) -> str:
    return _hash({
        "problem_hash": problem_hash,
        "ordering": [
            (
                item.key,
                item.confirmed_click_count_today,
                item.last_click_opportunity_at.isoformat()
                if item.last_click_opportunity_at
                else None,
                item.persistent_account_cursor,
            )
            for item in paths
        ],
    })


def _result(
    demands: tuple[SearchClickDemand, ...],
    matches: tuple[SearchClickAssignmentMatch, ...],
    *,
    problem_hash: str,
    input_hash: str,
) -> SearchClickSolverResult:
    matched_ids = {item.obligation_id for item in matches}
    unmatched = tuple(
        item.obligation_id for item in demands if item.obligation_id not in matched_ids
    )
    outcome = "optimal" if matches else "no_candidate"
    outcome_hash = _hash({
        "outcome": outcome,
        "matches": [
            (item.obligation_id, item.candidate_key, item.candidate_unit_ordinal)
            for item in matches
        ],
        "unmatched": unmatched,
    })
    return SearchClickSolverResult(
        outcome,
        matches,
        unmatched,
        problem_hash,
        input_hash,
        outcome_hash,
    )


def _hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["SearchClickAssignmentMatch", "SearchClickCandidatePath", "SearchClickDemand",
           "SearchClickSolverResult", "solve_search_click_assignments"]
