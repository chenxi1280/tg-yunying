from types import SimpleNamespace

import pytest

from app.services.task_center.ai_message_memory import mark_group_ai_message_result


pytestmark = pytest.mark.no_postgres


def _memory(result):
    memory = SimpleNamespace(result=result, status="reserved", action_id="action", sent_at=None)
    return memory, SimpleNamespace(get=lambda *_args: memory)


def test_success_result_preserves_emergency_evidence_without_old_errors():
    previous = {"emergency_selection_id": "selection", "content_hash": "frozen", "error_code": "old"}
    incoming = {"success": True, "emergency_selection_id": "selection"}
    memory, session = _memory(previous)
    mark_group_ai_message_result(session, "memory", status="success", result=incoming)
    assert memory.result == {"success": True, "emergency_selection_id": "selection", "content_hash": "frozen"}
    assert incoming == {"success": True, "emergency_selection_id": "selection"}
    assert previous["error_code"] == "old"


@pytest.mark.parametrize("field", ["content_hash", "emergency_selection_id"])
def test_conflicting_frozen_evidence_rejects_before_state_change(field):
    previous = {"emergency_selection_id": "selection", "content_hash": "frozen"}
    memory, session = _memory(previous)
    with pytest.raises(ValueError, match="emergency_message_memory_evidence_changed"):
        mark_group_ai_message_result(session, "memory", status="success", result={field: "other"})
    assert memory.status == "reserved" and memory.result == previous


def test_historical_missing_hash_is_not_invented_during_result_update():
    memory, session = _memory({"emergency_selection_id": "selection"})
    mark_group_ai_message_result(session, "memory", status="success", result={"success": True})
    assert memory.result == {"success": True, "emergency_selection_id": "selection"}


def test_ordinary_result_keeps_existing_replacement_semantics():
    memory, session = _memory({"error_code": "old"})
    mark_group_ai_message_result(session, "memory", status="success", result={"success": True})
    assert memory.result == {"success": True}
