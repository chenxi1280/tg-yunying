from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import (
    Action,
    ChannelCommentCapacityAllocationEpoch,
    ChannelCommentPlanContract,
    ChannelCommentPlanLifecycleEvent,
    CommentFulfillmentObligation,
    GenerationJob,
    SourcePacingAdmission,
    Task,
    TaskCommentCapacityReservation,
)
from app.services.task_center.service import delete_task
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
    _ordered_obligations,
    _seed_pre_gateway_generation_and_source_admission,
)


pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize("held_state", ("unknown", "confirmed"))
def test_soft_delete_terminates_only_pre_gateway_comment_owner(
    monkeypatch,
    held_state: str,
) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        from app.services.task_center.executors import channel_comment

        channel_comment.build_plan(session, task)
        actions = sorted(
            session.scalars(select(Action).where(Action.task_id == task.id)),
            key=lambda row: int(row.payload["target_ordinal"]),
        )
        movable_action, held_action = actions
        obligations = _ordered_obligations(session)
        movable = next(row for row in obligations if row.current_action_id == movable_action.id)
        held = next(row for row in obligations if row.current_action_id == held_action.id)
        _seed_pre_gateway_generation_and_source_admission(
            session, task, action=movable_action, obligation=movable,
        )
        _hold_delete_identity(session, held_action, held, held_state=held_state)
        frozen_held = (
            held.current_action_id,
            held.grounding_assignment_id,
            dict(held_action.payload),
            held.remote_comment_id,
        )
        old_epoch = int(task.task_lifecycle_epoch or 1)
        monkeypatch.setattr(
            "app.services.task_center.service._now",
            lambda: STABLE_PLANNER_NOW,
        )

        delete_task(session, task.tenant_id, task.id, "operator")
        result = _delete_result(session, task.id, movable.id, held.id)

    assert result["task_status"] == "deleted"
    assert result["task_epoch"] == old_epoch + 1
    assert result["plan_state"] == "terminated_by_operator"
    assert result["movable_status"] == "terminated_by_operator"
    assert result["movable_action_status"] == "cancelled"
    assert result["generation_state"] == "failed"
    assert result["capacity_state"] == "released"
    assert result["source_admission_state"] == "cancelled_pre_gateway"
    assert result["held_identity"] == frozen_held
    assert result["held_status"] == held_state
    assert result["held_capacity_state"] == held_state.replace("unknown", "gateway_hold")
    assert result["event_count"] == 1
    assert result["allocation_epochs"] == [1, 2]


def _delete_result(
    session,
    task_id: str,
    movable_id: str,
    held_id: str,
) -> dict:
    movable = session.get(CommentFulfillmentObligation, movable_id)
    held = session.get(CommentFulfillmentObligation, held_id)
    plan = session.get(ChannelCommentPlanContract, movable.plan_contract_id)
    capacity = session.scalar(select(TaskCommentCapacityReservation).where(
        TaskCommentCapacityReservation.obligation_id == movable.id,
    ))
    held_capacity = session.scalar(select(TaskCommentCapacityReservation).where(
        TaskCommentCapacityReservation.obligation_id == held.id,
    ))
    held_action = session.get(Action, held.current_action_id)
    task = session.get(Task, task_id)
    return {
        "task_status": task.status,
        "task_epoch": int(task.task_lifecycle_epoch),
        "plan_state": plan.contract_state,
        "movable_status": movable.status,
        "movable_action_status": session.get(Action, capacity.action_id).status,
        "generation_state": session.get(
            GenerationJob, "source-delete-generation-job",
        ).state,
        "capacity_state": capacity.reservation_state,
        "source_admission_state": session.get(
            SourcePacingAdmission, "source-delete-admission",
        ).state,
        "held_identity": (
            held.current_action_id,
            held.grounding_assignment_id,
            dict(held_action.payload),
            held.remote_comment_id,
        ),
        "held_status": held.status,
        "held_capacity_state": held_capacity.reservation_state,
        "event_count": int(session.scalar(select(func.count(
            ChannelCommentPlanLifecycleEvent.id,
        )).where(
            ChannelCommentPlanLifecycleEvent.task_id == task_id,
            ChannelCommentPlanLifecycleEvent.event_type == "delete",
        )) or 0),
        "allocation_epochs": list(session.scalars(select(
            ChannelCommentCapacityAllocationEpoch.allocation_epoch,
        ).order_by(ChannelCommentCapacityAllocationEpoch.allocation_epoch))),
    }
