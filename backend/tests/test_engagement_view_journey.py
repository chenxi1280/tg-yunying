from types import SimpleNamespace

import pytest

from app.services.task_center.engagement_view_allocation_solver import AllocationDraft
from app.services.task_center.engagement_view_journey import _match_joint_graph
from app.services.task_center.executors.channel_view import _new_view_plan_inputs


pytestmark = pytest.mark.no_postgres


def _inputs():
    sources = [
        {"source_identity": key, "source_revision_id": key, "message_id": index}
        for index, key in enumerate(("a", "b"))
    ]
    draft = AllocationDraft(
        [{"account_id": key, "degree_cap": 1, "assigned_degree": 1} for key in (1, 2)],
        [{**row, "assigned_exposure": 1} for row in sources],
        [], [], "achievable",
    )
    plans = {
        key: SimpleNamespace(edge_set=[{"account_id": account, "action_class": "authored_comment"}])
        for key, account in (("a", 1), ("b", 2))
    }
    return sources, draft, plans


def test_joint_graph_optimizes_overlap_across_sources_with_exact_degrees() -> None:
    sources, draft, plans = _inputs()
    edges = _match_joint_graph(
        draft, sources, candidates={"a": [1, 2], "b": [1, 2]}, plans=plans,
        frozen_edges=[], seed="stable",
    )
    assert {(row["account_id"], row["source_identity"]) for row in edges} == {(1, "b"), (2, "a")}


def test_joint_graph_does_not_optimize_away_frozen_edges_or_their_plan_identity() -> None:
    sources, draft, plans = _inputs()
    frozen = {"account_id": 1, **sources[0], "source_journey_plan_id": "prior-plan"}
    edges = _match_joint_graph(
        draft, sources, candidates={"a": [2], "b": [1, 2]}, plans=plans,
        frozen_edges=[frozen], seed="stable",
    )
    assert frozen in edges
    assert {(row["account_id"], row["source_identity"]) for row in edges} == {(1, "a"), (2, "b")}
    assert frozen == {"account_id": 1, **sources[0], "source_journey_plan_id": "prior-plan"}


def test_execution_input_excludes_unreserved_edges_without_mutating_requirement_graph() -> None:
    plan = SimpleNamespace(edge_set=[
        {"message_id": 10, "account_id": 1, "portfolio_reserved_units": 1},
        {"message_id": 10, "account_id": 2, "portfolio_reserved_units": 0},
    ])
    scope = SimpleNamespace(messages=[], ledger=None, targets_by_message={}, now=None)
    inputs = _new_view_plan_inputs(
        scope, accounts=[], allocation_plan=plan, task_remaining_today=2,
        daily_counts_by_account={}, account_ids_by_message={}, materialized_ids_by_message={},
    )
    assert inputs.allowed_account_ids_by_message == {10: {1}}
    assert len(plan.edge_set) == 2
