from __future__ import annotations

from sqlalchemy import select

import pytest

from app.models import (
    Action,
    AiContentScopeTakeoverBatch,
    AiContentScopeTakeoverItem,
    ContentMixCycleSlot,
    RemoteReconcileCase,
    Task,
    TaskGroupDailyMessageSlot,
)
from app.services.task_center.service import reset_task
from app.services.task_center import dispatcher
from app.services.task_center.ai_content_scope_takeover_apply import (
    apply_takeover_chunk,
    begin_takeover_apply,
    takeover_chain_is_complete,
)
from ai_content_scope_takeover_test_support import (
    preview as _preview,
    seed_bound_legacy_action as _seed_bound_legacy_action,
    seed_scope as _seed_scope,
    seed_terminal_action as _seed_terminal_action,
    seed_unknown_action as _seed_unknown_action,
    sessions as _sessions,
)


pytestmark = pytest.mark.no_postgres


def test_preview_excludes_actions_from_route_fenced_tasks() -> None:
    sessions = _sessions()
    with sessions() as session:
        _seed_scope(session)
        _seed_bound_legacy_action(session, "fenced-action")
        task = session.get(Task, "task-ai")
        task.status = "stopped"

        batch = _preview(session)

        assert batch.classification_counts == {}


def test_reset_preserves_action_referenced_by_takeover_audit() -> None:
    sessions = _sessions()
    with sessions() as session:
        _seed_scope(session)
        action = _seed_bound_legacy_action(session, "audit-referenced-action")
        batch = _preview(session)
        session.flush()
        item = session.scalar(select(AiContentScopeTakeoverItem).where(
            AiContentScopeTakeoverItem.batch_id == batch.id,
            AiContentScopeTakeoverItem.action_id == action.id,
        ))
        assert item is not None
        item_id = item.id
        session.commit()

        reset_task(session, 1, "task-ai", "tester", reason="配置重排")
        preserved = session.get(Action, action.id)

        assert preserved is not None
        assert preserved.status == "skipped"
        assert preserved.result["error_code"] == "plan_superseded"
        assert session.get(AiContentScopeTakeoverItem, item_id) is not None


def test_takeover_resumes_and_never_mutates_terminal_or_unknown_actions() -> None:
    sessions = _sessions()
    with sessions() as session:
        _seed_scope(session)
        _seed_bound_legacy_action(session, "a-legacy")
        _seed_terminal_action(session, "b-terminal")
        _seed_unknown_action(session, "c-unknown")
        batch = _preview(session)
        batch_id = batch.id
        expected_hash = batch.classification_hash
        expected_counts = dict(batch.classification_counts)
        assert expected_counts == {
            "equivalent_snapshot_safe": 1,
            "remote_reconcile_required": 1,
        }
        session.commit()

    with sessions() as session:
        summary = begin_takeover_apply(
            session,
            batch_id,
            classification_hash=expected_hash,
            expected_counts=expected_counts,
            actor="release-owner",
        )
        assert summary["status"] == "applying"
        apply_takeover_chunk(
            session,
            batch_id,
            classification_hash=expected_hash,
            actor="release-owner",
            batch_size=1,
        )
        session.commit()

    _finish_takeover(
        sessions,
        batch_id=batch_id,
        expected_hash=expected_hash,
        expected_counts=expected_counts,
    )
    with sessions() as session:
        legacy = session.get(Action, "a-legacy")
        terminal = session.get(Action, "b-terminal")
        unknown = session.get(Action, "c-unknown")
        assert legacy.payload["content_scope_contract_version"] == "group_content_scope_v1"
        assert legacy.payload["message_text"] == "legacy body"
        assert terminal.status == "success"
        assert terminal.payload.get("content_scope_contract_version", "") == ""
        assert unknown.status == "unknown_after_send"
        assert session.scalar(select(RemoteReconcileCase.id)) is not None
        assert takeover_chain_is_complete(session, batch_id) is True


def _finish_takeover(
    sessions,
    *,
    batch_id: str,
    expected_hash: str,
    expected_counts: dict,
) -> None:
    with sessions() as session:
        begin_takeover_apply(
            session,
            batch_id,
            classification_hash=expected_hash,
            expected_counts=expected_counts,
            actor="release-owner",
        )
        while True:
            summary = apply_takeover_chunk(
                session,
                batch_id,
                classification_hash=expected_hash,
                actor="release-owner",
                batch_size=1,
            )
            session.commit()
            if summary["status"] == "completed":
                return


def test_preview_drift_blocks_zero_business_writes_then_supersedes() -> None:
    sessions = _sessions()
    with sessions() as session:
        _seed_scope(session)
        action = _seed_bound_legacy_action(session, "a-drift")
        batch = _preview(session)
        batch_id = batch.id
        batch_hash = batch.classification_hash
        counts = dict(batch.classification_counts)
        session.commit()
        action = session.get(Action, action.id)
        action.payload = {**action.payload, "message_text": "drifted body"}
        session.commit()

    with sessions() as session:
        summary = begin_takeover_apply(
            session,
            batch_id,
            classification_hash=batch_hash,
            expected_counts=counts,
            actor="release-owner",
        )
        session.commit()
        action = session.get(Action, "a-drift")
        assert summary["status"] == "blocked"
        assert summary["conflict_count"] == 1
        assert action.payload.get("content_scope_contract_version", "") == ""

        action.payload = {**action.payload, "message_text": "legacy body"}
        replacement = _preview(session, supersedes_batch_id=batch_id)
        replacement_id = replacement.id
        replacement_hash = replacement.classification_hash
        replacement_counts = dict(replacement.classification_counts)
        session.commit()

    with sessions() as session:
        begin_takeover_apply(
            session,
            replacement_id,
            classification_hash=replacement_hash,
            expected_counts=replacement_counts,
            actor="release-owner",
        )
        summary = apply_takeover_chunk(
            session,
            replacement_id,
            classification_hash=replacement_hash,
            actor="release-owner",
        )
        session.commit()
        assert summary["status"] == "completed"
        assert takeover_chain_is_complete(session, replacement_id) is True


def test_missing_context_replans_the_same_quantity_and_content_slot() -> None:
    sessions = _sessions()
    with sessions() as session:
        _seed_scope(session)
        action = _seed_bound_legacy_action(
            session,
            "a-replan",
            context_message_ids=[999],
        )
        batch = _preview(session)
        assert batch.classification_counts == {"replan_required": 1}
        batch_id = batch.id
        batch_hash = batch.classification_hash
        counts = dict(batch.classification_counts)
        session.commit()

    with sessions() as session:
        begin_takeover_apply(
            session,
            batch_id,
            classification_hash=batch_hash,
            expected_counts=counts,
            actor="release-owner",
        )
        apply_takeover_chunk(
            session,
            batch_id,
            classification_hash=batch_hash,
            actor="release-owner",
        )
        session.commit()
        action = session.get(Action, action.id)
        cycle_slot = session.get(ContentMixCycleSlot, action.content_mix_cycle_slot_id)
        quantity = session.get(TaskGroupDailyMessageSlot, action.primary_quantity_slot_id)
        assert action.status == "skipped"
        assert action.result["error_code"] == "content_contract_replan_required"
        assert cycle_slot.current_action_id is None
        assert cycle_slot.slot_state == "replan_required"
        assert quantity.state == "open"


def test_invalid_pre_gateway_payload_replans_instead_of_quarantine() -> None:
    sessions = _sessions()
    with sessions() as session:
        _seed_scope(session)
        action = _seed_bound_legacy_action(session, "a-invalid-payload")
        action.payload = {**action.payload, "message_text": ""}
        batch = _preview(session)
        assert batch.classification_counts == {"replan_required": 1}
        batch_id = batch.id
        batch_hash = batch.classification_hash
        counts = dict(batch.classification_counts)
        session.commit()

    _finish_takeover(
        sessions,
        batch_id=batch_id,
        expected_hash=batch_hash,
        expected_counts=counts,
    )
    with sessions() as session:
        action = session.get(Action, action.id)
        cycle_slot = session.get(ContentMixCycleSlot, action.content_mix_cycle_slot_id)
        quantity = session.get(TaskGroupDailyMessageSlot, action.primary_quantity_slot_id)
        assert action.status == "skipped"
        assert action.result["error_code"] == "content_contract_replan_required"
        assert cycle_slot.current_action_id is None
        assert cycle_slot.slot_state == "replan_required"
        assert quantity.state == "open"


def test_legacy_scope_is_excluded_from_claim_and_stops_before_provider() -> None:
    sessions = _sessions()
    with sessions() as session:
        _seed_scope(session)
        action = _seed_bound_legacy_action(session, "a-gated")
        session.commit()

        ready_ids = set(session.scalars(select(Action.id).where(
            dispatcher._ai_legacy_scope_is_ready(),
        )))
        assert action.id not in ready_ids

        action.status = "executing"
        original_payload = dict(action.payload)
        assert dispatcher.dispatch_action(session, action) is False
        assert action.status == "executing"
        assert action.payload == original_payload
