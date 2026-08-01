from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from app.models import (
    Action,
    ContentMixCycle,
    ContentMixCycleSlot,
    TaskAccountDailyCoverage,
    TaskGroupDailyMessageSlot,
)
from app.services.task_center import dispatcher
from app.services.task_center.executors import group_ai_chat


pytestmark = pytest.mark.no_postgres


class _LookupSession:
    def __init__(self, rows: dict[tuple[type, str], object]) -> None:
        self.rows = rows

    def get(self, model, row_id):
        return self.rows.get((model, row_id))

    def scalar(self, _statement):
        return None


@pytest.mark.parametrize(
    "invalid_part",
    [
        "quantity_missing",
        "cycle_slot_quantity",
        "account_coverage",
        "task_ownership",
        "ledger_ownership",
    ],
)
def test_content_mix_binding_guard_rejects_invalid_identity(
    invalid_part: str,
) -> None:
    action, session = _valid_binding()
    _invalidate_binding(action, session, invalid_part)

    accepted = dispatcher._ensure_ai_content_mix_binding(session, action)
    dispatcher._sync_action_content_mix_state(session, action)

    assert accepted is False
    assert action.status == "skipped"
    assert action.result["error_code"] == "content_mix_binding_invalid"


def test_content_mix_binding_guard_accepts_complete_identity() -> None:
    action, session = _valid_binding()

    accepted = dispatcher._ensure_ai_content_mix_binding(session, action)

    assert accepted is True
    assert action.status == "executing"


def test_content_mix_sync_releases_authoritative_quantity_only() -> None:
    action, session = _valid_binding()
    authoritative = session.rows[(TaskGroupDailyMessageSlot, "quantity-1")]
    wrong_quantity = SimpleNamespace(
        id="quantity-other",
        tenant_id=1,
        task_id="task-ai",
        task_day_ledger_id="ledger-1",
        task_account_daily_coverage_id="coverage-1",
        state="pending",
    )
    session.rows[(TaskGroupDailyMessageSlot, wrong_quantity.id)] = wrong_quantity
    action.primary_quantity_slot_id = wrong_quantity.id
    action.payload["primary_quantity_slot_id"] = wrong_quantity.id

    accepted = dispatcher._ensure_ai_content_mix_binding(session, action)
    dispatcher._sync_action_content_mix_state(session, action)

    cycle_slot = session.rows[(ContentMixCycleSlot, "cycle-slot-1")]
    assert accepted is False
    assert cycle_slot.slot_state == "replan_required"
    assert authoritative.state == "open"
    assert wrong_quantity.state == "pending"


def test_deadline_budget_exhaustion_terminates_slot_without_replan(
    monkeypatch,
) -> None:
    action, session = _valid_binding()
    action.status = "failed"
    action.result = {"error_code": "ai_generation_deadline_budget_exhausted"}
    effects: list[str] = []
    monkeypatch.setattr(
        dispatcher,
        "_shortfall_action_content_obligations",
        lambda *_args: effects.append("shortfall"),
    )
    monkeypatch.setattr(
        dispatcher,
        "_reconcile_content_mix_for_slot",
        lambda *_args: effects.append("reconcile"),
    )

    dispatcher._sync_action_content_mix_state(session, action)

    cycle_slot = session.rows[(ContentMixCycleSlot, "cycle-slot-1")]
    quantity = session.rows[(TaskGroupDailyMessageSlot, "quantity-1")]
    assert cycle_slot.slot_state == "terminal"
    assert quantity.state == "terminal"
    assert effects == ["shortfall", "reconcile"]


def test_dispatch_finally_tolerates_missing_authoritative_quantity(
    monkeypatch,
) -> None:
    action, session = _valid_binding()
    session.rows.pop((TaskGroupDailyMessageSlot, "quantity-1"))
    monkeypatch.setattr(
        dispatcher,
        "_dispatch_action",
        lambda current_session, current_action, **_kwargs: (
            dispatcher._ensure_ai_content_mix_binding(
                current_session,
                current_action,
            )
        ),
    )
    for function_name in _non_content_mix_finalizers():
        monkeypatch.setattr(
            dispatcher,
            function_name,
            lambda *_args, **_kwargs: None,
        )

    dispatched = dispatcher.dispatch_action(session, action)

    assert dispatched is False
    assert action.status == "skipped"
    assert action.result["error_code"] == "content_mix_binding_invalid"


def test_dispatch_action_propagates_database_errors(monkeypatch) -> None:
    action, session = _valid_binding()
    finalizers: list[str] = []
    monkeypatch.setattr(dispatcher, "_dispatch_account", lambda *_args: object())
    monkeypatch.setattr(
        dispatcher,
        "validate_action_payload",
        lambda *_args: (_ for _ in ()).throw(SQLAlchemyError("deadlock detected")),
    )
    monkeypatch.setattr(
        dispatcher,
        "_release_runtime_resources",
        lambda *_args: finalizers.append("runtime"),
    )
    for function_name in _database_finalizers():
        monkeypatch.setattr(
            dispatcher,
            function_name,
            lambda *_args, current=function_name: finalizers.append(current),
        )

    with pytest.raises(SQLAlchemyError, match="deadlock detected"):
        dispatcher.dispatch_action(
            session,
            action,
            generation_dependencies=SimpleNamespace(),
            comment_generation_dependencies=SimpleNamespace(),
        )
    assert finalizers == ["runtime"]


def test_dispatch_action_releases_runtime_for_base_exception(monkeypatch) -> None:
    action, session = _valid_binding()
    released: list[str] = []
    monkeypatch.setattr(
        dispatcher,
        "_dispatch_action",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit()),
    )
    monkeypatch.setattr(
        dispatcher,
        "_release_runtime_resources",
        lambda *_args: released.append("runtime"),
    )

    with pytest.raises(SystemExit):
        dispatcher.dispatch_action(session, action)
    assert released == ["runtime"]


def test_content_mix_replan_locks_cycle_slots_for_concurrent_planners() -> None:
    session = SimpleNamespace(
        statement=None,
        execute=lambda statement: _capture_statement(session, statement),
    )

    rows = group_ai_chat._content_mix_replan_rows(
        session,
        SimpleNamespace(id="task-ai"),
        "ledger-1",
    )
    sql = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
        ),
    )

    assert rows == []
    assert "FOR UPDATE OF content_mix_cycle_slots SKIP LOCKED" in sql


def _capture_statement(session, statement):
    session.statement = statement
    return []


def _non_content_mix_finalizers() -> tuple[str, ...]:
    return (
        "_release_runtime_resources",
        "release_dispatch_claim",
        "_sync_action_coverage_state",
        "_sync_comment_fulfillment_state",
        "_sync_channel_fulfillment_state",
        "_sync_all_account_membership_state",
        "_sync_search_click_target_progress",
    )


def _database_finalizers() -> tuple[str, ...]:
    return (
        "release_dispatch_claim",
        "_sync_action_coverage_state",
        "_sync_action_content_mix_state",
        "_sync_comment_fulfillment_state",
        "_sync_channel_fulfillment_state",
        "_sync_all_account_membership_state",
        "_sync_search_click_target_progress",
    )


def _valid_binding() -> tuple[Action, _LookupSession]:
    action = Action(
        id="content-mix-action",
        tenant_id=1,
        task_id="task-ai",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=201,
        status="executing",
        primary_quantity_slot_id="quantity-1",
        content_mix_cycle_slot_id="cycle-slot-1",
        payload={
            "content_scope_contract_version": "group_content_scope_v1",
            "coverage_ledger_id": "coverage-1",
            "primary_quantity_slot_id": "quantity-1",
            "content_mix_cycle_slot_id": "cycle-slot-1",
        },
    )
    rows = {
        (TaskGroupDailyMessageSlot, "quantity-1"): SimpleNamespace(
            id="quantity-1",
            tenant_id=1,
            task_id="task-ai",
            task_day_ledger_id="ledger-1",
            task_account_daily_coverage_id="coverage-1",
            state="pending",
        ),
        (ContentMixCycleSlot, "cycle-slot-1"): SimpleNamespace(
            id="cycle-slot-1",
            cycle_id="cycle-1",
            primary_quantity_slot_id="quantity-1",
            current_action_id=action.id,
            slot_state="pending",
            terminal_reason=None,
        ),
        (ContentMixCycle, "cycle-1"): SimpleNamespace(
            id="cycle-1",
            tenant_id=1,
            task_id="task-ai",
            task_day_ledger_id="ledger-1",
        ),
        (TaskAccountDailyCoverage, "coverage-1"): SimpleNamespace(
            id="coverage-1",
            tenant_id=1,
            task_id="task-ai",
            task_day_ledger_id="ledger-1",
            account_id=201,
        ),
    }
    return action, _LookupSession(rows)


def _invalidate_binding(
    action: Action,
    session: _LookupSession,
    invalid_part: str,
) -> None:
    quantity = session.rows[(TaskGroupDailyMessageSlot, "quantity-1")]
    cycle_slot = session.rows[(ContentMixCycleSlot, "cycle-slot-1")]
    coverage = session.rows[(TaskAccountDailyCoverage, "coverage-1")]
    if invalid_part == "quantity_missing":
        session.rows.pop((TaskGroupDailyMessageSlot, "quantity-1"))
    elif invalid_part == "cycle_slot_quantity":
        cycle_slot.primary_quantity_slot_id = "quantity-other"
    elif invalid_part == "account_coverage":
        coverage.account_id = 202
    elif invalid_part == "task_ownership":
        quantity.task_id = "task-other"
    elif invalid_part == "ledger_ownership":
        coverage.task_day_ledger_id = "ledger-other"
