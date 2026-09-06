from itertools import product

import pytest

from app.services.task_center.engagement_bipartite_matching import match_bipartite_capacity
from app.services.task_center.engagement_view_allocation_solver import initial_allocation_draft


pytestmark = pytest.mark.no_postgres


def test_view_graph_reassigns_earlier_choice_to_cover_restricted_account() -> None:
    sources = [
        {"source_identity": str(i), "message_id": i, "forbidden_account_ids": [1] if i == 2 else []}
        for i in range(3)
    ]
    draft = initial_allocation_draft(
        "regression", [1, 2, 3], sources,
        config={"per_account_source_degree_min": 1, "per_account_source_degree_max": 1},
    )
    assert draft.decision == "achievable"
    assert len(draft.edges) == 3
    assert {row["assigned_degree"] for row in draft.account_degrees} == {1}


def test_view_graph_converges_degree_when_account_forbidden_on_sources() -> None:
    sources = [
        {"source_identity": "0", "message_id": 0, "forbidden_account_ids": [1]},
        {"source_identity": "1", "message_id": 1, "forbidden_account_ids": []},
    ]
    draft = initial_allocation_draft(
        "test_converge", [1, 2, 3], sources,
        config={"per_account_source_degree_min": 2, "per_account_source_degree_max": 2},
    )
    assert draft.decision == "achievable"
    assert len(draft.edges) == 5
    degree_map = {row["account_id"]: row["assigned_degree"] for row in draft.account_degrees}
    assert degree_map[1] == 1  # account 1 cannot view source 0, so degree converges to 1
    assert degree_map[2] == 2
    assert degree_map[3] == 2


def test_all_three_by_three_graphs_match_exhaustive_feasibility_oracle() -> None:
    all_edges = [(account, source) for account in range(3) for source in ("a", "b", "c")]
    for mask in range(1 << len(all_edges)):
        costs = {edge: 0 for index, edge in enumerate(all_edges) if mask & (1 << index)}
        feasible = any(
            len(set(choice)) == 3
            and all((account, source) in costs for account, source in enumerate(choice))
            for choice in product(("a", "b", "c"), repeat=3)
        )
        selected = match_bipartite_capacity(
            {account: (1, 1) for account in range(3)}, {"a": 1, "b": 1, "c": 1}, costs,
        )
        assert (selected is not None) == feasible, mask


def test_minimum_coverage_precedes_any_overlap_objective() -> None:
    selected = match_bipartite_capacity(
        {1: (1, 2), 2: (1, 2)}, {"a": 1, "b": 1},
        {(1, "a"): 0, (1, "b"): 0, (2, "a"): 100, (2, "b"): 100},
    )
    assert {account for account, _ in selected} == {1, 2}


def test_cost_objective_is_minimized_without_changing_exposure_or_degree() -> None:
    selected = match_bipartite_capacity(
        {1: (1, 1), 2: (1, 1)}, {"a": 1, "b": 1},
        {(1, "a"): 5, (1, "b"): 0, (2, "a"): 0, (2, "b"): 5},
    )
    assert selected == [(1, "b"), (2, "a")]


def test_unreachable_minimum_is_not_reported_as_feasible_optional_flow() -> None:
    assert match_bipartite_capacity(
        {1: (1, 2), 2: (1, 2)}, {"a": 1, "b": 1},
        {(1, "a"): 0, (1, "b"): 0},
    ) is None


def test_variable_capacity_costs_match_exhaustive_oracle_and_inputs_stay_unchanged() -> None:
    edges = [(account, source) for account in (1, 2) for source in ("a", "b", "c")]
    bounds = {1: (1, 2), 2: (1, 3)}
    counts = {"a": 1, "b": 1, "c": 1}
    for mask in range(1 << len(edges)):
        costs = {edge: (index * 7) % 11 for index, edge in enumerate(edges) if mask & (1 << index)}
        expected = _oracle_cost(bounds, counts, costs)
        selected = match_bipartite_capacity(bounds, counts, costs)
        assert (selected is None) == (expected is None), mask
        if selected is not None:
            assert sum(costs[edge] for edge in selected) == expected, mask
        assert bounds == {1: (1, 2), 2: (1, 3)}
        assert counts == {"a": 1, "b": 1, "c": 1}
        assert costs == {edge: (index * 7) % 11 for index, edge in enumerate(edges) if mask & (1 << index)}


def _oracle_cost(bounds, counts, costs):
    feasible_costs = []
    for included in product((False, True), repeat=len(costs)):
        chosen = [edge for edge, enabled in zip(costs, included) if enabled]
        if any(sum(source == key for _, source in chosen) != count for key, count in counts.items()):
            continue
        if any(not lower <= sum(account == key for account, _ in chosen) <= upper
               for key, (lower, upper) in bounds.items()):
            continue
        feasible_costs.append(sum(costs[edge] for edge in chosen))
    return min(feasible_costs, default=None)
