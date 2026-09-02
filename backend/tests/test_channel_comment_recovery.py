from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import (
    Action,
    ChannelCommentRecoveryManifest,
    CommentFulfillmentObligation,
    ExecutionAttempt,
    TaskCommentCapacityReservation,
)
from app.services.task_center.channel_comment_capacity import mark_comment_capacity_gateway_hold
from app.services.task_center.channel_comment_recovery import (
    CLOSE_NO_EFFECT_UNKNOWN,
    RETIRE_PRE_GATEWAY,
    RecoveryApplyRequest,
    RecoveryPreviewRequest,
    apply_channel_comment_recovery,
    preview_channel_comment_recovery,
    readback_channel_comment_recovery,
)
from app.services.task_center.executors import channel_comment
from channel_comment_planner_test_support import (
    STABLE_PLANNER_NOW,
    fixed_profile,
    forbid_planner_external_boundaries,
    planner_session,
    seed_comment_task,
)
from test_channel_comment_plan_contract import _enable_grounding_plan


pytestmark = pytest.mark.no_postgres
DEPLOYED_SHA = "a" * 40


def _preview_request(task_id: str, kind: str, *, action_ids=(), evidence=None):
    return RecoveryPreviewRequest(
        task_id=task_id,
        expected_deployed_sha=DEPLOYED_SHA,
        recovery_kind=kind,
        operator_id="recovery-operator",
        approval_reference="approval-ticket-1",
        previewed_at=STABLE_PLANNER_NOW,
        expires_at=STABLE_PLANNER_NOW + timedelta(hours=1),
        exact_action_ids=tuple(action_ids),
        authoritative_no_effect_evidence=evidence,
    )


def _apply_request(manifest: ChannelCommentRecoveryManifest):
    return RecoveryApplyRequest(
        manifest_id=manifest.id,
        expected_preview_hash=manifest.preview_hash,
        current_deployed_sha=DEPLOYED_SHA,
        operator_id="recovery-operator",
        approval_reference="approval-ticket-1",
        applied_at=STABLE_PLANNER_NOW + timedelta(minutes=5),
    )


def _paused_plan(session, monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    task = seed_comment_task(session, mode="comment", target_count=3)
    _enable_grounding_plan(session, task)
    channel_comment.build_plan(session, task)
    task.status = "paused"
    session.commit()
    return task


def test_preview_apply_retires_only_hash_locked_pre_gateway_actions(monkeypatch) -> None:
    with planner_session() as session:
        task = _paused_plan(session, monkeypatch)
        action_ids = tuple(session.scalars(select(Action.id).where(
            Action.action_type == "post_comment",
        )))
        manifest = preview_channel_comment_recovery(
            session, _preview_request(task.id, RETIRE_PRE_GATEWAY, action_ids=action_ids),
        )
        session.commit()

        applied = apply_channel_comment_recovery(session, _apply_request(manifest))
        session.commit()
        actions = list(session.scalars(select(Action).where(Action.id.in_(action_ids))))
        obligations = list(session.scalars(select(CommentFulfillmentObligation)))

        assert applied.manifest_state == "applied"
        assert len(applied.readback_hash) == 64
        assert readback_channel_comment_recovery(session, applied.id)["readback_hash"] == applied.readback_hash
        assert all(action.status == "skipped" for action in actions)
        assert all((action.result or {}).get("error_code") == RETIRE_PRE_GATEWAY for action in actions)
        assert all(item.status == "terminated" and item.current_action_id is None for item in obligations)


def test_apply_stops_without_writes_when_task_revision_drifted(monkeypatch) -> None:
    with planner_session() as session:
        task = _paused_plan(session, monkeypatch)
        action_ids = tuple(session.scalars(select(Action.id).where(
            Action.action_type == "post_comment",
        )))
        manifest = preview_channel_comment_recovery(
            session, _preview_request(task.id, RETIRE_PRE_GATEWAY, action_ids=action_ids),
        )
        session.commit()
        task.config_revision += 1
        session.commit()

        with pytest.raises(ValueError, match="channel_comment_recovery_snapshot_drift"):
            apply_channel_comment_recovery(session, _apply_request(manifest))
        session.rollback()

        assert all(
            status == "pending"
            for status in session.scalars(select(Action.status).where(Action.id.in_(action_ids)))
        )


def test_unknown_closes_only_with_authoritative_no_effect_evidence(monkeypatch) -> None:
    with planner_session() as session:
        task = _paused_plan(session, monkeypatch)
        action = session.scalar(select(Action).where(Action.action_type == "post_comment"))
        action.status = "unknown_after_send"
        session.add(ExecutionAttempt(
            tenant_id=task.tenant_id,
            action_id=action.id,
            account_id=action.account_id,
            attempt_no=1,
            status="unknown_after_send",
            gateway_call_started_at=STABLE_PLANNER_NOW,
        ))
        mark_comment_capacity_gateway_hold(session, action.id)
        session.commit()
        evidence = {action.id: "telegram-readback:no-comment:9001:account-101"}
        manifest = preview_channel_comment_recovery(
            session,
            _preview_request(
                task.id, CLOSE_NO_EFFECT_UNKNOWN,
                action_ids=(action.id,), evidence=evidence,
            ),
        )
        session.commit()

        apply_channel_comment_recovery(session, _apply_request(manifest))
        session.commit()
        obligation = session.scalar(select(CommentFulfillmentObligation).where(
            CommentFulfillmentObligation.current_action_id == action.id,
        ))
        reservation = session.scalar(select(TaskCommentCapacityReservation).where(
            TaskCommentCapacityReservation.obligation_id == obligation.id,
        ))

        assert action.status == "failed"
        assert (action.result or {}).get("authoritative_no_effect_evidence_ref") == evidence[action.id]
        assert obligation.status == "closed_no_effect"
        assert reservation.reservation_state == "released"
