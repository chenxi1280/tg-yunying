from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Action, ExecutionAttempt
from app.services.task_center.runtime_state_hash import (
    action_state_hash,
    canonical_state_hash,
    execution_attempt_state_hash,
)


pytestmark = pytest.mark.no_postgres


def test_canonical_state_hash_is_map_order_independent() -> None:
    assert canonical_state_hash({"b": 2, "a": {"y": 1, "x": 0}}) == canonical_state_hash(
        {"a": {"x": 0, "y": 1}, "b": 2}
    )


def test_action_hash_detects_status_change_without_retry_count_change() -> None:
    action = _action()
    before = action_state_hash(action)

    action.status = "claiming"

    assert action.retry_count == 0
    assert action_state_hash(action) != before


def test_action_hash_normalizes_equivalent_utc_times_and_hashes_body() -> None:
    first = _action()
    second = _action()
    second.scheduled_at = first.scheduled_at.astimezone(
        timezone(timedelta(hours=8)),
    )
    second.payload = {"content_scope_task_id": "task-1", "message_text": "hello"}

    assert action_state_hash(first) == action_state_hash(second)
    assert "hello" not in action_state_hash(first)


def test_action_hash_detects_media_segment_change() -> None:
    action = _action()
    action.payload = {
        **action.payload,
        "media_segments": [{"type": "image", "asset": "one"}],
    }
    before = action_state_hash(action)
    action.payload = {
        **action.payload,
        "media_segments": [{"type": "image", "asset": "two"}],
    }
    assert action_state_hash(action) != before


def test_action_hash_detects_channel_remote_fact_payload_drift() -> None:
    action = _action()
    action.payload = {
        "channel_id": "-1008",
        "message_id": 700,
        "reaction_emoji": "🔥",
    }
    before = action_state_hash(action)
    action.payload = {**action.payload, "reaction_emoji": "👍"}
    assert action_state_hash(action) != before


def test_attempt_hash_detects_remote_and_result_drift() -> None:
    attempt = _attempt()
    before = execution_attempt_state_hash(attempt)

    attempt.remote_message_id = "9988"

    assert execution_attempt_state_hash(attempt) != before


def _action() -> Action:
    observed_at = datetime(2026, 8, 1, 4, tzinfo=timezone.utc)
    return Action(
        id="action-1",
        tenant_id=1,
        task_id="task-1",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=7,
        scheduled_at=observed_at,
        executed_at=None,
        status="pending",
        lease_owner="",
        lease_expires_at=None,
        claim_owner="",
        claim_token="",
        claim_expires_at=None,
        primary_quantity_slot_id="quantity-1",
        content_mix_cycle_slot_id="cycle-slot-1",
        content_mix_slot_attempt=1,
        payload={"message_text": "hello", "content_scope_task_id": "task-1"},
        result={},
        retry_count=0,
    )


def _attempt() -> ExecutionAttempt:
    observed_at = datetime(2026, 8, 1, 4, tzinfo=timezone.utc)
    return ExecutionAttempt(
        id="attempt-1",
        tenant_id=1,
        action_id="action-1",
        worker_id="worker-1",
        account_id=7,
        attempt_no=1,
        status="gateway_call_started",
        before_call_at=observed_at,
        gateway_call_started_at=observed_at,
        after_call_at=None,
        remote_message_id="",
        failure_type="",
        failure_detail="",
        result_snapshot={"gateway_request_identity": "request-1"},
    )
