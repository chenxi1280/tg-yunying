from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import (
    Action,
    ChannelCommentCapacityAllocationEpoch,
    ChannelCommentGroundingAssignment,
    ChannelCommentPlanContract,
    ChannelCommentPlanLifecycleEvent,
    ChannelMessage,
    CommentFulfillmentObligation,
    ExecutionAttempt,
    TaskCommentCapacityReservation,
)
from app.services.task_center.channel_comment_content_revision import (
    reconcile_channel_comment_source_edit,
)
from app.services.task_center.executors import channel_comment
from app.services.task_center.service import pause_task, resume_task
from channel_comment_planner_test_support import (
    STABLE_PLANNER_NOW,
    fixed_profile,
    forbid_planner_external_boundaries,
    planner_session,
    seed_comment_task,
)
from test_channel_comment_plan_contract import (
    _enable_grounding_plan,
    _hold_delete_identity,
    _new_edited_source,
    _obligation_shape,
    _ordered_obligations,
)


pytestmark = pytest.mark.no_postgres


def test_resume_reuses_paused_identity_and_remaining_curve(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)
        actions = sorted(
            session.scalars(select(Action).where(Action.task_id == task.id)),
            key=lambda row: int(row.payload["target_ordinal"]),
        )
        movable_action, held_action = actions
        obligations = _ordered_obligations(session)
        original_shape = _obligation_shape(obligations)
        movable = next(row for row in obligations if row.current_action_id == movable_action.id)
        held = next(row for row in obligations if row.current_action_id == held_action.id)
        _hold_delete_identity(session, held_action, held, held_state="unknown")
        held_identity = (held.current_action_id, held.grounding_assignment_id, dict(held_action.payload))
        pause_at = STABLE_PLANNER_NOW + timedelta(hours=1)
        monkeypatch.setattr("app.services.task_center.service._now", lambda: pause_at)
        pause_task(session, task.tenant_id, task.id, "operator")
        edited = _new_edited_source(session, session.get(ChannelMessage, 41))
        edited_id = edited.id
        reconcile_channel_comment_source_edit(
            session, session.get(ChannelMessage, 41), edited, at=pause_at,
        )
        resume_at = STABLE_PLANNER_NOW + timedelta(days=1)
        monkeypatch.setattr("app.services.task_center.service._now", lambda: resume_at)

        resumed = resume_task(session, task.tenant_id, task.id, "operator")
        replay = resume_task(session, task.tenant_id, task.id, "operator")
        result = _resume_result(session, movable.id, held.id)

    assert resumed.status == replay.status == "running"
    assert resumed.task_lifecycle_epoch == replay.task_lifecycle_epoch
    assert result["shape"] == original_shape
    assert result["movable_status"] == "replan_required"
    assert result["movable_action_id"] is None
    assert result["assignment_source_revision"] == edited_id
    assert result["scheduled_for_at"] == resume_at
    assert result["held_identity"] == held_identity
    assert result["held_status"] == "unknown"
    assert result["held_capacity"] == "gateway_hold"
    assert result["events"] == ["pause", "resume"]
    assert result["epochs"] == [1, 2, 3]


def test_resume_never_reopens_deadline_missed_or_gateway_identity(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)
        actions = sorted(
            session.scalars(select(Action).where(Action.task_id == task.id)),
            key=lambda row: int(row.payload["target_ordinal"]),
        )
        gateway_action, expiring_action = actions
        obligations = _ordered_obligations(session)
        gateway = next(row for row in obligations if row.current_action_id == gateway_action.id)
        missed = next(row for row in obligations if row.current_action_id == expiring_action.id)
        deadline = session.get(ChannelCommentPlanContract, missed.plan_contract_id).deadline_at
        gateway_action.status = "executing"
        session.add(ExecutionAttempt(
            id="resume-gateway-attempt", tenant_id=task.tenant_id,
            action_id=gateway_action.id, worker_id="worker", attempt_no=1,
            status="gateway_call_started", before_call_at=deadline,
            gateway_call_started_at=deadline,
        ))
        now = deadline + timedelta(seconds=1)
        monkeypatch.setattr("app.services.task_center.service._now", lambda: now)
        pause_task(session, task.tenant_id, task.id, "operator")
        resume_task(session, task.tenant_id, task.id, "operator")

        assert missed.status == "missed_task_paused"
        assert missed.current_action_id is None
        assert gateway.status == "pending"
        assert gateway.current_action_id == gateway_action.id
        assert gateway_action.status == "executing"
        assert _event_count(session, "resume") == 1


def _resume_result(session, movable_id: str, held_id: str) -> dict:
    movable = session.get(CommentFulfillmentObligation, movable_id)
    held = session.get(CommentFulfillmentObligation, held_id)
    assignment = session.get(ChannelCommentGroundingAssignment, movable.grounding_assignment_id)
    capacity = session.scalar(select(TaskCommentCapacityReservation).where(
        TaskCommentCapacityReservation.obligation_id == movable.id,
        TaskCommentCapacityReservation.reservation_state == "plan_reserved",
    ))
    held_capacity = session.scalar(select(TaskCommentCapacityReservation).where(
        TaskCommentCapacityReservation.obligation_id == held.id,
    ))
    held_action = session.get(Action, held.current_action_id)
    return {
        "shape": _obligation_shape(_ordered_obligations(session)),
        "movable_status": movable.status,
        "movable_action_id": movable.current_action_id,
        "assignment_source_revision": assignment.source_revision_id,
        "scheduled_for_at": capacity.scheduled_for_at,
        "held_identity": (held.current_action_id, held.grounding_assignment_id, dict(held_action.payload)),
        "held_status": held.status,
        "held_capacity": held_capacity.reservation_state,
        "events": list(session.scalars(select(
            ChannelCommentPlanLifecycleEvent.event_type,
        ).order_by(ChannelCommentPlanLifecycleEvent.occurred_at))),
        "epochs": list(session.scalars(select(
            ChannelCommentCapacityAllocationEpoch.allocation_epoch,
        ).order_by(ChannelCommentCapacityAllocationEpoch.allocation_epoch))),
    }


def _event_count(session, event_type: str) -> int:
    return int(session.scalar(select(func.count(ChannelCommentPlanLifecycleEvent.id)).where(
        ChannelCommentPlanLifecycleEvent.event_type == event_type,
    )) or 0)
