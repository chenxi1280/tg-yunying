from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiAccountGroupStanceMemory,
    AiGroupMessageMemory,
    ContentMixCycleSlot,
    ExecutionAttempt,
    TaskGroupDailyMessageSlot,
)
from app.services._common import _now
from app.services.task_center.remote_reconciliation import (
    RemoteReconcileEvidence,
    apply_remote_reconcile_evidence,
    ensure_remote_reconcile_case,
)
from ai_content_scope_takeover_test_support import (
    seed_bound_legacy_action,
    seed_scope,
    sessions,
)


pytestmark = pytest.mark.no_postgres


def test_remote_absence_reopens_original_ai_slot_and_unknown_memory() -> None:
    session_factory = sessions()
    with session_factory() as session:
        seed_scope(session)
        action, attempt = _seed_remote_unknown_ai(session, "a-absence")
        case = ensure_remote_reconcile_case(session, action, attempt)
        evidence = RemoteReconcileEvidence(
            result="remote_absence_proven",
            source="gateway_request_evidence_journal",
            evidence_fingerprint="a" * 64,
            failure_code="rpc_rejected_before_mutation",
            remote_mutation_started=False,
        )

        apply_remote_reconcile_evidence(
            session, case.id, evidence, actor="release-owner",
        )

        cycle_slot, quantity = _content_rows(session, action)
        memory = session.get(AiGroupMessageMemory, "memory-a-absence")
        stance = session.scalar(select(AiAccountGroupStanceMemory))
        assert cycle_slot.current_action_id is None
        assert cycle_slot.slot_state == "replan_required"
        assert quantity.state == "open"
        assert memory.status == "failed"
        assert memory.result["fulfillment_replan_required"] is True
        assert stance.last_message_id == ""


def test_remote_confirmed_updates_ai_memory_and_real_stance_id() -> None:
    session_factory = sessions()
    with session_factory() as session:
        seed_scope(session)
        action, attempt = _seed_remote_unknown_ai(session, "a-confirmed")
        case = ensure_remote_reconcile_case(session, action, attempt)
        evidence = RemoteReconcileEvidence(
            result="remote_confirmed",
            source="telegram_history_read_only",
            evidence_fingerprint="b" * 64,
            remote_message_id="remote-confirmed-1",
            exact_match_count=1,
        )

        apply_remote_reconcile_evidence(
            session, case.id, evidence, actor="release-owner",
        )

        cycle_slot, quantity = _content_rows(session, action)
        memory = session.get(AiGroupMessageMemory, "memory-a-confirmed")
        stance = session.scalar(select(AiAccountGroupStanceMemory))
        assert cycle_slot.slot_state == "confirmed"
        assert quantity.state == "confirmed"
        assert memory.status == "success"
        assert memory.sent_at is not None
        assert stance.last_message_id == "remote-confirmed-1"


def _seed_remote_unknown_ai(
    session: Session,
    action_id: str,
) -> tuple[Action, ExecutionAttempt]:
    memory_id = f"memory-{action_id}"
    action = seed_bound_legacy_action(
        session,
        action_id,
        ai_message_memory_id=memory_id,
        slot_id=f"slot-{action_id}",
    )
    action.status = "unknown_after_send"
    action.result = {"error_code": "unknown_after_send"}
    attempt = _unknown_attempt(action_id)
    session.add_all([
        attempt,
        _unknown_memory(action_id, memory_id),
        _unknown_stance(action_id),
    ])
    session.flush()
    return action, attempt


def _unknown_attempt(action_id: str) -> ExecutionAttempt:
    return ExecutionAttempt(
        id=f"attempt-{action_id}",
        tenant_id=1,
        action_id=action_id,
        account_id=11,
        attempt_no=1,
        status="result_unknown",
        before_call_at=_now() - timedelta(seconds=2),
        gateway_call_started_at=_now() - timedelta(seconds=1),
        after_call_at=_now(),
        failure_type="unknown_after_send",
    )


def _unknown_memory(action_id: str, memory_id: str) -> AiGroupMessageMemory:
    return AiGroupMessageMemory(
        id=memory_id,
        tenant_id=1,
        group_id=8,
        task_id="task-ai",
        action_id=action_id,
        account_id=11,
        raw_text="legacy body",
        normalized_text="legacy body",
        text_fingerprint="f" * 64,
        status="unknown_after_send",
    )


def _unknown_stance(action_id: str) -> AiAccountGroupStanceMemory:
    return AiAccountGroupStanceMemory(
        id=f"stance-{action_id}",
        tenant_id=1,
        group_id=8,
        account_id=11,
        stance="sent",
        last_message_id=action_id,
        summary="unknown placeholder",
    )


def _content_rows(
    session: Session,
    action: Action,
) -> tuple[ContentMixCycleSlot, TaskGroupDailyMessageSlot]:
    cycle_slot = session.get(ContentMixCycleSlot, action.content_mix_cycle_slot_id)
    quantity = session.get(TaskGroupDailyMessageSlot, action.primary_quantity_slot_id)
    assert cycle_slot is not None
    assert quantity is not None
    return cycle_slot, quantity
