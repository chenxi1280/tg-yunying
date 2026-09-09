"""Immutable selections preserve quantity credit after mutable send-result updates."""
from datetime import time, datetime, timedelta

import pytest

from app.models import AiGroupEmergencySelection, AiGroupMessageMemory, ExecutionAttempt, TaskGroupDailyMessageSlot, TaskGroupDailyTarget
from app.services._common import _now
from app.services.task_center.ai_group_emergency import emergency_memory_matches, select_emergency_content, validate_emergency_selection
from app.services.task_center.ai_message_memory import mark_group_ai_message_result
from app.services.task_center.daily_group_target import _confirmed_message_count
from app.services.task_center.payloads import SendMessagePayload
from tests.test_ai_group_emergency import _pending, session


pytestmark = pytest.mark.no_postgres


def _selected(session):
    task, actions, _, _ = _pending(session)
    action = actions[0]
    assert select_emergency_content(session, task, action)
    memory = session.get(AiGroupMessageMemory, action.payload["ai_message_memory_id"])
    selection = session.get(AiGroupEmergencySelection, action.payload["emergency_selection_id"])
    now = _now()
    target = TaskGroupDailyTarget(tenant_id=action.tenant_id, task_id=action.task_id,
        task_day_ledger_id="emergency-ledger", group_id=7, target_date=now.date(),
        configured_message_target=2, frozen_account_count=2, effective_message_target=2,
        daily_fulfillment_phase="full_day_committed", scope_frozen_at=now, full_day_committed_at=now)
    session.add(target)
    session.flush()
    return action, memory, selection, target


def _sent(session, action, memory, *, historical=False):
    now = _now()
    action.status, action.executed_at = "success", now
    attempt = ExecutionAttempt(tenant_id=action.tenant_id, action_id=action.id,
        account_id=action.account_id, status="success", remote_message_id="QA-remote-1",
        gateway_call_started_at=now, after_call_at=now)
    session.add(attempt)
    outcome = {"success": True, "emergency_selection_id": action.payload["emergency_selection_id"]}
    mark_group_ai_message_result(session, memory.id, status="success", action_id=action.id,
                                 sent_at=now, result=outcome)
    if historical:
        # A persisted pre-fix row has only the send outcome, with the hash copy lost.
        memory.result = dict(outcome)
    session.flush()
    return attempt


def _count(session, target):
    start = datetime.combine(target.target_date, time.min)
    return _confirmed_message_count(session, target, start, start + timedelta(days=1))


@pytest.mark.parametrize("historical", [False, True])
def test_success_after_real_result_update_counts_with_immutable_selection(session, historical):
    action, memory, selection, target = _selected(session)
    _sent(session, action, memory, historical=historical)
    before = dict(memory.result)
    if historical:
        assert "content_hash" not in memory.result
    assert emergency_memory_matches(session, action, memory)
    assert _count(session, target) == 1
    assert memory.result == before
    assert selection.content_hash == action.candidate_hash


def test_gateway_uses_original_selection_when_old_mutable_result_has_no_hash_copy(session):
    action, memory, _, _ = _selected(session)
    mark_group_ai_message_result(session, memory.id, status="claiming", action_id=action.id,
                                 result={"success": False})
    memory.result = {"success": False}
    session.flush()
    validate_emergency_selection(session, action, SendMessagePayload.model_validate(action.payload))


@pytest.mark.parametrize("invalid", ["missing_selection", "selection_action", "selection_tenant", "selection_task",
    "selection_slot", "selection_hash", "candidate_hash", "memory_text", "action_text", "memory_account",
    "memory_group", "memory_task", "materialization", "source", "identity", "copy_hash", "copy_selection"])
def test_invalid_immutable_or_memory_binding_cannot_receive_quantity_credit(session, invalid):
    action, memory, selection, target = _selected(session)
    _sent(session, action, memory, historical=True)
    if invalid == "missing_selection":
        action.payload = {**action.payload, "emergency_selection_id": "missing"}
    elif invalid in {"action_text", "source"}:
        key = "message_text" if invalid == "action_text" else "content_source"
        action.payload = {**action.payload, key: "incorrect"}
    elif invalid in {"copy_hash", "copy_selection"}:
        key = "content_hash" if invalid == "copy_hash" else "emergency_selection_id"
        memory.result = {**memory.result, key: "incorrect"}
    else:
        owner, field, value = _invalid_field(action, memory, selection, invalid=invalid)
        setattr(owner, field, value)
    session.flush()
    assert not emergency_memory_matches(session, action, memory)
    assert _count(session, target) == 0


def _invalid_field(action, memory, selection, *, invalid):
    return {
        "selection_action": (selection, "action_id", "different"),
        "selection_tenant": (selection, "tenant_id", 2),
        "selection_task": (selection, "task_id", "different"),
        "selection_slot": (selection, "primary_quantity_slot_id", "different"),
        "selection_hash": (selection, "content_hash", "b" * 64),
        "candidate_hash": (action, "candidate_hash", "b" * 64),
        "memory_text": (memory, "raw_text", "different"),
        "memory_account": (memory, "account_id", 12),
        "memory_group": (memory, "group_id", 8),
        "memory_task": (memory, "task_id", "different"),
        "materialization": (action, "materialization_version", 99),
        "identity": (selection, "identity", {**selection.identity, "account_id": 12}),
    }[invalid]


@pytest.mark.parametrize("invalid", ["no_quantity_credit", "unknown_action", "unknown_attempt", "no_attempt", "other_account"])
def test_immutable_selection_does_not_relax_quantity_or_success_gates(session, invalid):
    action, memory, _, target = _selected(session)
    attempt = _sent(session, action, memory, historical=True)
    if invalid == "no_quantity_credit":
        session.get(TaskGroupDailyMessageSlot, action.primary_quantity_slot_id).quantity_credit_eligible = False
    elif invalid == "unknown_action":
        action.status = "unknown_after_send"
    elif invalid == "unknown_attempt":
        attempt.status = "result_unknown"
    elif invalid == "other_account":
        attempt.account_id = 12
    else:
        session.delete(attempt)
    session.flush()
    assert _count(session, target) == 0
