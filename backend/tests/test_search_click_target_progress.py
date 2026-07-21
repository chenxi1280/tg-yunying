from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, OperationTarget, Task, Tenant
from app.schemas.task_center import SearchRankDeboostTaskConfigUpdate, TaskRetryRequest
from app.services.task_center import dispatcher
from app.services.task_center.search_click_target_progress import (
    reconcile_search_click_target_progress,
    search_click_target_progress,
)
from app.services.task_center.service import (
    reset_task,
    retry_task,
    start_task,
    update_search_rank_deboost_config,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id=1, name="默认运营空间"))
        db.commit()
        yield db


def _task(task_type: str, target_count: int | None) -> Task:
    type_config = {} if target_count is None else {"target_count": target_count}
    return Task(
        tenant_id=1,
        name="搜索点击",
        type=task_type,
        status="running",
        type_config=type_config,
        stats={},
    )


def _action(task: Task, action_type: str, status: str, result: dict | None = None) -> Action:
    if result is None:
        result = _confirmed_result(action_type)
    return Action(
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type=action_type,
        status=status,
        payload={},
        result=result,
    )


def _confirmed_result(action_type: str) -> dict:
    if action_type == "search_join":
        return {"join_status": "membership_observed"}
    return {
        "execution_status": "confirmed",
        "click_outcomes": [{
            "status": "confirmed",
            "competitor_username": "competitor",
            "competitor_position": 1,
            "row": 0,
            "col": 0,
            "dwell_seconds": 10,
            "effect": "navigate_only",
            "joined": False,
        }],
    }


@pytest.mark.no_postgres
def test_progress_counts_confirmed_and_unknown_as_held_slots(session: Session) -> None:
    task = _task("search_join_group", 3)
    session.add(task)
    session.flush()
    session.add_all([
        _action(task, "search_join", "success"),
        _action(task, "search_join", "unknown_after_send"),
    ])
    session.flush()

    progress = search_click_target_progress(session, task)

    assert progress.confirmed_count == 1
    assert progress.held_count == 1
    assert progress.remaining_slot_count == 1


@pytest.mark.no_postgres
@pytest.mark.parametrize(
    ("task_type", "action_type", "result"),
    [
        ("search_join_group", "search_join", {"join_status": "failed"}),
        ("search_rank_deboost", "search_rank_deboost", {"execution_status": "confirmed", "click_outcomes": []}),
    ],
)
def test_progress_does_not_count_success_without_a_confirmed_click_fact(
    session: Session,
    task_type: str,
    action_type: str,
    result: dict,
) -> None:
    task = _task(task_type, 1)
    session.add(task)
    session.flush()
    session.add(_action(task, action_type, "success", result))
    session.flush()

    progress = reconcile_search_click_target_progress(session, task)

    assert progress.confirmed_count == 0
    assert progress.remaining_slot_count == 1
    assert task.status == "running"


@pytest.mark.no_postgres
def test_progress_holds_only_unconfirmed_transient_states(session: Session) -> None:
    task = _task("search_rank_deboost", 7)
    session.add(task)
    session.flush()
    session.add_all([
        _action(task, "search_rank_deboost", "pending"),
        _action(task, "search_rank_deboost", "claiming"),
        _action(task, "search_rank_deboost", "executing"),
        _action(task, "search_rank_deboost", "unknown_after_send"),
        _action(task, "search_rank_deboost", "retryable_failed"),
        _action(task, "search_rank_deboost", "failed"),
        _action(task, "search_rank_deboost", "skipped"),
    ])
    session.flush()

    progress = search_click_target_progress(session, task)

    assert progress.confirmed_count == 0
    assert progress.held_count == 4
    assert progress.remaining_slot_count == 3


@pytest.mark.no_postgres
def test_reconcile_completes_task_after_last_confirmed_click(session: Session) -> None:
    task = _task("search_rank_deboost", 2)
    session.add(task)
    session.flush()
    session.add_all([
        _action(task, "search_rank_deboost", "success"),
        _action(task, "search_rank_deboost", "success"),
    ])
    session.flush()

    progress = reconcile_search_click_target_progress(session, task)

    assert progress.remaining_slot_count == 0
    assert task.status == "completed"
    assert task.next_run_at is None
    assert task.stats["completion_reason"] == "target_count_reached"
    assert task.stats["search_click_target"] == {
        "target_count": 2,
        "confirmed_count": 2,
        "held_count": 0,
        "remaining_slot_count": 0,
        "state": "completed",
    }


@pytest.mark.no_postgres
def test_retry_completed_search_click_task_does_not_requeue_historical_failure(session: Session) -> None:
    task = _task("search_join_group", 1)
    session.add(task)
    session.flush()
    completed_action = _action(task, "search_join", "success")
    failed_action = _action(task, "search_join", "failed")
    session.add_all([completed_action, failed_action])
    session.flush()
    reconcile_search_click_target_progress(session, task)
    session.commit()

    retried = retry_task(session, 1, task.id, TaskRetryRequest(failed_only=True), "tester")

    session.refresh(failed_action)
    assert retried.status == "completed"
    assert retried.next_run_at is None
    assert failed_action.status == "failed"


@pytest.mark.no_postgres
@pytest.mark.parametrize(
    ("task_type", "action_type"),
    [("search_join_group", "search_join"), ("search_rank_deboost", "search_rank_deboost")],
)
def test_start_completed_search_click_task_does_not_reopen_target_lifecycle(
    session: Session,
    task_type: str,
    action_type: str,
) -> None:
    task = _task(task_type, 1)
    session.add(task)
    session.flush()
    session.add(_action(task, action_type, "success"))
    session.commit()

    started = start_task(session, 1, task.id, "tester")

    assert started.status == "completed"
    assert started.next_run_at is None
    assert started.stats["completion_reason"] == "target_count_reached"


@pytest.mark.no_postgres
def test_retry_search_click_task_respects_remaining_target_slots(session: Session) -> None:
    task = _task("search_join_group", 1)
    session.add(task)
    session.flush()
    pending_action = _action(task, "search_join", "pending")
    failed_action = _action(task, "search_join", "failed")
    session.add_all([pending_action, failed_action])
    session.commit()

    retry_task(session, 1, task.id, TaskRetryRequest(failed_only=True), "tester")

    session.refresh(pending_action)
    session.refresh(failed_action)
    assert pending_action.status == "pending"
    assert failed_action.status == "failed"


@pytest.mark.no_postgres
def test_retry_search_click_task_reopens_only_available_target_slots(session: Session) -> None:
    task = _task("search_join_group", 1)
    session.add(task)
    session.flush()
    first_failed_action = _action(task, "search_join", "failed")
    second_failed_action = _action(task, "search_join", "failed")
    session.add_all([first_failed_action, second_failed_action])
    session.commit()

    retry_task(session, 1, task.id, TaskRetryRequest(failed_only=True), "tester")

    actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))
    assert sum(action.status == "pending" for action in actions) == 1
    assert sum(action.status == "failed" for action in actions) == 1


@pytest.mark.no_postgres
def test_legacy_task_without_target_count_is_not_capped(session: Session) -> None:
    task = _task("search_join_group", None)
    session.add(task)
    session.flush()

    progress = reconcile_search_click_target_progress(session, task)

    assert progress.target_count is None
    assert progress.remaining_slot_count is None
    assert task.status == "running"


@pytest.mark.no_postgres
def test_reset_rank_deboost_task_returns_to_draft_for_fresh_start_readiness(session: Session) -> None:
    task = _task("search_rank_deboost", 1)
    task.status = "failed"
    task.type_config["target_group_ids"] = [17]
    session.add(
        OperationTarget(
            id=17,
            tenant_id=1,
            target_type="group",
            tg_peer_id="-10017",
            title="目标群",
            username="target_group",
        )
    )
    session.add(task)
    session.commit()

    reset = reset_task(session, 1, task.id, "tester")

    assert reset.status == "draft"
    assert reset.next_run_at is None
    assert reset.stats["rank_deboost_readiness"]["status"] == "pending"


@pytest.mark.no_postgres
def test_rank_target_count_increase_clears_obsolete_completion_reason(session: Session) -> None:
    task = _task("search_rank_deboost", 1)
    session.add(task)
    session.flush()
    session.add(_action(task, "search_rank_deboost", "success"))
    session.flush()
    reconcile_search_click_target_progress(session, task)
    session.commit()

    updated = update_search_rank_deboost_config(
        session,
        1,
        task.id,
        SearchRankDeboostTaskConfigUpdate(target_count=2),
        "tester",
    )

    assert updated.status == "draft"
    assert updated.next_run_at is None
    assert "completion_reason" not in updated.stats
    assert updated.stats["search_click_target"]["target_count"] == 2


@pytest.mark.no_postgres
def test_rank_edit_repeating_target_and_keywords_keeps_readiness(session: Session) -> None:
    task = _task("search_rank_deboost", 1)
    task.status = "running"
    task.type_config = {
        "target_count": 1,
        "target_group_ids": [17],
        "target_operation_target_id": 17,
        "target_reference_type": "operation_target",
        "keywords": [{"text": "上海 留学"}],
    }
    task.stats = {"rank_deboost_readiness": {"status": "ready"}}
    session.add_all([
        task,
        OperationTarget(
            id=17,
            tenant_id=1,
            target_type="group",
            tg_peer_id="-10017",
            title="目标群",
            username="target_group",
        ),
    ])
    session.commit()

    updated = update_search_rank_deboost_config(
        session,
        1,
        task.id,
        SearchRankDeboostTaskConfigUpdate(
            target_title="目标群",
            target_link="https://t.me/target_group",
            keywords=["上海 留学"],
            target_count=2,
        ),
        "tester",
    )

    assert updated.stats["rank_deboost_readiness"]["status"] == "ready"


@pytest.mark.no_postgres
def test_dispatch_finalizer_completes_the_last_confirmed_search_click(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task("search_join_group", 1)
    session.add(task)
    session.flush()
    action = _action(task, "search_join", "executing")
    session.add(action)
    session.flush()

    def mark_success(*_args, **_kwargs) -> bool:
        action.status = "success"
        return True

    monkeypatch.setattr(dispatcher, "_dispatch_action", mark_success)

    assert dispatcher.dispatch_action(session, action) is True
    assert task.status == "completed"
    assert task.stats["completion_reason"] == "target_count_reached"
