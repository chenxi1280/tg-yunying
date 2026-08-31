from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models import (
    Action,
    ChannelCommentGroundingAssignment,
    ChannelCommentOrdinalAccountBinding,
    ChannelCommentPlanContract,
    ChannelMessage,
    ChannelMessageSourceRevision,
    CommentFulfillmentObligation,
    TaskCommentCapacityPeriod,
    TaskCommentCapacityReservation,
)
from app.services.task_center.executors import channel_comment
from app.services.task_center.channel_comment_acceptance import channel_comment_acceptance
from app.services.task_center.channel_comment_capacity import (
    mark_comment_capacity_gateway_hold,
    remaining_comment_capacity,
    reserve_comment_capacity,
    settle_comment_capacity,
)
from app.services.task_center.channel_comment_grounding_guard import comment_grounding_send_blocker
from app.services.task_center.channel_payloads import PostCommentPayload
from channel_comment_planner_test_support import (
    STABLE_PLANNER_NOW,
    fixed_profile,
    forbid_planner_external_boundaries,
    planner_session,
    seed_comment_task,
)


pytestmark = pytest.mark.no_postgres


def _enable_grounding_plan(session, task, *, source_text: str = "频道事实正文") -> None:
    task.fulfillment_contract_version = "fact_first_v3"
    task.type_config = {
        **task.type_config,
        "rolling_window_days": 3,
        "daily_comment_cap": 10,
        "channel_comment_grounding_v1_enabled": True,
        "unicode_emoji_enabled": True,
        "image_meme_enabled": False,
        "unicode_emoji_weight_bps": 10000,
        "image_meme_weight_bps": 0,
        "context_bound_schedule_window_seconds": 3 * 24 * 60 * 60,
    }
    message = session.get(ChannelMessage, 41)
    message.content_preview = source_text
    message.published_at = datetime(2030, 8, 1, 10, 0, 0)
    revision = ChannelMessageSourceRevision(
        id="source-revision-1",
        tenant_id=1,
        channel_message_id=message.id,
        source_revision=1,
        source_remote_message_id=message.message_id,
        source_published_at=message.published_at,
        source_observed_at=STABLE_PLANNER_NOW,
        source_text_snapshot=source_text,
        source_content_hash="a" * 64,
        observation_identity_hash="b" * 64,
        source_operation="observed",
    )
    session.add(revision)
    session.flush()
    message.current_source_revision_id = revision.id
    session.commit()


def test_grounding_plan_freezes_distinct_target_and_survives_config_revision(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=99)
        _enable_grounding_plan(session, task)

        created = channel_comment.build_plan(session, task)
        plan = session.scalar(select(ChannelCommentPlanContract))
        obligation_count = session.scalar(select(func.count(CommentFulfillmentObligation.id)))
        binding_accounts = list(session.scalars(
            select(ChannelCommentOrdinalAccountBinding.account_id)
            .order_by(ChannelCommentOrdinalAccountBinding.target_ordinal)
        ))
        assignments = list(session.scalars(
            select(ChannelCommentGroundingAssignment)
            .order_by(ChannelCommentGroundingAssignment.target_ordinal)
        ))
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))

        task.config_revision = 2
        task.type_config = {**task.type_config, "target_comments_per_message": 1}
        session.commit()
        created_after_edit = channel_comment.build_plan(session, task)

        assert created == plan.required_distinct_account_count == 2
        assert obligation_count == 2
        assert len(binding_accounts) == len(set(binding_accounts)) == 2
        assert len(assignments) == 2
        assert {action.payload["source_revision_id"] for action in actions} == {
            "source-revision-1",
        }
        assert all(action.payload["grounding_assignment_id"] for action in actions)
        assert created_after_edit == 0
        assert session.scalar(select(func.count(ChannelCommentPlanContract.id))) == 1
        assert session.scalar(select(func.count(CommentFulfillmentObligation.id))) == 2


def test_empty_source_freezes_planned_fallback_actions(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task, source_text="")

        created = channel_comment.build_plan(session, task)
        plan = session.scalar(select(ChannelCommentPlanContract))
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))

    assert created == 2
    assert plan.grounding_required_count == 0
    assert plan.planned_fallback_count == 2
    assert {action.payload["comment_fallback_intent_kind"] for action in actions} == {"planned"}
    assert {action.payload["grounding_assignment_id"] for action in actions} == {""}


def test_daily_cap_applies_per_continuous_period_without_shrinking_obligations(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        task.type_config = {**task.type_config, "daily_comment_cap": 1}
        session.commit()

        created = channel_comment.build_plan(session, task)
        obligations = session.scalar(select(func.count(CommentFulfillmentObligation.id)))
        action_rows = list(session.scalars(select(Action).where(Action.task_id == task.id)))
        reservations = list(session.scalars(select(TaskCommentCapacityReservation)))
        periods = list(session.scalars(select(TaskCommentCapacityPeriod)))

    assert created == len(action_rows) == 2
    assert obligations == 2
    assert len(reservations) == len(periods) == 2
    assert {period.capacity_limit for period in periods} == {1}
    assert {row.capacity_period_id for row in reservations} == {
        period.id for period in periods
    }
    assert {row.reservation_state for row in reservations} == {"action_reserved"}


def test_capacity_reservation_advances_through_gateway_and_remote_fact(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)
        action = session.scalar(select(Action).where(Action.task_id == task.id))

        mark_comment_capacity_gateway_hold(session, action.id)
        reservation = session.scalar(select(TaskCommentCapacityReservation).where(
            TaskCommentCapacityReservation.action_id == action.id,
        ))
        assert reservation.reservation_state == "gateway_hold"

        settle_comment_capacity(session, reservation.obligation_id, confirmed=True)
        assert reservation.reservation_state == "confirmed"


def test_acceptance_requires_typed_fact_and_grounding_evidence(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)
        obligations = list(session.scalars(select(CommentFulfillmentObligation)))
        for obligation in obligations:
            action = session.get(Action, obligation.current_action_id)
            action.status = "success"
            action.payload = {
                **action.payload,
                "content_source": "normal",
                "comment_text": "频道事实正文",
            }
            action.result = {
                "comment_quality_audit": {
                    "two_stage_evaluator_evidence": {
                        "semantic_review": {"decision": "pass"},
                    },
                },
            }
            obligation.remote_comment_id = f"remote-{obligation.target_ordinal}"
            obligation.status = "confirmed"
        session.flush()

        acceptance = channel_comment_acceptance(session, task)

    assert acceptance["quantity_status"] == "met"
    assert acceptance["content_mix_status"] == "met"
    assert acceptance["grounding_quality_status"] == "met"
    assert acceptance["acceptance_status"] == "met"


def test_timezone_change_creates_contiguous_capacity_periods() -> None:
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        task.timezone = "Asia/Shanghai"
        session.flush()
        assert remaining_comment_capacity(
            session, task, 10, at=STABLE_PLANNER_NOW,
        ) == 10
        first = session.scalar(select(TaskCommentCapacityPeriod))

        task.timezone = "America/New_York"
        next_at = first.period_end_at.replace(tzinfo=None)
        assert remaining_comment_capacity(
            session, task, 10, at=next_at,
        ) >= 0
        periods = list(session.scalars(
            select(TaskCommentCapacityPeriod)
            .order_by(TaskCommentCapacityPeriod.period_start_at)
        ))

    assert len(periods) == 2
    assert periods[1].period_start_at == periods[0].period_end_at
    assert periods[1].calendar_revision == 2
    assert periods[1].capacity_limit <= 10


@pytest.mark.parametrize(
    ("occupied_offset_hours", "candidate_offset_hours", "expected_reserved"),
    ((0, 23, False), (23, 0, False), (0, 24, True)),
)
def test_rolling_24h_cap_spans_capacity_periods(
    monkeypatch,
    occupied_offset_hours: int,
    candidate_offset_hours: int,
    expected_reserved: bool,
) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        task.type_config = {**task.type_config, "daily_comment_cap": 1}
        session.commit()
        channel_comment.build_plan(session, task)
        reservations = list(session.scalars(
            select(TaskCommentCapacityReservation)
            .order_by(TaskCommentCapacityReservation.scheduled_for_at)
        ))
        first, second = reservations
        first.scheduled_for_at = STABLE_PLANNER_NOW + timedelta(
            hours=occupied_offset_hours,
        )
        second.reservation_state = "released"
        session.flush()
        obligation = session.get(CommentFulfillmentObligation, second.obligation_id)

        reserved = reserve_comment_capacity(
            session,
            task,
            obligation,
            scheduled_at=STABLE_PLANNER_NOW + timedelta(
                hours=candidate_offset_hours,
            ),
            daily_cap=1,
        )

        assert (reserved is not None) is expected_reserved
        assert second.reservation_state == (
            "plan_reserved" if expected_reserved else "released"
        )
        assert session.scalar(select(func.count(TaskCommentCapacityReservation.id))) == 2



def test_source_edit_blocks_frozen_action_before_gateway(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)
        action = session.scalar(select(Action).where(Action.task_id == task.id))
        message = session.get(ChannelMessage, 41)
        edited = ChannelMessageSourceRevision(
            id="source-revision-2",
            tenant_id=1,
            channel_message_id=message.id,
            source_revision=2,
            source_remote_message_id=message.message_id,
            source_published_at=message.published_at,
            source_observed_at=STABLE_PLANNER_NOW,
            source_text_snapshot="编辑后的频道事实",
            source_content_hash="c" * 64,
            observation_identity_hash="d" * 64,
            source_operation="edited",
        )
        session.add(edited)
        session.flush()
        message.current_source_revision_id = edited.id
        session.flush()

        blocker = comment_grounding_send_blocker(
            session, action, PostCommentPayload.model_validate(action.payload),
        )

    assert blocker == "source_revision_superseded"


def test_released_capacity_blocks_frozen_action_before_gateway(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)
        action = session.scalar(select(Action).where(Action.task_id == task.id))
        reservation = session.scalar(select(TaskCommentCapacityReservation).where(
            TaskCommentCapacityReservation.action_id == action.id,
        ))
        reservation.reservation_state = "released"
        session.flush()

        blocker = comment_grounding_send_blocker(
            session, action, PostCommentPayload.model_validate(action.payload),
        )

    assert blocker == "comment_capacity_reservation_not_action_reserved"
