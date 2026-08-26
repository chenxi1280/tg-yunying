from __future__ import annotations

from sqlalchemy import event, select

import pytest

from app.models import Action, ExecutionAttempt, TaskGroupDailyMessageSlot
from app.services._common import _now
from app.services.task_center.ai_content_scope_takeover import (
    build_takeover_classification_context,
    classify_takeover_action,
    takeover_classification_reason_counts,
)
from ai_content_scope_takeover_test_support import (
    preview as _preview,
    seed_fact_first_action as _seed_fact_first_action,
    seed_scope as _seed_scope,
    sessions as _sessions,
)


pytestmark = pytest.mark.no_postgres


def test_fact_first_quantity_only_binding_is_already_current() -> None:
    sessions = _sessions()
    with sessions() as session:
        _seed_scope(session)
        action = _seed_fact_first_action(session, "fact-first-current")

        batch = _preview(session)

        assert action.content_mix_cycle_slot_id is None
        assert not action.payload.get("content_mix_cycle_slot_id")
        assert batch.classification_counts == {"already_current": 1}
        assert takeover_classification_reason_counts(session, batch.id) == {
            "already_current:scope_contract_current": 1,
        }


@pytest.mark.parametrize(
    ("action_slot", "payload_slot", "expected"),
    [
        (None, "", ("replan_required", "fact_first_quantity_binding_missing")),
        ("quantity-fact-first-invalid", "different-slot", ("quarantine", "fact_first_quantity_binding_conflict")),
    ],
)
def test_fact_first_quantity_binding_failures_remain_closed(
    action_slot: str | None,
    payload_slot: str | None,
    expected: tuple[str, str],
) -> None:
    sessions = _sessions()
    with sessions() as session:
        _seed_scope(session)
        action = _seed_fact_first_action(session, "fact-first-invalid")
        action.primary_quantity_slot_id = action_slot
        action.payload = {**action.payload, "primary_quantity_slot_id": payload_slot}
        session.flush()

        result = classify_takeover_action(session, action)

        assert (result.name, result.reason_code) == expected


def test_bulk_preview_classifies_invalid_legacy_payload_without_crashing() -> None:
    sessions = _sessions()
    with sessions() as session:
        _seed_scope(session)
        action = _seed_fact_first_action(session, "invalid-legacy-payload")
        action.payload = {
            **action.payload,
            "group_id": "not-an-integer",
            "context_message_ids": ["not-an-integer"],
            "context_snapshot_message_id": {},
        }
        session.flush()

        batch = _preview(session)

        assert batch.classification_counts == {"replan_required": 1}
        assert takeover_classification_reason_counts(session, batch.id) == {
            "replan_required:legacy_payload_invalid_pre_gateway": 1,
        }


def test_bulk_and_authoritative_classification_are_identical() -> None:
    sessions = _sessions()
    with sessions() as session:
        _seed_scope(session)
        actions = [
            _seed_fact_first_action(session, f"fact-first-{index}")
            for index in range(3)
        ]
        authoritative = [classify_takeover_action(session, action) for action in actions]

        context = build_takeover_classification_context(session, actions)
        bulk = [
            classify_takeover_action(session, action, context=context)
            for action in actions
        ]

        assert bulk == authoritative


def test_fact_first_quantity_fact_drift_changes_classification_hash() -> None:
    sessions = _sessions()
    with sessions() as session:
        _seed_scope(session)
        action = _seed_fact_first_action(session, "fact-first-drift")
        before = classify_takeover_action(session, action)
        quantity = session.get(
            TaskGroupDailyMessageSlot, action.primary_quantity_slot_id,
        )
        quantity.state = "consumed"
        session.flush()

        after = classify_takeover_action(session, action)

        assert after.name == before.name
        assert after.input_hash != before.input_hash


def test_bulk_scope_facts_match_own_history_reply_target() -> None:
    sessions = _sessions()
    with sessions() as session:
        _seed_scope(session)
        history = Action(
            id="reply-history",
            tenant_id=1,
            task_id="task-ai",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            scheduled_at=_now(),
            executed_at=_now(),
            status="success",
            payload={"group_id": 8, "message_text": "history body"},
        )
        session.add(history)
        session.add(ExecutionAttempt(
            id="reply-history-attempt",
            tenant_id=1,
            action_id=history.id,
            account_id=11,
            attempt_no=1,
            status="success",
            remote_message_id="9001",
        ))
        action = _seed_fact_first_action(
            session,
            "fact-first-reply",
            chat_mode="reply",
            reply_to_message_id=9001,
        )
        authoritative = classify_takeover_action(session, action)

        context = build_takeover_classification_context(session, [action])
        bulk = classify_takeover_action(session, action, context=context)

        assert bulk == authoritative
        assert bulk.name == "already_current"


def test_bulk_classification_select_count_is_constant() -> None:
    sessions = _sessions()
    with sessions() as session:
        _seed_scope(session)
        for index in range(12):
            _seed_fact_first_action(session, f"fact-first-query-{index:02d}")
        actions = list(session.scalars(select(Action).where(
            Action.id.like("fact-first-query-%"),
        ).order_by(Action.id.asc())))

        one_count = _classification_select_count(session, actions[:1])
        many_count = _classification_select_count(session, actions)

        assert many_count == one_count
        assert many_count <= 6


def _classification_select_count(session, actions: list[Action]) -> int:
    engine = session.get_bind()
    count = 0

    def count_selects(_conn, _cursor, statement, _parameters, _context, _many):
        nonlocal count
        if statement.lstrip().upper().startswith("SELECT"):
            count += 1

    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        context = build_takeover_classification_context(session, actions)
        for action in actions:
            classify_takeover_action(session, action, context=context)
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)
    return count
