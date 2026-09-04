"""Exact capacity matching with hard row minima and optional edge costs."""

from __future__ import annotations

from dataclasses import dataclass
from heapq import heappop, heappush


@dataclass(frozen=True)
class _Arc:
    source: int
    target: int
    cost: int


class _ResidualNetwork:
    def __init__(self, size: int) -> None:
        self.adjacency: list[list[int]] = [[] for _ in range(size)]
        self.arcs: list[_Arc] = []
        self.capacity: list[int] = []

    def add(self, endpoints: tuple[int, int], *, capacity: int, cost: int) -> int:
        source, target = endpoints
        index = len(self.arcs)
        self.arcs.extend((_Arc(source, target, cost), _Arc(target, source, -cost)))
        self.capacity.extend((capacity, 0))
        self.adjacency[source].append(index)
        self.adjacency[target].append(index + 1)
        return index

    def push(self, required: int) -> bool:
        potentials = [0] * len(self.adjacency)
        delivered = 0
        while delivered < required:
            distances, previous = self._shortest_path(potentials)
            if previous[1] is None:
                return False
            for node, distance in enumerate(distances):
                if distance is not None:
                    potentials[node] += distance
            path = self._path(previous)
            amount = min(required - delivered, *(self.capacity[i] for i in path))
            for index in path:
                self.capacity[index] -= amount
                self.capacity[index ^ 1] += amount
            delivered += amount
        return True

    def _shortest_path(self, potentials: list[int]):
        distances: list[int | None] = [None] * len(self.adjacency)
        previous: list[int | None] = [None] * len(self.adjacency)
        distances[0] = 0
        queue = [(0, 0)]
        while queue:
            distance, node = heappop(queue)
            if distance != distances[node]:
                continue
            for index in self.adjacency[node]:
                if self.capacity[index] <= 0:
                    continue
                arc = self.arcs[index]
                candidate = distance + arc.cost + potentials[node] - potentials[arc.target]
                if distances[arc.target] is not None and candidate >= distances[arc.target]:
                    continue
                distances[arc.target] = candidate
                previous[arc.target] = index
                heappush(queue, (candidate, arc.target))
        return distances, previous

    def _path(self, previous: list[int | None]) -> list[int]:
        result = []
        node = 1
        while node != 0:
            index = previous[node]
            if index is None:
                raise ValueError("matching_augmenting_path_incomplete")
            result.append(index)
            node = self.arcs[index].source
        return result


def match_bipartite_capacity(
    account_bounds: dict[int, tuple[int, int]],
    source_counts: dict[str, int],
    edge_costs: dict[tuple[int, str], int],
) -> list[tuple[int, str]] | None:
    """Return an exact minimum-cost simple graph, or None if infeasible.

    First-unit arcs encode row minima ahead of all optional cost objectives.
    Residual reverse arcs allow earlier assignments to be displaced, unlike a
    greedy allocator. Inputs are immutable for the duration of the solve.
    """
    required = sum(source_counts.values())
    if not _valid_bounds(account_bounds, source_counts, required=required):
        return None
    if any(cost < 0 for cost in edge_costs.values()):
        raise ValueError("matching_edge_cost_negative")
    network, edge_indices = _build_network(account_bounds, source_counts, edge_costs)
    if not network.push(required):
        return None
    selected = [edge for edge, index in edge_indices.items() if network.capacity[index] == 0]
    counts = {account_id: 0 for account_id in account_bounds}
    for account_id, _ in selected:
        counts[account_id] += 1
    if any(counts[key] < bounds[0] for key, bounds in account_bounds.items()):
        return None
    return sorted(selected)


def _valid_bounds(account_bounds, source_counts, *, required: int) -> bool:
    return (
        all(0 <= lower <= upper for lower, upper in account_bounds.values())
        and all(count >= 0 for count in source_counts.values())
        and sum(lower for lower, _ in account_bounds.values()) <= required
        and required <= sum(upper for _, upper in account_bounds.values())
    )


def _build_network(account_bounds, source_counts, edge_costs):
    accounts = {key: index + 2 for index, key in enumerate(sorted(account_bounds))}
    sources = {
        key: index + 2 + len(accounts) for index, key in enumerate(sorted(source_counts))
    }
    network = _ResidualNetwork(2 + len(accounts) + len(sources))
    # One missed minimum must cost more than every possible edge objective.
    penalty = (max(edge_costs.values(), default=0) + 1) * sum(source_counts.values()) + 1
    for account_id, node in accounts.items():
        lower, upper = account_bounds[account_id]
        network.add((0, node), capacity=lower, cost=0)
        network.add((0, node), capacity=upper - lower, cost=penalty)
    for identity, node in sources.items():
        network.add((node, 1), capacity=source_counts[identity], cost=0)
    indices = {}
    for edge, cost in sorted(edge_costs.items()):
        account_id, identity = edge
        if account_id not in accounts or identity not in sources:
            raise ValueError("matching_edge_endpoint_missing")
        indices[edge] = network.add(
            (accounts[account_id], sources[identity]), capacity=1, cost=cost,
        )
    return network, indices
