from __future__ import annotations

import pytest

from app.services.task_center.search_click_assignment_solver import (
    SearchClickCandidatePath,
    SearchClickDemand,
    solve_search_click_assignments,
)


@pytest.mark.no_postgres
def test_solver_finds_maximum_matching_instead_of_greedy_prefix() -> None:
    demands = (
        SearchClickDemand("o1", "task-a"),
        SearchClickDemand("o2", "task-a"),
    )
    paths = (
        SearchClickCandidatePath(
            key="flex",
            account_id=1,
            authorization_id=11,
            keyword_hash="a" * 64,
            proxy_route_id="proxy-1",
            protocol_sample_version="v1",
            hard_safe_remaining_capacity=1,
            confirmed_click_count_today=0,
            last_click_opportunity_at=None,
            persistent_account_cursor=1,
            eligible_obligation_ids=("o1", "o2"),
        ),
        SearchClickCandidatePath(
            key="only-o1",
            account_id=2,
            authorization_id=22,
            keyword_hash="b" * 64,
            proxy_route_id="proxy-2",
            protocol_sample_version="v1",
            hard_safe_remaining_capacity=1,
            confirmed_click_count_today=0,
            last_click_opportunity_at=None,
            persistent_account_cursor=2,
            eligible_obligation_ids=("o1",),
        ),
    )

    result = solve_search_click_assignments(demands, paths)

    assert result.outcome == "optimal"
    assert {(item.obligation_id, item.candidate_key) for item in result.matches} == {
        ("o1", "only-o1"),
        ("o2", "flex"),
    }


@pytest.mark.no_postgres
def test_solver_maximizes_served_tasks_after_click_count() -> None:
    demands = (
        SearchClickDemand("a-1", "task-a"),
        SearchClickDemand("a-2", "task-a"),
        SearchClickDemand("b-1", "task-b"),
    )
    paths = (
        SearchClickCandidatePath(
            key="shared",
            account_id=1,
            authorization_id=11,
            keyword_hash="a" * 64,
            proxy_route_id="proxy-1",
            protocol_sample_version="v1",
            hard_safe_remaining_capacity=1,
            confirmed_click_count_today=0,
            last_click_opportunity_at=None,
            persistent_account_cursor=1,
            eligible_obligation_ids=("a-1", "b-1"),
        ),
        SearchClickCandidatePath(
            key="a-only",
            account_id=2,
            authorization_id=22,
            keyword_hash="b" * 64,
            proxy_route_id="proxy-2",
            protocol_sample_version="v1",
            hard_safe_remaining_capacity=1,
            confirmed_click_count_today=0,
            last_click_opportunity_at=None,
            persistent_account_cursor=2,
            eligible_obligation_ids=("a-2",),
        ),
    )

    result = solve_search_click_assignments(demands, paths)
    task_by_obligation = {item.obligation_id: item.task_id for item in demands}
    served_tasks = {
        task_by_obligation[item.obligation_id] for item in result.matches
    }

    assert len(result.matches) == 2
    assert served_tasks == {"task-a", "task-b"}


@pytest.mark.no_postgres
def test_solver_max_min_fairness_balances_remaining_task_debt() -> None:
    demands = tuple(
        SearchClickDemand(f"{task}-{ordinal}", task)
        for task in ("task-a", "task-b")
        for ordinal in range(1, 4)
    )
    paths = tuple(
        SearchClickCandidatePath(
            key=f"path-{index}",
            account_id=index,
            authorization_id=index,
            keyword_hash=f"{index:064x}",
            proxy_route_id=f"proxy-{index}",
            protocol_sample_version="v1",
            hard_safe_remaining_capacity=1,
            confirmed_click_count_today=0,
            last_click_opportunity_at=None,
            persistent_account_cursor=index,
        )
        for index in range(1, 5)
    )

    result = solve_search_click_assignments(demands, paths)
    task_by_obligation = {item.obligation_id: item.task_id for item in demands}
    assigned = [
        task_by_obligation[item.obligation_id] for item in result.matches
    ]

    assert assigned.count("task-a") == 2
    assert assigned.count("task-b") == 2


@pytest.mark.no_postgres
def test_solver_max_min_fairness_uses_frozen_remaining_debt() -> None:
    demands = (
        SearchClickDemand("a-1", "task-a", task_remaining_count=10),
        SearchClickDemand("a-2", "task-a", task_remaining_count=10),
        SearchClickDemand("a-3", "task-a", task_remaining_count=10),
        SearchClickDemand("b-1", "task-b", task_remaining_count=2),
        SearchClickDemand("b-2", "task-b", task_remaining_count=2),
    )
    paths = tuple(
        SearchClickCandidatePath(
            key=f"path-{index}",
            account_id=index,
            authorization_id=index,
            keyword_hash=f"{index:064x}",
            proxy_route_id=f"proxy-{index}",
            protocol_sample_version="v1",
            hard_safe_remaining_capacity=1,
            confirmed_click_count_today=0,
            last_click_opportunity_at=None,
            persistent_account_cursor=index,
        )
        for index in range(1, 5)
    )

    result = solve_search_click_assignments(demands, paths)
    task_by_obligation = {item.obligation_id: item.task_id for item in demands}
    assigned = [
        task_by_obligation[item.obligation_id] for item in result.matches
    ]

    assert assigned.count("task-a") == 3
    assert assigned.count("task-b") == 1


@pytest.mark.no_postgres
def test_solver_uses_system_owned_stable_tie_break_order() -> None:
    demands = (SearchClickDemand("o1", "task-a"),)
    paths = (
        SearchClickCandidatePath(
            key="lower-capacity",
            account_id=1,
            authorization_id=11,
            keyword_hash="a" * 64,
            proxy_route_id="proxy-1",
            protocol_sample_version="v1",
            hard_safe_remaining_capacity=1,
            confirmed_click_count_today=0,
            last_click_opportunity_at=None,
            persistent_account_cursor=1,
        ),
        SearchClickCandidatePath(
            key="higher-capacity",
            account_id=2,
            authorization_id=22,
            keyword_hash="b" * 64,
            proxy_route_id="proxy-2",
            protocol_sample_version="v1",
            hard_safe_remaining_capacity=3,
            confirmed_click_count_today=3,
            last_click_opportunity_at=None,
            persistent_account_cursor=2,
        ),
    )

    result = solve_search_click_assignments(demands, paths)

    assert result.matches[0].candidate_key == "higher-capacity"


@pytest.mark.no_postgres
def test_solver_returns_no_candidate_without_partial_output() -> None:
    result = solve_search_click_assignments(
        (SearchClickDemand("o1", "task-a"),),
        (),
    )

    assert result.outcome == "no_candidate"
    assert result.matches == ()
    assert result.unmatched_obligation_ids == ("o1",)


@pytest.mark.no_postgres
def test_solver_does_not_duplicate_shared_account_capacity_across_tasks() -> None:
    demands = (
        SearchClickDemand("a-1", "task-a"),
        SearchClickDemand("b-1", "task-b"),
    )
    paths = (
        SearchClickCandidatePath(
            key="task-a-account-1",
            account_id=1,
            authorization_id=11,
            keyword_hash="a" * 64,
            proxy_route_id="proxy-1",
            protocol_sample_version="v1",
            hard_safe_remaining_capacity=1,
            confirmed_click_count_today=0,
            last_click_opportunity_at=None,
            persistent_account_cursor=1,
            eligible_obligation_ids=("a-1",),
        ),
        SearchClickCandidatePath(
            key="task-b-account-1",
            account_id=1,
            authorization_id=11,
            keyword_hash="b" * 64,
            proxy_route_id="proxy-1",
            protocol_sample_version="v1",
            hard_safe_remaining_capacity=1,
            confirmed_click_count_today=0,
            last_click_opportunity_at=None,
            persistent_account_cursor=1,
            eligible_obligation_ids=("b-1",),
        ),
    )

    result = solve_search_click_assignments(demands, paths)

    assert len(result.matches) == 1


@pytest.mark.no_postgres
def test_solver_uses_each_account_session_once_per_claim_window() -> None:
    demands = (
        SearchClickDemand("o-1", "task-a"),
        SearchClickDemand("o-2", "task-a"),
        SearchClickDemand("o-3", "task-a"),
    )
    paths = (
        SearchClickCandidatePath(
            key="high-capacity-account",
            account_id=1,
            authorization_id=11,
            keyword_hash="a" * 64,
            proxy_route_id="proxy-1",
            protocol_sample_version="v1",
            hard_safe_remaining_capacity=100,
            confirmed_click_count_today=0,
            last_click_opportunity_at=None,
            persistent_account_cursor=1,
        ),
        SearchClickCandidatePath(
            key="second-account",
            account_id=2,
            authorization_id=22,
            keyword_hash="b" * 64,
            proxy_route_id="proxy-2",
            protocol_sample_version="v1",
            hard_safe_remaining_capacity=10,
            confirmed_click_count_today=0,
            last_click_opportunity_at=None,
            persistent_account_cursor=2,
        ),
    )

    result = solve_search_click_assignments(demands, paths)

    assert len(result.matches) == 2
    assert {item.candidate_key for item in result.matches} == {
        "high-capacity-account",
        "second-account",
    }
