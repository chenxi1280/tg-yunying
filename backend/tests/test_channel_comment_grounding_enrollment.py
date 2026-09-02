from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import (
    AuditLog,
    ChannelCommentGroundingEnrollment,
    ChannelDiscussionGroupBinding,
    ChannelMessage,
)
from app.services.task_center.channel_comment_discussion_contracts import EnrollmentRequest
from app.services.task_center.channel_comment_grounding_enrollment import (
    EnrollmentCloseRequest,
    activate_grounding_enrollment,
    active_grounding_enrollment,
    close_grounding_enrollment,
)
from channel_comment_planner_test_support import planner_session, seed_comment_task
from test_channel_comment_plan_contract import _enable_grounding_plan
from app.api.routers.task_center import router


pytestmark = pytest.mark.no_postgres


def test_enrollment_activation_and_close_routes_are_exposed() -> None:
    paths = {route.path for route in router.routes}
    assert "/api/tasks/{task_id}/channel-comment-grounding-enrollment" in paths
    assert "/api/tasks/{task_id}/channel-comment-grounding-enrollment/close" in paths


def _activation_request(task, binding, enabled_at) -> EnrollmentRequest:
    return EnrollmentRequest(
        tenant_id=task.tenant_id, task_id=task.id,
        expected_config_revision=task.config_revision,
        expected_lifecycle_epoch=task.task_lifecycle_epoch,
        group_binding_id=binding.id, enabled_at=enabled_at,
        operator_id="test-operator", approval_reference="test-approval",
    )


def test_enrollment_exact_replay_is_idempotent_and_identity_drift_conflicts() -> None:
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        binding = session.scalar(select(ChannelDiscussionGroupBinding))
        message = session.get(ChannelMessage, 41)
        enabled_at = message.published_at - timedelta(minutes=1)
        original = active_grounding_enrollment(session, task)

        replay = activate_grounding_enrollment(
            session, _activation_request(task, binding, enabled_at),
        )

        assert replay.id == original.id
        assert session.scalar(select(func.count(ChannelCommentGroundingEnrollment.id))) == 1
        with pytest.raises(ValueError, match="channel_comment_enrollment_identity_conflict"):
            activate_grounding_enrollment(
                session,
                _activation_request(task, binding, enabled_at + timedelta(seconds=1)),
            )


def test_enrollment_close_disables_runtime_owner_and_audits() -> None:
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        enrollment = active_grounding_enrollment(session, task)

        closed = close_grounding_enrollment(session, EnrollmentCloseRequest(
            tenant_id=task.tenant_id, task_id=task.id, enrollment_id=enrollment.id,
            expected_config_revision=task.config_revision,
            expected_lifecycle_epoch=task.task_lifecycle_epoch,
            closed_at=enrollment.enabled_at + timedelta(minutes=1),
            operator_id="test-operator", approval_reference="close-approval",
        ))

        assert closed.enrollment_state == "closed"
        assert active_grounding_enrollment(session, task) is None
        assert session.scalar(select(func.count(AuditLog.id)).where(
            AuditLog.target_type == "channel_comment_grounding_enrollment",
        )) == 2
